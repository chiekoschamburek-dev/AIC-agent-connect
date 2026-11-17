# 多入口编排 P0 验证报告

> 日期：2026-11-17｜对应计划：`multi-entry-orchestration-plan.md` 的「P0：接口核对」
> 工具：`python -m acps_cluster.tools.p0_checks {acs,jobs,wenji}`
> 原则：本报告只出结论与命令，不改业务代码、不改生产配置。**文档结论不等于服务器验收。**

## 0. 结论摘要

| # | 验证项 | 本地结论 | 还需在哪跑 | 对计划的影响 |
|---|---|---|---|---|
| ① | 新能力对 ACS 指纹的影响 | **已定论**：`public=False` 零影响；`public` 与 `visibility=domain` 都会改 ACS | 无需 | §5.1 必须改成非公开；**不要**用 `visibility=domain` 做入口技能 |
| ② | 后台作业底座 | 代码已复核：`submit_job` 的"受理-作业-取件"**就是**计划 §6.2 要的形态，可直接复用 | 研枢服务器一条命令 | P1 不新写 runner；只需幂等索引 + 业务状态映射 |
| ③ | 问迹并发 | **已定论**：问迹**不是单工作者**（async handler + `asyncio.to_thread`，无执行位信号量） | 问迹主机一条实测 | §6.2 的"释放执行位置"对象要改：不是线程，是入口的 HTTP/任务生命周期 |

## 1. ① ACS 指纹：入口技能必须非公开（已定论）

### 方法

用 `deploy/make_acs.py` 的真实投影逻辑，比较 supervisor 当前 skills 与"加一个同类能力"后的投影：

```bash
python -m acps_cluster.tools.p0_checks acs --json
```

合成三个探针能力：`research.submit`（public=False）、`research.status_public`（public=True）、
`research.domain_demo`（`visibility=domain`, `domains=["yanshu-research"]`）。

### 结果（本地实测）

| 场景 | skills 投影 sha256 | 与基线比较 |
|---|---|---|
| 基线（`research.orchestrate`） | `56be2ab9…90528` | — |
| 新增 `public=False` | `56be2ab9…90528` | **完全不变** |
| 新增 `public=True` | `7c2674a0…32d50` | 新增 `research.status_public` |
| 新增 `visibility=domain` | `48521829…e3a43b1` | 新增 `research.domain_demo`（带 visibility/domains） |

> 说明：这里是 **skills 数组的本地投影哈希**，不是平台完整指纹；但平台指纹由这份投影派生，
> "skills 数组不变"是"指纹不变"的必要条件。仓库自己的记录也一致：非公开能力不进 ACS，
> 新增不会触发重新注册；每次重新注册都会换 AIC。

### 结论与修订

1. **`research.submit/status/cancel` 全部 `public=False`，且不要设 `acps_skill`**。
   `acps_skill` 才是"投影进 ACS"的开关；`public` 只管 PEP 可见性。两者都做对，
   supervisor 的 ACS 一个字不动，**不换 AIC**，表 5-1-1 / PPT / 视频 / 佐证材料无需返工。
   （P1 实现里已用单测锁住这条不变量：`test_root_orchestration.AcsInvariantTest`。）
2. **不要给入口技能写 `visibility=domain`**。它虽然叫"域可见"，但会进 ACS 投影
   （`discovery_metadata` 把 `visibility/domains` 写进技能），同样触发重新注册。
3. "双向授权"的故事仍然成立：**域 grant 可以授权非公开技能**。
   代码依据 `delegation/pep.py::_evaluate_grant`：
   - 授权判定看 grant 的 `acps_assets`（`providerAic + skill`）与域章程的 `asset_allows(role)`；
   - `cfg.domain_skills`（即 `visibility=domain` 的声明）只是"第二把锁"——**没声明就不拦**；
   - `quota_path` 默认由 `default_quota_path(workspace)` 配好（`pep.py:368`），grant 路径开箱可用。
   即：**非公开技能 + 双边域章程 + grant 令牌**，不动 ACS、不开口子。

## 2. ② 后台作业底座：已有实现可复用（待服务器一条命令）

### 代码复核（可复用清单）

`agents/supervisor.py::_make_submit_job_tool` + `runtime/jobs_runner.py` + `runtime/jobs_store.py`
已经实现了计划 §6.2 的全部要素：

