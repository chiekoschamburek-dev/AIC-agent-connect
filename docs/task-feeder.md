# 任务派发器：让调用量真实增长，且不重复

> 前置：**必须先完成计量修复**（`amp-access-fix-runbook.md`）。
> 在 `amp_hooks` 没挂上之前，本派发器可以正常调用、但对排行榜贡献 0。

对应代码：`acps_cluster/feed_tasks.py`

## 0. 最重要的一条：只派给 supervisor

**2026-10-19 现网实测结论**：

> 平台排行榜按 **caller 侧 span** 归属计数——即 `record.aic == body.caller.aic`
> 的记录（由**发起方**发出的那条）。被调方（partner）自己记的 inbound 记录
> 平台**不认**。

这解释了"计量修好后榜单仍为 0"，也决定了派发策略：

| 派发方式 | 产生的记录 | 平台计数 | 实测 |
|---|---|---|---|
| 脚本直连 partner（旧默认） | 只有 partner 侧（被调方） | ❌ | 360 条进 Kafka、消费组积压 0、DLQ 无拒收，榜单恒 0 |
| **派给 supervisor**（现默认） | supervisor 侧 caller 记录 + 各伙伴 callee | ✅ | 榜单立刻出现 supervisor 与 scout，并从 9 涨到 97 |

supervisor 一次运行会**扇出多个子委派**（scout 检索、writer 起草…），
一次派发等于多条有效调用。所以 `--targets` 默认值已改为 `supervisor`；
只有要压测单个伙伴吞吐时才显式指定别的。

## 1. 它做了什么

```
本机（调用方证书）
  └─ 真实 mTLS → 目标 agent 的 https://<host>:<port>/rpc
       └─ AIP `start` 命令（dataItems[].metadata.skillId 指明能力）
            └─ 目标 agent 走自己的 PEP 准入 → 执行 → 产出 Product
                 └─ 我们轮询 `get` 到终态，再发 `complete` 关单，记入账本
```

**每个任务在两侧各留一条 access 记录**（我们是 caller、目标是 callee），
但**只有 caller 侧会被平台计入榜单**（见 §0）。

> ⚠️ 它**不会**用 `runtime.run()` 或本机直调能力——那条路不经过传输层，不计数。

> ⚠️ AIP 的产物态是 `awaiting-completion`（不是 `completed`），必须由 leader
> 发 `complete` 才进终态。派发器两条都处理了：既把 `awaiting-completion` 视为成功，
> 也会补发关单。协议规定 `complete` **只能**在该状态发送。

## 2. 为什么不会重复

| 层 | 机制 |
|---|---|
| 组合池 | 8 种任务形态 × 24 个主题 = **192 个不同组合** |
| 指纹 | `sha256(agent\|skill\|topic)`，同组合永不二次派发 |
| 账本 | `.feeder/ledger.jsonl` 追加记录，重启后继续接着取 |
| 轮转 | 每轮优先派给"当前派得最少"的 agent，避免单点打满、也更像真实协作 |

跑满 192 个组合后，脚本会自动停止并提示扩充 `TOPICS`（在 `feed_tasks.py` 顶部）。

## 3. 覆盖哪些 agent

| agent | 能力 | 形态 | 是否需要令牌 |
|---|---|---|---|
| supervisor | `research.orchestrate` | 常规协作（检索→大纲）/ **AutoResearch 复现**（`--include-heavy`） | 否 |
| scout | `arxiv.search` / `semanticscholar.search` / `openalex.search` | 3 种检索视角 | 否（PEP L0 公开） |
| synthesis | `paper.extract` | 精读四要素抽取 | 否 |
| writer | `paper.write` | 论文大纲起草 | 否 |
| critic | `paper.review` | 严格审稿裁决 | 否 |
| contradiction | `contradiction.detect` | 矛盾对 + 调和假说 | 否 |
| frontier | `frontier.detect` | 新兴方向探测 | 否 |
| experiment | `exp.run` | 最小实验设计 | **是**（默认不派） |

