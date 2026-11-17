# 多入口编排 P3 设计：回派子任务 + 按 root_id 取证

> 日期：2026-11-17｜状态：**设计稿，未实现。**
> 前置：P1（根任务/入口能力）、P2（域 grant 与问迹接入点）。
> 目标：让"同一根任务"里出现**两个方向各一条真实业务调用**，并能按 `root_id` 自动取证：

```
问迹 ──research.submit──▶ 研枢 Supervisor        （入口调用，平台 taskId T1）
                              │
                              ├──派发 wenji.data_query──▶ 问迹   （回派子任务，平台 taskId T2）
                              │        ◀──分析产物────────┘
                              └──派发其他伙伴（可选，T3…）
```

`T1`、`T2` 是两条**不同的平台 AIP 任务**，caller/callee 互换，由同一个
`root_task_id`（业务根任务）关联。这是比赛口径里"跨智能体双向通信"的完整形态。

## 1. 范围

**做**：

- 根任务上下文（`root_id` + 入口 AIC）注入到后台作业进程；
- 出站委派记账：每次成功委派写一条 `root_dispatches`（子任务号、平台 taskId、目标、技能、终态）；
- 根任务级委派预算（默认 20 次）在执行前拦截；
- "必须回派入口伙伴"的要求（提示词 + 代码侧计数 + 取证检查）；
- `pack --root-id`：按业务根任务自动抓 T1/T2、做关联自检、输出业务链证据。

**不做**（P4/P5 或明确不做）：

- 取消/超时/缺口报告（P4）；
- 结果主动推送（v1 仍是入口查询）；
- 给对端模型暴露 `root_id`（相关性只留在服务端，避免提示注入与越权串联）；
- 自动重试/多 worker 接管（P1 的单 worker 语义不变）。

## 2. 关键设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | 回派是**Leader 正常编排的一部分**，不新增"回派协议" | Supervisor 已有 `acps_delegate`；问迹已是名册成员，能力 `wenji.data_query` 公开可发现 |
| D2 | "必须回派"用**三层**保障：入口简报 → 代码侧计数 → 取证检查 | 只靠提示词不可验证；只靠硬闸会把正常任务卡死 |
| D3 | 委派记账在 **hook 层**（`on_tool_response`），不侵入 `aip_delegate` | 覆盖内置与自研四种委派工具，且不改变 PEP/令牌语义 |
| D4 | `root_id` 通过 **env 传给后台作业进程**，不写进平台任务载荷 | 平台任务里出现业务根号会给对端模型可见的串联线索，也没有协议字段承载 |
| D5 | 预算在 **委派前**检查、成功后计数 | 并发/重试都不会超发；失败委派不占预算 |
| D6 | 证据按 `root_id` 组装，但**仍需要两条平台 taskId 都存在** | 业务关联不能替代平台调用事实；两者都验才算证据 |

## 3. 数据模型（`root_store` 增量）

```sql
-- 新增：根任务下的子任务派发账（一次成功委派一行）
CREATE TABLE IF NOT EXISTS root_dispatches (
    dispatch_id      TEXT PRIMARY KEY,      -- st-<hex>
    root_id          TEXT NOT NULL,
    job_id           TEXT NOT NULL DEFAULT '',
    seq              INTEGER NOT NULL,      -- 根任务内递增（预算/展示）
    target_aic       TEXT NOT NULL,
    skill_id         TEXT NOT NULL DEFAULT '',
    platform_task_id TEXT NOT NULL DEFAULT '',  -- 平台 AIP taskId（来自委派返回值）
    state            TEXT NOT NULL DEFAULT '',  -- 最近一次观测到的状态
    to_entry         INTEGER NOT NULL DEFAULT 0,-- 目标 == 入口 AIC
    workspace        TEXT NOT NULL DEFAULT '',  -- job workspace（任务库所在）
    error            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_root_seq ON root_dispatches(root_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_task     ON root_dispatches(platform_task_id)
    WHERE platform_task_id <> '';
CREATE INDEX        IF NOT EXISTS ix_dispatch_root     ON root_dispatches(root_id);
```

`root_tasks` 不新增冗余计数列；`entry_dispatch_count` / `dispatch_count` 由
`COUNT(*)` 派生（数量小，无性能问题）。

新增 API（`RootStore`）：

```python
def add_dispatch(root_id, *, job_id, target_aic, skill_id, platform_task_id,
                 state="accepted", workspace="") -> Dispatch   # seq 在 BEGIN IMMEDIATE 内自增
def update_dispatch_state(platform_task_id, state, *, error="") -> Dispatch | None
def list_dispatches(root_id) -> list[Dispatch]
def count_dispatches(root_id, *, to_entry_only=False) -> int
```