| 计划要求 | 现有实现 | 位置 |
|---|---|---|
| 先持久化再返回回执 | `create_job` 写 `request.txt` + `status.json` 后才 `Popen` | `jobs_store.create_job` |
| 受理后释放入口 | `submit_job` 立即返回 `job_id` + 取件方式，要求 `aip_submit` 交受理回执后结束 run | `supervisor.py:1374` |
| 后台推进 | detached `jobs_runner.py`，`start_new_session=True` | `supervisor.py:1358` |
| 并发上限 | `ACPS_JOB_MAX_CONCURRENT`（默认 2）+ 进程内锁 | `jobs_store.count_active` |
| 超时止损 | watchdog（`ACPS_JOB_MAX_RUNTIME`）到点 `failed/ttl-exceeded` | `jobs_runner.py:159` |
| 隔离 | 独立 workspace `ws-supervisor-job`（复制身份、独立任务库、`discovered/` symlink） | `jobs_runner._job_workspace` |
| 取件 | `check_job` / `match_jobs` / `report.md` / 分页 | `jobs_store` |
| 重启恢复 | `resolve_liveness`（pid 死 → `interrupted`） | `jobs_store.resolve_liveness` |

→ **P1 不需要新写 runner**；要补的只有：`request_id` 幂等索引、业务状态（`accepted/running/...`）
映射、根任务级预算计数、结果取件接口。

### 三个未闭环风险

| # | 风险 | 依据 | 处置 |
|---|---|---|---|
| R1 | **同 AIC 二次登录**：`jobs_runner → supervisor.main() → up_until_ready(serve=False, credentials=SUPERVISOR)`；wit 的 `enroll` 先用 `token_store`/`_token_expired` 判断，**token 仍有效时可能跳过登录**，过期/被轮换时才会用同一账号重新登录 | `jobs_runner.py:177-180`、`wit/acps/provisioner`（`enroll` 的 token 分支） | 用 `--probe-workspace` 在作业期间探活常驻 supervisor；同时记录作业日志里是否出现 login/reauth |
| R2 | **job workspace 是一次性复制**：证书/ACS 轮换后 `ws-supervisor-job/acps` 变旧 | `_job_workspace()` 首次复制后直接复用 | 轮换后删除 `ws-supervisor-job/acps` 重建；P1 加一个 `--refresh-workspace` 检查 |
| R3 | **缺 `ws-supervisor/acps` 时**：`_job_workspace()` 抛错在 try 之外，`status.json` 停在 `running` | `jobs_runner.py:141` | 生产不会触发；自检脚本已能识别（`--static` 的 `supervisor-acps` 项） |

### 服务器执行（一条命令判定 R1）

```bash
cd /opt/wit/yanshu-AIC
python -m acps_cluster.tools.p0_checks jobs --static          # 先看前置
python -m acps_cluster.tools.p0_checks jobs --run --yes \
  --timeout 600 --probe-workspace acps_cluster/ws-kb          # 真跑 + 作业期间探活
```

**通过标准**（四项全满足）：

1. `state=done`、`report_bytes>0`、`exit_code=0`；
2. `probe.ok=true`（作业运行期间常驻 supervisor 仍能完成一条 `research.orchestrate`）；
3. 作业结束后 `doctor --discover` 里 supervisor 仍 `active`；
4. 日志无 `needs_login` / `acps_config_changed_after_submission` / `lock-timeout`。

**失败判读**：

| 现象 | 含义 | 下一步 |
|---|---|---|
| `run_status=needs_login` | 后台进程拿不到/用不了注册凭据 | 改"独立 worker 身份"或"进程内 detached"，见决策树 |
| `lock-timeout` | 上一个 job 卡死占锁 | 查 `ACPS_JOB_LOCK_TIMEOUT` 与卡死原因，不是本方案问题 |
| `ttl-exceeded` | 超 `ACPS_JOB_MAX_RUNTIME` | 确认长任务预算；P1 的根预算要低于它 |
| `probe.ok=false` / 常驻掉线 | **R1 成立**：同 AIC 双登录互挤 | 不要把 root 编排放在 detached runner 里；改进程内 detached 或独立身份 |
| `state` 卡 `running`、日志有 `FileNotFoundError` | R3 | 先修 workspace 前置 |

**决策树**（按结果选 P1 形态）：

```
job done + probe ok ──────────────► 直接复用 jobs（推荐）
job done + 常驻掉线 ──────────────► 进程内 detached asyncio（无重启恢复，演示版）
needs_login / 启不来 ─────────────► 独立 worker 服务 + 新注册身份（成本最高，最后选）
```

## 3. ③ 问迹并发：不是单工作者（待问迹主机实测）

### 代码结论

| 事实 | 位置 | 含义 |
|---|---|---|
| 所有 AIP handler 是 `async`，阻塞流水线经 `asyncio.to_thread` 执行 | `app/acps/capabilities.py`、`bridge.run_in_thread` | **事件循环不被阻塞**，可同时处理多个入站任务 |
| `_task_lock` 只保护计数器（`_active_tasks` 等） | `bridge.py:24` | 不是执行位锁，不序列化任务 |
| `maxActiveTasks: 8` 只是 AMP 指标里的常量 | `bridge.task_metrics` | **不是并发上限**，没有信号量 |
| 每次调用新建 `AgentKernel()/NL2SQLPipeline()/RAGPipeline()` | `bridge.py:101-176` | 没有全局单例锁；并发上限取决于 DB 连接池与 LLM 代理 |
| `serve_direct_partner` 是 wit 的 ASGI 服务（单进程、async） | `app/acps/runtime.py:109` | 单进程 ≠ 单任务 |