默认只派公开能力（8 种形态 / 6 个 agent），因为它们走 PEP L0 免令牌，
**不需要签发委托令牌就能跑通**，摩擦最小。`experiment` 的 `exp.run` 是受保护的，
需要 `--include-protected`，且目标侧要允许你的调用方身份。

> 注意：gpu / cpu / eval / kb / model 是**本地 dev 身份**（路线 B，未在平台注册），
> 从服务器打不进去，所以不在派发表里。但 `experiment` 会在自己进程内持令牌
> 直连 gpu（服务器侧 `127.0.0.1:8460` 由反向隧道暴露），所以"派给 experiment"是通的。

### 3.2 跨节点：队友的 designer（由 supervisor 调控）

| 项 | 值 |
|---|---|
| 谁调用 | **supervisor**（名册内成员，走 `acps_delegate`） |
| 能力 | `research.assess_feasibility` |
| 在计划中的位置 | **第 1 步**（先做，20 秒内返回） |
| 端点 | `https://192.0.2.10:8590/rpc` |
| 实测 | 21 次调用，HTTP 200，单次 15–68ms 级耗时，产物为结构化研究方案 |

**为什么放在第 1 步**：写在第 3 步时，supervisor 常把长任务改道成后台作业
（`submit_job`），同步阶段收尾时 designer 还没被调用就退出了——实测一个 3 步
任务跑了 14 分钟仍未调到。把快的可行性评估提到最前面，即使后面被截断也已完成。

**不再由脚本直连**：`--designer-per-round` 默认改为 **0**。
脚本直连不产生本侧 caller 记录（不计入榜单），且绕过了 supervisor 的调控，
与"designer 也由 supervisor 调控"的要求冲突。

> 外部社区 agent 仍是**硬隔离**：`ACPS_ROSTER_ALLOW_DISCOVERED=0`，
> 发现缓存里有 60+ 个外部 agent，但规划闸门只认名册。

### 3.1 重型形态：AutoResearch 论文复现（默认关闭）

代码里 `auto_research` 是 supervisor 的**工具**（`MAX_ITERATIONS=2`，
动作 `iterate` / `summarize`，verdict 必须 `supported` / `refuted` / `inconclusive`），
也可以走 `submit_job(mode="auto_research")`。派发器把它登记为**重型形态**：

| 项 | 值 |
|---|---|
| 形态 id | `autoresearch` |
| agent / skill | `supervisor` / `research.orchestrate` |
| 开关 | `--include-heavy`（**默认关**） |
| 配额 | `--heavy-per-round N`（开了之后默认 **1** 个/轮；`0`=不要） |
| 链路 | supervisor → `auto_research` → scout/frontier 定假设 → `experiment(exp.run)` → GPU 沙盒 → 读指标登记 verdict → summarize |

**为什么默认关**：一次复现要真跑 GPU 沙盒 + 多轮迭代，耗时是普通任务数倍、
失败率更高；混进默认轮转会把队列拖满。

**为什么必须要配额**：默认只派 supervisor，而 supervisor 只有 **2 个**形态
（普通协作 + 重型复现），池子一半是重型。只加 `--include-heavy` 不管配额的话，
随机 30 个里会有 **13–16 个**复现任务（2026-10-19 实测），一轮要跑一整天。
配额会让它恰好 N 个，其余名额给普通，混合后整体再打乱（顺序仍随机），
并且**轮内主题不重复**（同一 topic 不会既做普通又做复现）。

```bash
# 每轮恰好 1 个复现（推荐）
python3 feed_tasks.py --include-heavy

# 每轮 3 个复现
python3 feed_tasks.py --include-heavy --heavy-per-round 3

# 先看会派什么
python3 feed_tasks.py --include-heavy --dry-run
```

