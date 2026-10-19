# L2 作业化交路：受理-作业-取件

> 2026-10-16 落地，2026-10-17 审查加固。对应代码：`acps_cluster/jobs_store.py`、
> `acps_cluster/jobs_runner.py`、`supervisor.py` 的 `submit_job` / `check_job`。
> 回归测试：`acps_cluster/tests/test_jobs_store.py`。

## 1. 为什么需要这条交路

前端/叮当的等待窗口 **< 20 分钟**（两次实测：38.5 分钟与约 20 分钟的任务均被判红），
而 DeepResearch / AutoResearch 这类真实研究天然是**小时级**。让模型在一个 run 里
同步跑完再交付，只有两个结局：前端超时变红，或模型为了"按时交差"提前收尾。

L2 交路把"接到任务"和"跑完任务"拆开：

- **受理**：秒级返回作业号 + 受理回执，本次 run 立即收口；
- **作业**：独立进程在后台跑完整链路，状态与终稿落在自己的作业目录；
- **取件**：用户稍后问「查作业 <作业号>」，秒级拿到状态或终稿。

## 2. 交路总览

```
前端/叮当
   │  任务文本
   ▼
supervisor（本次 run）
   │  submit_job(question, mode)
   ├──────────────► jobs/<job-id>/   目录即作业台账（queued）
   │                 request.txt
   │                 status.json
   │                 log.txt
   │  ◄── 作业号 + 受理回执（用 aip_submit 交付，run 结束）
   │
   └─ detached 进程：python jobs_runner.py jobs/<job-id>
          │  加载 deploy/.env.production，ACPS_SUPERVISOR_NO_SERVE=1
          │  端口 8544（与常驻 8444 隔离，只做出站委派）
          ├── supervisor.main() → 按名册委派伙伴链
          └── ACPS_JOB_REPORT_OUT → jobs/<id>/report.md
                                     status.json → done / failed

用户「查作业 <job-id>」
   ▼
supervisor 本次 run → check_job(job_ref) → 状态 / 终稿 / 日志尾
```

作业进程**不对外服务**：它只做出站委派，所以不注册端点、不与常驻 supervisor
抢 8444，也不会把"作业 worker"误当成新的对外 agent。

## 3. 目录与状态机

每个作业一个目录（`<workspace>/jobs/<job-id>/`，生产上通常是
`/opt/wit/my-project/acps_cluster/ws-supervisor/jobs/`）：

| 文件 | 内容 |
|---|---|
| `request.txt` | 原始任务文本（完整约束，原样传给作业进程） |
| `status.json` | `id / mode / state / created_at / started_at / finished_at / pid / exit_code / question_head / run_status / iterations`，异常时带 `reason` |
| `report.md` | 终稿；作业进程经 `ACPS_JOB_REPORT_OUT` 写入 |
| `log.txt` | stdout/stderr 合流（排障用） |

状态机：

```
queued ──► running ──► done
   │           │
   │           ├──► failed          （作业进程异常 / 没产出终稿）
   └───────────┴──► interrupted     （process-exited / ttl-exceeded）
```

`interrupted` 的两种 `reason`：

- `process-exited`：pid 已不在（取件方读取时顺手收敛）；
- `ttl-exceeded`：进程还活着但已超过 `ACPS_JOB_MAX_RUNTIME`（默认 6 小时）。
  TTL **只改台账状态、不杀进程**；若进程随后正常收尾，`jobs_runner` 会把状态覆写成
  `done/failed`。这样"永远 running"不再永久挂账。

`done` 只在 **`run_status ∈ {completed, success}` 且终稿落盘**时成立；
`budget_exhausted`/异常但留下残稿的 run 记 `failed`（残稿仍可被 `check_job` 取到，
见 §4.2）。`status.json` 同时落盘 `run_status` 与 `iterations`，用户不必只凭"done"猜。

## 4. 工具契约

### 4.0 开工前 intake（先问清楚再动手）

同步链路在委派任何伙伴之前先做一次判断：