→ 计划 §6.2 的"若问迹一直占用唯一执行位置"在当前代码里**不成立**。真正会让入口提交堵住的，
是新入口代码自己的写法（同步 `httpx` 放事件循环、全局锁、或把"等 supervisor 完成"放在 handler 里）。
**"释放执行位置"的对象应改为入口的 HTTP 请求/任务生命周期，不是线程。**

### 仍需实测的原因

代码并发 ≠ 实际吞吐：DB 连接池大小、LLM 代理限流、pgvector 查询竞争都可能让短任务排在长任务后面。

```bash
# 在问迹主机执行（需要问迹自己的身份工作区，含私钥）
python -m acps_cluster.tools.p0_checks wenji \
  --workspace .wit/wenji-aip-agent \
  --peer-aic 1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN \
  --long-question "统计 2010 年每个国家的销售额，按金额降序" \
  --json
```

**判定**：

| 结果 | 含义 | 处置 |
|---|---|---|
| `concurrent` | 短任务（`wenji.schema`）在长任务结束前完成 | 入口方案按"短受理"设计即可，不需要额外释放机制 |
| `serialized` | 短任务在长任务结束后才开始 | 查 DB 连接池/全局锁；入口提交必须先受理再释放 |
| `inconclusive` | 重叠但短任务没先完成 | 资源竞争；给入口提交单独限流，不与子任务抢 |

### 设计建议

- 入口提交用 **async 客户端**（httpx.AsyncClient）+ 5–10s 超时；不要用同步 httpx 放在事件循环里；
- 若要限流，semaphore **只包"出站提交"**，不要包子任务执行（否则又造出单工作者）；
- 入口提交得到的 `root_task_id` 立即落库并返回，后续进度由独立查询路径处理。

## 4. 对计划的逐条修订建议

| 计划位置 | 原文 | 建议 |
|---|---|---|
| §2.2 `jobs_store.py` | "评估根任务映射、幂等受理、结果交付能否复用" | 改为"**直接复用**；扩展幂等索引、业务状态映射、根预算计数" |
| §5.1 能力声明 | 未写可见性 | 三个入口能力全部 `public=False`，且**不设 `acps_skill`**（它才是 ACS 投影开关；设了会换 AIC） |
| §5.3 授权 | 只说"来源验证" | 补：非公开技能 + 双边域章程 + grant（`_evaluate_grant` 已就绪，`quota_path` 默认已配） |
| §6.2 释放执行位置 | "问迹只有一个专业执行工作者" | 修正：问迹是并发服务；要验证的是**入口实现**是否阻塞，以及 DB/LLM 资源竞争 |
| §8 P0 | "核实 SDK 异步受理、平台关单、身份传递及问迹并发机制" | 增加本文三项；把"jobs 同 AIC 登录"列为 P0 阻断项 |
| §8 P1 | "实现提交、查询、幂等、准入、持久化入队" | 不新写 runner；`root_task_id` 可直接用 `job_id`，加幂等索引表 |
| §9 A4 | "入口提交释放执行占用，回派任务能够推进" | 改为"并发共存"：入口提交在途时，回派子任务仍能推进（用本文 ③ 的方法度量） |

## 5. 复现与证据归档

```bash
# ① ACS（本地即可；需 wit 运行时）
python -m acps_cluster.tools.p0_checks acs --json > artifacts/p0/acs-20261117.json

# ② 作业底座（服务器）
python -m acps_cluster.tools.p0_checks jobs --static --json > artifacts/p0/jobs-static.json
python -m acps_cluster.tools.p0_checks jobs --run --yes --timeout 600 \
  --probe-workspace acps_cluster/ws-kb --json > artifacts/p0/jobs-run.json

# ③ 问迹并发（问迹主机）
python -m acps_cluster.tools.p0_checks wenji --workspace .wit/wenji-aip-agent \
  --peer-aic <问迹 AIC> --json > artifacts/p0/wenji-concurrency.json
```

本地已跑：`acs`（结论见 §1）；`jobs --static` 在本机因缺生产配置为 `NOT READY`（预期）；
`jobs --run` 的创建/跟踪/超时逻辑由 `acps_cluster/tests/test_p0_checks.py` 用假 runner 覆盖。

## 6. 边界（如实记录）

- 本报告不能替代服务器验收：平台账号、证书、真实任务库都不在仓内；
- ACS 结论是"skills 投影不变"，平台完整指纹由平台侧计算；**P1 落地时仍要跑一次
  `make_acs.py --dry-run` 对照现网 ACS**；
- `jobs --run` 会真实创建一个后台作业（默认问句只要求回 `p0-ok`），消耗一次模型调用；
- 问迹实测必须在其主机执行（工作区含私钥，不跨机复制）。