> 踩过的坑：重型与普通形态的 `agent`+`skill` 完全相同，若指纹只按
> `agent|skill|topic` 计算会**撞车**、重型永远选不中。已改为把形态 `id`
> 混进指纹（仅对声明了 `id` 的形态生效，旧账本不受影响），
> dry-run 输出也会带 `[autoresearch]` 前缀以便区分。

**前置条件**：`exp.run` 是公开能力（实验编排公开可达），但 GPU 侧 PEP 要求
委托令牌——由 experiment 自己签发。服务器侧需能看到 gpu 端点：

```bash
ss -ltnp | grep 8460        # 期望：sshd 在 127.0.0.1:8460 监听（反向隧道）
```

**诚实性约束**（模板里已写死）：只跑 1 轮迭代；拿不到指标必须记
`inconclusive`，**不允许编造分数**；失败要如实说明卡在哪。

## 4. 用法

**默认行为（2026-10-19 起）**：跑 **1 轮 × 30 个**，**全随机顺序**，**串行**执行
（一个任务到终态/超时再派下一个），跑完自动退出。

```bash
cd /opt/wit/my-project/acps_cluster

# ① 先看会派什么（不联网、不记账）
python3 feed_tasks.py --dry-run

# ② 正式跑一轮 30 个（默认就是它）
python3 feed_tasks.py

# ③ 只跑 3 个
python3 feed_tasks.py --limit 3

# ④ 跑两轮、每轮 10 个
python3 feed_tasks.py --round-size 10 --rounds 2

# ⑤ 只派给 writer / critic
python3 feed_tasks.py --targets writer,critic --limit 4

# ⑥ 看战果
python3 feed_tasks.py --stats
```

关键参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--order` | `random` | `random`=全随机（默认）；`balanced`=按 agent 轮转 |
| `--round-size` | **30** | 每轮派几个 |
| `--rounds` | **1** | 跑几轮，跑完即停；`0`=不限（配 `--watch`） |
| `--limit` | 无 | 等价于"这一轮派 N 个、只跑 1 轮"（覆盖 `--round-size`） |
| `--per-round` | 无 | 旧名，等价于 `--round-size` |
| `--rate` | 5 | **串行**：两个任务之间的最小间隔（秒） |
| `--wait` | 600 | 单任务等终态上限（秒）；超时记 `working` 并继续下一个 |
| `--interval` | 60 | 轮与轮之间的间隔（秒） |
| `--hours` | 0 | 总时长上限，0=不限 |
| `--caller` | `supervisor` | 用哪个工作区证书当调用方 |
| `--targets` | `supervisor` | 逗号分隔，只派给这些 agent |
| `--include-protected` | 关 | 是否派受保护能力 |
| `--include-heavy` | 关 | 是否派 AutoResearch 复现（重型） |
| `--heavy-per-round` | 开重型后=1 | 每轮恰好几个复现；`0`=不要 |
| `--seed` | 无 | 固定随机种子（复现演示用） |
| `--ca` | 自动 | 信任锚文件 |
| `--state` | `<cluster>/.feeder` | 账本目录 |

> **池子容量**：默认只派 supervisor，可用组合数 = 主题数（当前 **40**）。
> 所以一轮 30 个能凑满；但要连续跑多轮，主题会被消耗完——耗尽时脚本会
> 提示并提前结束，去 `TOPICS` 里加主题即可。

> **常驻单元**：`wit-task-feeder.service` 已改为批次模式
> （`--round-size 30 --rounds 1 --order random`，`Restart=no`）。
> `systemctl start wit-task-feeder` = 跑一轮 30 个后退出；
> 想再来一轮就再 start 一次。

## 4.1 身份与证书：**必须在服务器上跑**

这是实测结论，不是假设。派发器需要两样东西，它们都只在服务器上成立：

| 需要什么 | 本地工作区的情况 | 后果 |
|---|---|---|
| **调用方客户端证书** | `ws-supervisor/acps/client/` 是 **dev CA** 签的（CN=`…1.1.ZV8ZLB…`） | 平台不认：TLS 层直接被拒 |
| **目标身份与端点** | `acs.final.json` 里是 dev AIC + `https://localhost:8447/rpc` | 连到本机空端口，必然失败 |