- 关键参数齐全，或用户明确说“按默认/直接开始/你决定”→ 立即开工；
- 关键参数缺失且会显著改变产出 → 调 `aip_request_input(question=...)`，把 **3–5 个具体字段 +
  默认值**一次问完（字数/篇幅、范围与材料、引用与审稿、读者与语言、实验时限/算力），然后
  **结束本 run**（`awaiting-input` 是合法收尾，不提交产物）；
- **禁止“先做一半，做完再改”**；同一任务最多澄清一次，用户回答后直接开工；
- 例外：查作业/直接粘贴作业号 → 直接 `check_job`；简单问题、参数齐全、用户说按默认 → 不问。

### 4.1 `submit_job(question, mode)`

- `question`：**完整任务文本**（含全部已知约束），少于 8 字符直接拒绝；
- `mode`：`deep_research` / `auto_research` / `custom`（仅记录，不改变链路）；
- 成功返回 `{ok, job_id, pid, note}`；`note` 强制要求模型**立即**用 `aip_submit` 交付
  受理回执（作业号/模式/取件方式，并注明"不代表任务已完成"），然后结束 run；
- **回执代码兜底**：受理成功即把 `job_id` 记进 run 状态。收尾 `_flush_buffered_submit`
  发现缓冲产物里不含该作业号时，自动追加一条"受理回执"产物（幂等，仅在尚未成功提交时
  触发）；**不要自行估算完成时间**，只转述代码返回的时间字段；
- **job 模式禁止递归**：作业进程自身（`ACPS_JOB_REPORT_OUT` 已设置）不注册 `submit_job`，
  工具入口也会 fail-closed 拒绝；
- **受理后硬闸**：`submit_job` 成功后，本 run 只允许 `aip_submit / aip_fail`；其他工具被代码
  直接拒绝，强制交回执并结束（2026-10-17 12:26 现网 follow-up：受理后继续 arxiv.fetch/
  citation.verify，run 不收尾，前端 ~20 分钟判红）；
- **超预算硬闸**：同步预算耗尽后只允许 `submit_job / aip_submit / aip_fail /
  aip_request_input`，其余调用被拒，强制改道或如实收尾；job 模式不受此闸影响；
- 并发上限默认 **2**（`ACPS_JOB_MAX_CONCURRENT`）；超限明确拒绝，并禁止模型假称已受理；
- 受理前会顺手清理过老终态作业（见 §6）。

### 4.2 `check_job(job_ref, offset=0)`

- `job_ref` 支持 `list`/`ls`/`列表`（最近 8 个作业）或作业号/前缀；**用户直接粘贴
  `job-YYYYMMDD-HHMMSS-xxxx` 也按取件处理**；
- **作业号白名单**：仅接受字母/数字/连字符（`^[A-Za-z0-9-]+$`），绝对路径、`../`
  与 symlink 逃逸一律拒绝（2026-10-17 审查 P1-1）；
- **前缀歧义不猜**：命中多个时返回候选列表，要求给出更长作业号；
- **分段取件**：`report` 默认每段 14000 字符（`ACPS_JOB_REPORT_PAGE_CHARS`）；
  返回 `report_offset / report_total_chars / report_truncated`，未取完时带
  `report_next_offset`——必须继续取，**不要只交第一段**；取完再过 `product_gate`；
- 同时返回 `run_status / iterations`（新作业）；**状态异常但终稿存在**时同样返回
  `report`——不会因为状态写入失败让终稿永久取不到；
- `failed/interrupted`：返回日志尾（30 行）与 `reason`；
- 仍在跑：返回 `elapsed_seconds / suggested_poll_seconds / ttl_remaining_seconds`
  + 日志尾（20 行）；`ttl_remaining_seconds` 是**系统上限剩余**，不是预计完成时间。

## 5. 可靠性设计与 2026-10-17 审查修复