`status_root` 返回值增加：

```json
{"dispatch_count": 2, "entry_dispatch_count": 1,
 "entry_dispatch_satisfied": true}
```

## 4. 根上下文的传递链

```
research.submit（入站，问迹→研枢）
  └─ root_service.submit_root  ── 创建 root，set _ROOT_CTX=(root_id, entry_aic)
        └─ default_submit(question, mode)
              └─ supervisor.submit_job 工具
                    ├─ env["ACPS_ROOT_ID"]        = root_id
                    ├─ env["ACPS_ROOT_ENTRY_AIC"] = entry_aic
                    └─ Popen(jobs_runner.py)  ← env 继承
                          └─ supervisor.main()（后台 job）
                                └─ build_supervisor_agent 挂 RootDispatchHooks
                                      └─ 每次 acps_delegate* 成功 → root_store.add_dispatch
```

实现要点：

| 位置 | 改动 |
|---|---|
| `runtime/root_service.py` | 新增 `ContextVar` `_ROOT_CTX`；`submit_root` 在调用 `submit_fn` 前后 set/reset；导出 `current_root_context()` |
| `agents/supervisor.py::submit_job` | 读 `current_root_context()`；有值时往 `env` 加 `ACPS_ROOT_ID` / `ACPS_ROOT_ENTRY_AIC` / `ACPS_ROOT_JOB_ID`；`jobs_store.update_status(job_dir, root_id=...)` 便于排查 |
| `runtime/jobs_runner.py` | 无需改：`env` 已继承；只在日志里打印 `root_id` 便于追溯 |
| `agents/supervisor.py::build_supervisor_agent` | `ACPS_ROOT_ID` 存在时挂 `RootDispatchHooks`（常驻进程没有该 env → 不启用） |

> 为什么用 ContextVar 而不是给 `submit_fn` 加参数：`submit_root` 要兼容测试里的
> 两参 fake；ContextVar 还能保证"同一个入口请求只带自己的 root"（不会串到别的 run）。

## 5. 委派记账 hook（`RootDispatchHooks`）

```python
DELEGATE_TOOLS = {"acps_delegate", "acps.delegate",
                  "acps_delegate_public", "acps_delegate_authenticated"}

class RootDispatchHooks(WitHooks):
    id = "yanshu.root-dispatch"
    priority = 30               # 早于 DelegationTargetGateHooks(35)：先做预算拦截

    async def wrap_tool_call(self, ctx, tool_call, handler):
        name = str(getattr(tool_call, "name", "") or "")
        if name not in DELEGATE_TOOLS or not self._active():
            return await handler(tool_call)
        if self._store.count_dispatches(self._root_id) >= self._max_dispatches():
            return {"ok": False, "error": "根任务委派次数已达上限（ACPS_ROOT_MAX_DISPATCHES）"}
        return await handler(tool_call)

    async def on_tool_response(self, event):
        name = tool_name(event)                     # event.tool_call.name
        if name not in DELEGATE_TOOLS or not self._active():
            return
        resp = getattr(event, "response", None)     # wit 的响应外壳：{"status": "success", "result": {...}}
        payload = resp.get("result") if isinstance(resp, dict) and isinstance(resp.get("result"), dict) else resp
        if not isinstance(payload, dict):
            return
        task_ref = next((str(payload[k]) for k in ("task_ref", "taskRef", "task_id") if payload.get(k)), "")
        if not task_ref:
            return
        target = str(payload.get("partner_aic") or args_partner(event) or "")
        skill = str(payload.get("skill_id") or args_skill(event) or "")
        self._store.add_dispatch(
            self._root_id, job_id=self._job_id, target_aic=target, skill_id=skill,
            platform_task_id=task_ref, state=str(payload.get("state") or ""),
            workspace=str(Path(__file__).resolve().parents[1] / "ws-supervisor-job"),
        )
```

要点：

- **只记成功委派**（有 `task_ref`）；预算按成功计数，失败不占额度；
- `to_entry` 由 `target_aic == ACPS_ROOT_ENTRY_AIC` 判定，身份是 AIC 不是名字；
- `resource_call` 不计入（资源节点不是"回派入口"，避免把 GPU 调用误当双向证据）；
- 记账失败只打日志，不阻断 run（账本是取证设施，不能成为编排单点故障）；
- 状态更新（后续 `wait_partner` 到终态）不是 P3 必需；`pack` 直接读平台任务库拿终态。

## 6. 回派要求：三层保障

### 6.1 入口简报（提示词层）

`research.submit` 在把问题交给后台作业前，追加一段**代码生成**的简报（不靠模型自己推断）：

