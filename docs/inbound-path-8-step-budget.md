# 入站路径修复：wit 给入站 AIP 的 8 步预算

> 2026-10-09 · 起因：「入站 8 步预算太少 + 入站路径没有我方工具（wait_partner/plan_gate/product_gate）」

## 1. 两个问题的定位结论

### 问题一：入站 8 步预算（**真问题，已修**）

wit 的 `WitRuntime` 处理**入站 AIP 请求**（平台/叮当 → `:8444/rpc`）时，**不看我们传入的 `RunConfig`**，
而是自己构造一份：

```python
# wit 2.1.0 · kernel/runtime.pyi
async def _run_with_scoped_tools(
    self, input: RunInput, *, tools: list[Tool],
    metadata: dict[str, Any] | None = None,
    max_steps: int = 8,          # ← 入站默认只有 8 步
    ...
) -> RunResult
```

反汇编确认：该方法内部 `RunConfig(max_steps=max_steps, tools=tools, …)` → 交给 `_run_core`。

**8 步为什么不够**：leader 的完整链路是「签发委托令牌 → 委派 → 等伙伴 → 取产物 → product_gate 体检」，
而**等一个伙伴至少要 2–3 步**（`acps_task(wait)` 轮询一次 45s 超时 = 1 步）。
多伙伴顺序链路（检索→精读→矛盾→写作→审稿）必然撞顶，表现为**任务半途收尾**。

> 现场证据：`ws-supervisor/outbox/deepresearch-v10.md`（收件箱路径）实测 `steps=9`；
> 而 2026-10-09 曾有一次入站任务 `task_7f2e3b42d39b` 在"等 scout"阶段就烧光 8 步。

### 问题二：入站路径没有我方工具（**已修，且当晚才生效**）

`SupervisorRunHooks.on_run_start`（`supervisor.py`）会把 `build_run_config()` 的整套工具补进当次 run。
但**旧版**实现有个提前返回：

```python
tools = getattr(cfg, "tools", None)
if not isinstance(tools, list):
    return                      # ← 入站 cfg.tools 是空列表 []，能通过
```

空列表能通过 `isinstance` 检查，所以逻辑本身没错；**真正的问题是这份修复在服务器上还没生效**
（服务器跑的是旧 `supervisor.py`，2026-10-09 22:49 supervisor 重启后才加载新版）。

补挂后的工具集（实测）：`plan_gate` / `review_gate` / `product_gate` / `deep_research` /
`auto_research` / `wait_partner` / `wait_many`（7 个，按名去重，避免 wit 的
`Duplicate model tool name`）。

## 2. 修复内容

`supervisor.py` 的 `SupervisorRunHooks.on_run_start`：

1. **工具补挂**：保留原逻辑（空 `tools` 列表时补全整套）；
2. **预算抬升**（新增）：把入站 `config.max_steps` 抬到与出站同量级，
   `ACPS_SUPERVISOR_INBOUND_MIN_STEPS`（默认取 `ACPS_SUPERVISOR_MAX_STEPS`，即 200）。
   - **只抬升、不下调**：调用方要求更大预算时尊重其取值；
   - `RunConfig` 是 frozen dataclass，用 `object.__setattr__` 改这一个数值字段；
   - 每次抬升都在日志里留一行，便于事后核对。

### 为什么改得到？

反汇编确认 `_run_core` 把**同一个 `config` 对象**同时用于：
- 构造 hook 上下文（`self._hook_context(..., config=config, ...)`）→ 我们的 hook 能拿到它；
- 读取步数上限（`config.max_steps`）→ 在 hook 里改它就改了真实上限。

所以这是**官方扩展点内的修法**，不需要 patch wit。

## 3. 验证

`acps_cluster/tests/test_supervisor_inbound_budget.py`（不需要模型 provider，可离线跑）：

| 用例 | 期望 | 实测 |
|---|---|---|
| 入站默认 `RunConfig(max_steps=8)` | 抬到 200 | ✔ 200 |
| 补挂工具 | 含 7 个本地工具 | ✔ 7 个齐全 |
| 调用方要求 500 步 | 不下调 | ✔ 500 |
| 已有全套工具（7 个） | 不重复补 | ✔ 无重名 |
| `ACPS_SUPERVISOR_INBOUND_MIN_STEPS=77` | 生效 | ✔ 77 |

本地与服务器**均全绿**。

## 4. 部署状态

- `supervisor.py` 已部署（备份 `supervisor.py.pre-inboundfix-20261009-234806`）；
- supervisor 已重启，`active` + 8444 监听；
- **ACS 投影未变动**（这次只改运行期 hook 逻辑，不动描述/能力声明）→ 不触发指纹保护，实测重启正常。

## 5. 仍需一次真实验证（待办）

以上是**代码级 + 单测级**确认。要闭环还需**一次真实入站请求**，然后看 supervisor 日志里是否出现：

```
[supervisor] 入站步数预算 8 → 200（入站 AIP 默认 8 步不足以完成多伙伴链路）
```

**建议**：从叮当发一句**最小请求**（例如「列出当前集群有哪些伙伴」），我核对日志；
确认后再发真实科研任务。这样既验证了入站路径，也避免一上来就烧掉一整条链路的额度。

## 6. 顺带发现的一处文案不一致（未改，因为会改 ACS）

`supervisor.py` 里 agent 描述写着「单次任务上限 200 步、单次工具调用超时 420 秒」，
这句**原本只对出站/收件箱路径成立**（入站曾被压到 8 步）。本次修复后它才真正对两条路径都成立——
所以**这句文案现在反而是准的**，无需改动，也就不会因此触发重新注册。
