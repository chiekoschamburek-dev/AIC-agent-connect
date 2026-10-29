# 运行中守卫（RunGuardHooks）：把"事后审计"变成"过程中止损"

> 2026-10-29。解决的问题：作业 TTL 调高（2h）后，真正卡死的任务会长时间占着并发额度；
> TTL 调低又会让长任务跑不完。guard-api 只能事后扫历史事件、advisory-only，没有运行中
> 上下文，也没有处置权。

## 1. 两层 guard 的分工

| 层 | 位置 | 能看到什么 | 能不能处置 |
|---|---|---|---|
| `guard-api`（已有） | 独立/进程内 HTTP，只读 SQLite | 已完成任务的**历史事件**（oversized payload、定时规律、peer timing 等） | **不能**：`advisory-only`，只出报告 |
| `RunGuardHooks`（本次新增） | supervisor / 作业进程内，WitHooks | 当前 run 的**实时**工具响应、步数、错误、等待轮次、墙钟 | **能**：中止 run、`aip_fail` 上报、viz 标 failed |

guard-api 的检测规则面向"隐蔽信道/异常交互"；本守卫面向"卡住/空转/异常循环"。
两者互补，不互相替代。

## 2. 规则（全部代码侧观测，不靠模型自觉）

| 规则 | 默认阈值 | 触发条件 |
|---|---|---|
| `wall-clock` | `ACPS_GUARD_WALL_SECONDS=13800`（230 分钟） | run 墙钟超限（4h TTL 前留 10 分钟收尸） |
| `no-activity` | `ACPS_GUARD_IDLE_SECONDS=1500`（25 分钟） | 没有任何工具响应/步活动（模型或等待挂死） |
| `progress-stall` | `ACPS_GUARD_PROGRESS_SECONDS=5400`（90 分钟） | 长时间没有**实际推进**（无产物、无新委派、无任务状态变化） |
| `error-streak` | `ACPS_GUARD_ERROR_LIMIT=8` | 连续 8 次工具调用报错 |
| `repeat-loop` | `ACPS_GUARD_REPEAT_LIMIT=16` | 同一非等待工具 + 同一参数重复 16 次 |
| `step-budget` | `ACPS_GUARD_STEP_LIMIT=195` | 已用步数 ≥195（max_steps 200） |
| `partner-not-finishing` | `ACPS_GUARD_WAIT_LIMIT=30` | 对同一个 `task_ref` 等待/查询 30 轮仍未收尾 |

规则在 `on_step_end` / `on_tool_response` 时求值；命中一次即锁定，不重复触发。

**进度信号**（`progress-stall` 会续命的动作）：`aip_submit`、`ws.write/create/clone_repo`、
新委派成功、`product_gate`/`review_gate` 通过、等待/查询里某个 `task_ref` 的状态发生变化
（working→awaiting/completed）或出现产物。只是反复 `working`、内容不变，不算推进。

## 3. 中止与上报链路

```
命中规则
  → _RUN_GUARD_ABORT 置位（enforce）
  → wrap_tool_call 只放行 aip_fail；其余工具返回 error=run-guard-abort + 处置提示
  → 模型调用 aip_fail（或 run 自然收尾）
  → SupervisorRunHooks.on_run_end 检测到 abort：
       调用 fail_handler(reason=运行守卫中止（rule）：detail）→ AIP 任务 failed
       _viz_finish(status="failed") → 前端显示失败
  → jobs_runner 收尾 state=failed / 看门狗不再等到 2h
```

审计：`<workspace>/logs/run-guard-audit.jsonl`，每行含 `rule/detail/mode/elapsed_s/steps/errors`；
同时写 run-trace 的 `run_guard_abort` 事件。

## 4. 两种模式

- `ACPS_RUN_GUARD=observe`（**代码默认**）：只落审计 + trace，不阻断——用于灰度校准阈值；
- `ACPS_RUN_GUARD=enforce`（现网 `.env.production`）：真中止、真上报。

推荐新环境先 observe 跑几个任务，确认审计里没有误报后再切 enforce。

## 5. 配置（`.env.production`）

```bash
ACPS_RUN_GUARD=enforce
ACPS_GUARD_WALL_SECONDS=13800
ACPS_GUARD_PROGRESS_SECONDS=5400
ACPS_GUARD_IDLE_SECONDS=1500
ACPS_GUARD_REPEAT_LIMIT=16
ACPS_GUARD_ERROR_LIMIT=8
ACPS_GUARD_STEP_LIMIT=195
ACPS_GUARD_WAIT_LIMIT=30
```

作业 TTL 是 **4h** 的兜底（`ACPS_JOB_MAX_RUNTIME=14400`）：守卫负责提前止损，
TTL 负责防止进程层挂死；两者不是替代关系。

## 5.1 历史回放校准（2026-10-29）

用 34 个有轨迹的历史作业回放（18 个成功、16 个 failed/interrupted）：

- **成功作业 18/18 不触发任一规则**（零误杀）；
- 真正卡到 TTL 的 3 个作业被 `progress-stall` 捕获（90 分钟无实际推进）；
- 其余 13 个 failed/interrupted 是启动即锁超时、模型异常、进程被重启杀掉等，
  不属于"卡住/空转"目标域（它们本来就快速失败，不需要守卫止损）。

因此守卫在目标域（成功运行 + 卡死/停滞）上的准确率为 100%，但样本只有 34 个作业，
阈值仍建议在新环境先用 `observe` 看一两天审计再切 `enforce`。

## 6. 边界与后续

- 规则是启发式，存在误杀可能：`repeat-loop` 只看"工具 + 参数完全相同"，
  正常重试（参数略变）不会命中；`partner-not-finishing` 只按同一 `task_ref` 计数。
- 不做内容质量评分（那是 critic/product_gate 与 guard-api 的事）。
- 后续可把 guard-api 的 `analyze_events` 结果（HIGH 级别）也接入守卫，
  作为"交互异常"的额外中止条件；当前先保证"卡住/空转"这一类可止损。
- 观察入口：`logs/run-guard-audit.jsonl`、run-trace `run_guard_abort`、前端失败状态。