```
【入口回派要求（代码注入）】
本任务由入口伙伴 <entry_aic> 提交。按下列纪律执行：
1. 需要数据查询/文档问答/公式计算时，先把对应子任务回派给 <entry_aic>
   （优先 wenji.data_query / wenji.doc_qa），用 wait_partner 等产物，再做综合；
2. 回派是同一根任务的一部分，不要让对方重新提交整项任务；
3. 除入口伙伴外，其余步骤照常按名册调度。
```

拼装位置：`root_entry.default_submit` 里 `submit_fn(question + brief, mode)`。
简报**不含 root_id**（D4）。

### 6.2 代码侧计数（可观测层）

`status_root` 返回 `entry_dispatch_count`；`research.status` 的结果里带上
`delivery`（见下）。不做"没回派就判失败"的硬闸——模型可能因为任务本身不需要数据
分析而合理地不回派；硬失败会让正常任务变成假失败。

### 6.3 取证检查（证据层）

`pack --root-id` 要求 `entry_dispatch_count ≥ 1` 才算业务链 PASS；否则明确写
"只完成了入口受理，未回派子任务"，不冒充双向业务链。

## 7. 证据包扩展（`pack --root-id`）

### 7.1 输入与解析

```bash
python -m acps_cluster.bidirectional pack \
  --workspace acps_cluster/ws-supervisor \
  --root-id rt-20261117-201500-ab12 \
  --dispatch-workspace acps_cluster/ws-supervisor-job \
  [--peer-workspace acps_cluster/ws-wenji] \
  --out-dir artifacts/bidirectional/<日期>/root-<id>
```

解析顺序：

| 回执 | 来源 |
|---|---|
| T1（入口，问迹→研枢，**inbound**） | `root_tasks.platform_task_id` + `ws-supervisor/acps/aip-tasks.sqlite3` |
| T2（回派，研枢→问迹，**outbound**） | `root_dispatches` 里 `to_entry=1` 的 `platform_task_id` + `--dispatch-workspace` 的任务库 |
| T2 的对端镜像（可选） | `--peer-workspace` 任务库里同 taskId 的 inbound 行 |

**为什么 T2 在 job workspace**：后台作业的 AIP 客户端在 `ws-supervisor-job` 里发任务，
出站行写在那边（P0 已确认独立任务库）；常驻进程的任务库只有 T1。

CLI 兼容：`--root-id` 模式下 `--peer-aic` 可省略（由 `root.entry_aic` 派生；显式传则必须
一致，否则直接拒绝）。`RootStore` 初始化会幂等建新表，老 root 行不需要迁移；老 root 没有
派发账时，`pack` 明确写"无回派记录"，不补造。

### 7.2 新增自检

在现有 8 条（两方向、终态、AIC 对称、对端一致、taskId 不同、时间、产物、无本地错误）之上：

| id | 检查 | 要求 |
|---|---|---|
| `root-linked` | 两条回执都能追溯到同一 root | T1 == `root.platform_task_id`；T2 ∈ `root_dispatches(root)` |
| `entry-caller` | 入口方向确实是入口伙伴发起 | T1.caller == `root.entry_aic` |
| `dispatch-target` | 回派方向确实回给入口伙伴 | T2.callee == `root.entry_aic` |
| `entry-dispatch` | 至少一次回派 | `entry_dispatch_count ≥ 1` |
| `root-completed` | 业务根任务完成 | `root.state == completed` |
| `peer-ledger`（可选） | 对端任务库同 taskId 留痕 | T2 在 `--peer-workspace` 里能找到且 completed |

任一条不满足 → `verdict=incomplete`，逐条写原因；**不把"受理成功"算成"业务链完成"**。

### 7.3 Markdown 结构（新增"业务链"一节）

```markdown
## 业务链（root_task_id = rt-…）
| 环节 | 平台 taskId | caller → callee | 终态 | 产物 |
| 入口：问迹 → 研枢（research.submit） | T1 | 问迹 → 研枢 | completed | 受理回执 |
| 回派：研枢 → 问迹（wenji.data_query） | T2 | 研枢 → 问迹 | completed | 分析产物 |
| 其余派发（可选审计） | T3… | 研枢 → scout | completed | 资料清单 |
```

### 7.4 回执模型

`TaskReceipt` 增加可选字段 `root_task_id`（默认空串，`as_dict`/`from_dict` 兼容），
证据包里每条回执都能自证"属于哪个业务根任务"。

## 8. 时序（含失败分支）