实测三种失败形态（都是环境事实，不是脚本 bug）：

```
① Trust bundle 只有 dev CA      → CERTIFICATE_VERIFY_FAILED
② 改用平台身份后                → ReadError（服务端在 TLS 握手时拒绝）
③ 因为平台只信任自己签发的证书   → 本地证书私钥在服务器上，不可能伪造
```

**已做的两个改进**（让服务器上一把过、本地也不至于一脸懵）：

1. **`deploy/platform_identities.json`**：登记线上权威 AIC 与端点。
   本地工作区里是 dev 身份时，脚本自动读这张表，避免"派给不存在的自己"。
   服务器上不需要它（那份 ACS 本身就是权威的），存在也无害。
2. **自动信任锚**：线上服务端会下发中间 CA（`Agent CA Intermediate`），
   脚本抓取它作为信任锚——实测可验证通过。平台根 CA 未公开，
   用中间 CA 当锚是当前唯一可行做法；安全边界见 §8.3。

> 所以：**在服务器上执行本脚本**，本地只做 dry-run 与单测。


## 5. 常驻部署

```bash
install -m 644 deploy/wit-task-feeder.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wit-task-feeder.service
journalctl -u wit-task-feeder -f
```

单元默认：每 15 分钟 2 个、单任务最多等 7 分钟。

## 6. 怎么确认它真的在产生调用量

```bash
# ① 目标侧出现了 access 记录（换 writer 为你派过的 agent）
tail -1 /opt/wit/my-project/acps_cluster/ws-writer/logs/amp_access_*.jsonl | python3 -m json.tool

# ② 账本里任务都是 ok
python3 feed_tasks.py --stats

# ③ 平台榜单（30–60 分钟后）
curl -s "https://wt.ioa.pub/internal/matched-stats/top?startAt=$(date +%Y-%m-%dT%H:00:00+08:00)&endAt=$(date -d '+1 hour' +%Y-%m-%dT%H:00:00+08:00)&n=200" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('callee:', [x for x in d['callee'] if 'VTF97R' in x['aic']])"
```

## 7. 结果的几种含义

| 账本 `outcome` | 含义 | 要不要处理 |
|---|---|---|
| `ok` | 完成且带产物 | 不用 |
| `completed_no_product` | 跑完但没交产物 | 看目标 agent 的 `aip_submit`（本项目有兜底提交钩子，出现即异常） |
| `failed` / `rejected` | 对端失败或拒绝 | 看目标侧 journal；`rejected` 多半是 PEP 拒（能力/身份不匹配） |
| `dispatch_error` | 命令就被拒（如 `code: -32008` = 无凭据） | 该能力不是公开能力，需要令牌通道 |
| `exception` | 网络/TLS 层失败 | 查证书、端口、隧道 |

## 8. 边界与提醒

1. **这不是刷量工具**。它派的是真实可执行任务、主题互不重复、结果落账；
   每个任务都真的消耗对端算力与模型调用。请按答辩/演示的真实需要来跑。
2. **它在累积证据，不是制造假象**。`--stats` 与 `ledger.jsonl` 本身就是
   "我们确实做了 N 次真实协作"的可查证材料，配合平台榜单可以互相印证。
3. **主题池要持续扩充**。一个主题用一次就没有了；想长期跑就按
   `TOPICS` 列表的格式往里面加，指纹机制会自动保证新组合不重样。
4. **对端成本**：每派一个任务，目标 agent 要真的跑一次 LLM（writer/critic 尤其慢）。
   `--rate 20~30` 是保守值，别为了数字把对端压垮——那会让
   `completed_no_product` / `failed` 变多，反而损害证据质量。