| 问题 | 处理 |
|---|---|
| `status.json` 固定临时名，提交方与作业进程并发写会互相截断 | 唯一临时名 `status.json.<pid>.<rand>.tmp` + `os.replace` 原子替换；读到损坏文件不再静默，向 stderr 留痕 |
| 报告因瞬时读取失败被永久判成 `failed` | 完成判定改为"`report.md` 存在且非空"（`report_present`），不再依赖能否读出内容；`check_job` 也不再按 `state` 门控读取 |
| **Windows 上 `os.kill(pid, 0)` 会直接 `TerminateProcess` 把作业杀掉** | 平台分流：Windows 用 `OpenProcess` + `GetExitCodeProcess`；POSIX 用 `os.kill(pid,0)`，Linux 额外把僵尸态（`Z`）视为已结束 |
| 进程活着但任务卡死，永远 running | TTL（默认 6 小时）→ `interrupted/ttl-exceeded` |
| 前缀命中多个作业可能取错 | `match_jobs` 返回全部候选；`find_job` 只在唯一命中时返回；`check_job` 要求更长作业号 |
| `job_ref` 路径穿越（绝对路径 / `../` / symlink） | 作业号白名单 + `resolve()` 后必须仍在 jobs 根内；`match_jobs`/`find_job` 同源收口（2026-10-17 P1-1） |
| 作业进程内递归提交子作业（链式续命） | job 模式（`ACPS_JOB_REPORT_OUT` 已设置）不注册 `submit_job`；工具入口再 fail-closed 一次（2026-10-17 P1-2） |
| 无清理、无并发上限 | 终态作业保留最近 20 个 / 7 天；受理在进程内加锁并检查并发上限 |
| 回执完全靠模型自觉（忘交/漏作业号 → 用户拿不到取件凭据） | `submit_job` 成功即记录 `job_id`；`_flush_buffered_submit` 在缓冲产物缺作业号时自动追加受理回执（幂等，已成功提交则跳过） |
| 受理后模型继续做同步工作，run 不收尾（2026-10-17 12:26 follow-up 前端判红） | `JobLifecycleFenceHooks`：受理后只放行 `aip_submit/aip_fail`；超预算只放行 `submit_job/aip_submit/aip_fail/aip_request_input` |
| 终稿静默截断 | `report` 分段返回 + `report_truncated/report_next_offset` 显式标记；提示词要求取完全部段再提交 |
| `done` 只看 `report.md` 存在，失败残稿也显示 done | 落盘 `run_status/iterations`；`done` 仅限 `completed/success`，残稿 run 记 `failed` 但终稿仍可取 |
| 终稿非原子写，进程被杀留下半截报告 | 临时文件 + `os.replace` 原子替换 |
| 改道提示最坏要 16–17 分钟才出现 | 同步预算默认 8 分钟；`plan_gate/product_gate/check_job` 等高频工具也带 `budget_note`（代码计时 + 剩余秒数）；job 模式不追加改道提示 |
| 用户直接粘贴作业号，模型可能先追问 | 提示词按 `job-\d{8}-\d{6}-[0-9a-f]{4}` 识别为取件；回执给出"查作业/直接粘贴"两种方式 |
| 作业进程依赖启动方环境 | `jobs_runner` 自行加载 `deploy/.env.production`（覆盖式，照 `run_supervisor.sh` 配方） |
| 作业与常驻服务端口/端点冲突 | `ACPS_SUPERVISOR_NO_SERVE=1` + 显式 worker 端口 8544（不绑定）；只做出站委派 |

## 6. 运维参数

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `ACPS_JOB_MAX_CONCURRENT` | `2` | 同时运行的作业上限 |
| `ACPS_JOB_MAX_RUNTIME` | `21600`（6h） | running 超过该时长判 `ttl-exceeded` |
| `ACPS_JOB_RETENTION_DAYS` | `7` | 终态作业保留天数（超出最近 N 个之外） |
| `ACPS_JOB_KEEP_LAST` | `20` | 无论多老都保留的最近终态作业数 |
| `ACPS_JOB_WORKER_PORT` | `8544` | 作业 worker 的端口语义（`serve=False`，不绑定） |
| `ACPS_JOB_REPORT_OUT` | 内部注入 | 终稿输出路径；由 `jobs_runner` 写成 `jobs/<id>/report.md` |
| `ACPS_JOB_REPORT_PAGE_CHARS` | `14000` | `check_job` 单段终稿字符数；超出用 `offset` 续取 |
| `ACPS_JOB_POLL_HINT_SECONDS` | `600` | 给用户/前端的建议查询间隔（不是完成时间估算） |
| `ACPS_SUPERVISOR_SYNC_BUDGET_MINUTES` | `8` | 同步链路改道预算；超时提示改为作业化 |