```mermaid
sequenceDiagram
    participant W as 问迹(入口)
    participant S as 研枢-Supervisor
    participant J as 后台作业
    participant P as 其他伙伴
    W->>S: research.submit（带 grant，T1）
    S-->>W: 受理回执（root_id + job_id）
    S->>J: submit_job（env: root_id/entry_aic）
    J->>W: acps_delegate wenji.data_query（T2）
    W-->>J: 分析产物
    J->>P: 检索/写作（可选，T3…）
    P-->>J: 产物
    J->>J: 汇总 + 落 report.md
    W->>S: research.status（带 grant）
    S-->>W: completed + result

    失败分支:
    - J 未回派 → root 仍 completed，但 entry_dispatch_count=0，pack 判 incomplete
    - W 子任务失败 → J 的 wait_partner 拿到 failed，按原委派语义改派/收尾
    - J 崩溃 → jobs_store interrupted → root failed（status 映射）
```

## 9. 验收（P3 增补，对应计划 A1–A12）

| 编号 | 场景 | 观察点 |
|---|---|---|
| A3 | Supervisor 回派问迹 | `root_dispatches` 有 `to_entry=1` 行；T2 在 job 任务库 outbound completed |
| A3b | 问迹执行子任务 | 问迹侧 inbound T2 completed；产物非空；**没有**新的 `research.submit` |
| A12 | 端到端双向 | T1 inbound + T2 outbound、caller/callee 互换、同一 root、`pack --root-id` PASS |
| A5 | 幂等 | 同 request_id 重试仍只有一个 root、一个 T1；T2 不重复派发 |
| A7 | 预算 | 第 21 次委派被 `RootDispatchHooks` 拒绝，root 仍能收尾并保留已完成产物 |

## 10. 实施顺序（估 2–3 人日）

| 步 | 内容 | 退出条件 |
|---|---|---|
| P3-1 | `root_dispatches` 表 + API + 单测 | 台账层单测通过（含 seq 并发唯一） |
| P3-2 | ContextVar + `submit_job` env + `jobs_runner` 日志 | 假 runner 能收到 `ACPS_ROOT_ID` |
| P3-3 | `RootDispatchHooks`（预算 + 记账） | 假 tool response 能落一行；超预算被拒 |
| P3-4 | 入口简报 + `status` 返回 dispatch 计数 | 单测覆盖"有/无回派"两种 status |
| P3-5 | `pack --root-id` + 新检查 + Markdown | 假双向任务库 → PASS；缺回派 → incomplete |
| P3-6 | 真链路联调 | T1/T2 真实 taskId，`pack` PASS，录屏素材 |

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 模型不回派（只调 scout/writer） | 业务链证据缺失 | 入口简报明确要求；演示问句设计成"必须查业务库"；必要时把"回派"写进 `research.submit` 的 `mode=entry-analysis` 固定链路（后续版本） |
| 后台作业与常驻进程并写 root 库 | 锁竞争/SQLITE_BUSY | WAL + `busy_timeout` + `BEGIN IMMEDIATE`；写失败只告警，不阻断 run |
| 委派返回值字段变化（`task_ref`） | 记账漏行 | hook 兼容 `task_ref/taskRef/task_id`；缺失时记 warning 并让 `pack` 判 incomplete（不猜） |
| `root_id` 泄漏给对端模型 | 越权串联 | 只走 env/本地台账；平台载荷不含 root_id；`pack` 的关联在服务端完成 |
| 入口伙伴与资源节点重名/同一 AIC | `to_entry` 误判 | 以 AIC 相等判定；名册里 supervisor 与入口必须不同 AIC（P2 已校验） |
| 旧任务库无 `root_dispatches` 表 | `pack --root-id` 报错 | 先跑一次 `RootStore` 初始化（建表幂等）；老 root 无派发账 → 明确写"无记录"，不补造 |

## 12. 与 P4/P5 的接口

- **P4 根预算**：P3 已把预算检查放在委派前、计数在成功后；P4 把"重试/补充轮次"也计入同一张表即可（加 `kind` 列：dispatch/retry/clarify）。
- **P5 材料**：`pack --root-id` 的输出直接作为"互联交互记录"附在佐证材料；平台后台截图按 T1/T2 两个 taskId 分别截。
- **口径**：P3 完成后才能把比赛材料里的"双向通信"升级为"**同一业务根任务下的双向业务调用**"；在此之前维持"两条互调任务"的表述。

## 13. 明确不做（避免过度设计）

- 不做跨机构根任务接管/迁移（单 Supervisor 是既定取舍）；
- 不做对端主动推送（AIP notification 留待后续）；
- 不做"回派失败自动换伙伴"（现有 `wait_partner` + 改派纪律已覆盖）；
- 不让对端查询 `root_id`（只允许原入口 AIC 通过 `research.status` 查）。