手工排查（服务器）：

```bash
ls -lt /opt/wit/my-project/acps_cluster/ws-supervisor/jobs/ | head
cat /opt/wit/my-project/acps_cluster/ws-supervisor/jobs/<job-id>/status.json
tail -n 80 /opt/wit/my-project/acps_cluster/ws-supervisor/jobs/<job-id>/log.txt
```

清理是自动的（每次受理前触发）；确需手工删除时，只删**终态**作业目录，不要删
`queued/running` 的目录。

## 7. 验证

```bash
cd acps_cluster
python -m unittest tests.test_jobs_store -v          # 台账/工具专项
python -m unittest tests.test_job_receipt -v         # 回执自动补交专项
python -m unittest tests.test_wait_awaiting_input -v # 预算/生命周期硬闸专项
python -m unittest discover -s tests -p "test_*.py"  # 全量（当前 510 项，skip 2）
```

`test_jobs_store.py` 覆盖：

- 生命周期（queued → running → done）、死 pid 判中断、列表与前缀查找；
- **前缀歧义拒绝猜**、TTL 超时/未超时、`pid_alive` 对当前进程为真（Windows 回归）；
- 终稿文件存在即可取、状态异常时仍返回终稿；**分页 offset/截断标记**；
- 运行中返回 `suggested_poll_seconds / ttl_remaining_seconds`；`done` 仅限成功收尾；
- 受理成功后记录回执状态；`test_job_receipt.py` 覆盖"无产物也补回执 / 有产物缺作业号追加 /
  已含作业号不重复 / 已成功提交不补"；
- 清理只删过老终态且保留最近 N 个、不删在跑作业；
- 并发上限拒绝新作业；`submit_job` 创建目录并拉起 `jobs_runner.py`。

## 8. 边界与已知限制

- **TTL 不杀进程**：只把台账收口；卡死进程本身仍需人工处置（作业进程是 detached 的）；
- **并发锁是进程内的**：常驻 supervisor 是唯一提交方，所以够用；若将来多进程提交，
  需要文件锁/单写者服务；
- **作业 = 一次完整 supervisor run**：消耗模型步数与配额；作业失败/中断后需要**重新提交**，
  目前没有跨 run 断点恢复（与 wit 侧已知限制一致）；
- **作业进程内不能再提交作业**：job 模式不注册 `submit_job`，工具入口也会拒绝；`check_job`
  保留（排障用），它只能读 jobs 根内的作业，路径穿越已封堵；
- **回执自动补交只在"尚未成功提交"时触发**：若 run 已进入 `awaiting-input`，协议拒绝
  `aip_submit`，此时不会补交；模型应在请求输入前先交付回执；
- **受理后硬闸**：`submit_job` 成功后本 run 只能 `aip_submit/aip_fail`；模型若继续排伙伴/
  等待，会收到代码拒绝（不是静默忽略）。超预算硬闸同理——到点后只能改道/收尾；
- **`ttl_remaining_seconds` 不是 ETA**：它是作业系统上限（默认 6h）减已用时长；真实的
  完成时间未知，不要把它当预计完成时间展示；`suggested_poll_seconds` 只是查询间隔建议；
- **终稿判定按文件存在且非空**：空白报告视为失败；
- **`check_job` 报告截断 14000 字符**：超长报告请直接读作业目录里的 `report.md`；
- 作业与常驻服务共用 `ws-supervisor/acps/aip-tasks.sqlite3` 台账；收尾代签按"本轮亲自派发
  过的 task_ref"收窄，不会替并发 run 关单。

## 9. 代码索引

| 主题 | 位置 |
|---|---|
| 台账/状态机/探活/清理 | `acps_cluster/jobs_store.py` |
| detached 作业执行器 | `acps_cluster/jobs_runner.py` |
| `submit_job` / `check_job` 工具 | `acps_cluster/supervisor.py`（L2 作业化交路段） |
| 收尾代签（product_gate fail-closed） | `acps_cluster/supervisor.py` `UnclosedDeliverablesHooks` |
| 回归测试 | `acps_cluster/tests/test_jobs_store.py` |
