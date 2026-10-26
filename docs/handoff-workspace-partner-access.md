# Handoff：不建域给 partner 开放 workspace 读写（2026-10-26）

> 状态：**已实现、本地全绿、服务器已部署并通过端到端联调（2026-10-26）**。
> 剩余项见 §0.4。本文同时是设计说明与交接材料。
> 目标：让 scout / experiment / synthesis 等 partner 能在同一个 task_id 下
> `ws.list` / `ws.read` / `ws.write` workspace，用于看 GitHub 克隆仓库、交换中间产物；
> **不建协作域、不引入 grant、不放松 `act == peer` 绑定**。

---

## 0. 进度（2026-10-26 晚更新）

### 0.1 已完成

| 项 | 落点 | 验证 |
|---|---|---|
| §4.1 委派 request 形状 bug | `supervisor.ResourceDispatchAuthHooks.wrap_tool_call` 不再把 `Part` 列表塞回内置 `acps_delegate`，改为代码侧 `runtime.aip_delegate` 直发；`acps_delegate_authenticated` 只走它自己的 handler；非受保护伙伴无 bundle 时原样透传 | `tests/test_resource_dispatch_auth.py`（含"handler 不得被调用"回归） |
| §4.2 partner 令牌 bundle | `delegation/bridge.py` 新增 `current_delegation_bundle/set_delegation_bundle`；`delegation/client.py` 的 `DelegationTokenScrubHooks` 拆出主令牌选择与 ws bundle 收集，bundle 落 host 文件（只读不消费） | `tests/test_delegation.py::TokenBundleTest` |
| §4.2 partner 客户端 | 新模块 `partner_workspace_client.py`：`ws_list/ws_read/ws_write` 三个模型工具（`acps_skill=False`）＋ `WorkspaceAccessBudgetHooks`（抬到 ≥16 步）；task_id 只从令牌 claim 取 | `tests/test_partner_workspace_client.py` |
| §4.3 supervisor 签 partner 令牌 | `supervisor._ws_bundle_parts()`：按 `workspace_access.access_keys()` 为伙伴签 `ws.list/ws.read/ws.write` 三枚（`delegate_aic=partner`、`target_aic=workspace`、`task_id` claim、TTL≤600s），随委派 DataItem 下发；公开委派与受保护委派两条路径都附 | `tests/test_resource_dispatch_auth.py::WorkspaceBundleDispatchTest` |
| 多令牌共存 | PEP 取凭据改为 `token.extract_token_for_audience()`（只取 aud==本机；外发 ws 令牌忽略；malformed 仍报 invalid-token）；`client._primary_of()` 不让 ws 令牌成为主令牌 | `tests/test_delegation.py::TokenAudienceSelectionTest` |
| ACS 中性 | ws 客户端工具带 `metadata.internal_tool`：`skill_scope.non_public_ids()` 与 `discovery_metadata.apply_to_capabilities()` 都跳过（后者曾按 `acps_skill` 判，会关掉全部门面增强——已改回按 internal_tool） | `tests/test_partner_workspace_acs.py` |
| 七个研究伙伴装配 | scout / experiment / synthesis / writer / critic / contradiction / frontier 全部挂 `AipDelegationHooks + BridgeHooks + WorkspaceAccessBudgetHooks`，指令追加 `WORKSPACE_ACCESS_NOTE`；开关 `ACPS_WS_PARTNER_ACCESS`（`workspace_access.py`） | 同上 |
| §4.4 产物归档 | **选定"完全取消自动镜像"**：删除 `_mirror_products`/`_load_outbound_product_rows` 与 `ACPS_WS_MIRROR` 通道，supervisor 不再本地直写 `products/`；partner 用 `ws_write` 写自己的产物 | `tests/test_workspace_partner.py` 仍覆盖服务端分区规则 |
| **联调新增修复 ①**：workspace 误判作用域 | 服务器 e2e 实测 workspace 把 AIP 任务号（`task-<uuid>`）当成"leader 指定的 task_id"，拒写并回 `reject-illegal-task-id`。`partner_workspace` 的指令已明确"协议 taskId 不是工作区作用域" | e2e：`shared/e2e-note-b.md` 写入成功 |
| **联调新增修复 ②**：伙伴 discovery 缓存为空 | 七个研究伙伴的 `acps/discovered/` 本来就是空的（只有 supervisor 跑 seed），直连 workspace 报 `Partner has no discovered Direct JSON-RPC endpoint`。新增 `cluster_common.seed_workspace_peer()`，伙伴启动时把 workspace 的 ACS 预热进自己的缓存 | `tests/test_partner_workspace_acs.py::WorkspacePeerSeedTest` |
| **联调新增修复 ③**：`aip_task(action="get")` 不被支持 | wit 只接受 `refresh/continue/complete/cancel`，`get` 抛 `AipError: Unsupported task action`，三个 ws 工具全部失败。改为 `refresh` | `tests/test_partner_workspace_client.py::test_等待用refresh再complete` |
| **联调新增修复 ④**：兜底产物缺 `id` | `InboundProductFallbackHooks` 兜底产物没有 `id` → `AipError: Invalid AIP product`，任务停在 awaiting-completion 且拿不到产物。补 `fallback-<uuid12>` | `tests/test_inbound_product_fallback.py` 断言 id 非空 |

### 0.2 本次采用的 §8 决策

1. 需要工作区访问的伙伴：**全部七个研究伙伴**（默认开启，可用 `ACPS_WS_PARTNER_ACCESS` 收窄或置空关闭）；
2. 权限范围：**读写**（`ws.list` + `ws.read` + `ws.write` 三枚；repo 只读、scratch 强制前缀、products 版本化都由服务端强制）；
3. supervisor 最终产物：**不再自动归档**（AIP products 仍是交付面；需要落工作区时由编排显式委派 `ws.write`）；
4. 多 skill：**token bundle**，不改 PEP 的 `act==peer`、不改 workspace ACS 指纹。

### 0.3 服务器联调与部署（2026-10-26 17:3x，已完成）

- **部署**：supervisor + 七个研究伙伴 + workspace 全量重启；workspace 的 PEP/ACS 未改、
  指纹未触发保护。文件清单、备份目录、逐轮回滚命令见
  [server-rollout-2026-10-26.md](server-rollout-2026-10-26.md)；
- **E2E（作用域 `e2e-1026-b`）**：
  - supervisor 两次委派 workspace（`ws.create`、`ws.write`）均走 `delegate_direct`，
    作用域 `task_scope=e2e-1026-b`，落盘 `shared/e2e-note-b.md`；
  - supervisor 委派 scout 时 `ws_bundle_issued` 签发三枚令牌，
    `delegate_direct` 的 `ws_bundle=["ws.list","ws.read","ws.write"]`、`parts=4`；
  - scout 实际用 `ws_list` 找到文件、`ws_read` 读到 `联调标记 E2E-1026-B`、
    `ws_write` 写出 `products/scout/0001-e2e-note-b.md`；
  - workspace manifest：`{"action":"write","actor":"scout","path":"products/scout/0001-e2e-note-b.md",...}`；
  - workspace PEP 审计：`decision=allow rule=delegation-token caller=<scout AIC> skill=ws.read/ws.write`
    ——`act==peer` 绑定生效；
  - 部署后 supervisor 不再产生 `workspace_mirror` 事件（当日 5 条全在部署前），
    `e2e-1026-b` 目录里没有 supervisor 本地直写产物；
  - 七个伙伴的 discovery 缓存均已包含 workspace AIC。

### 0.4 仍未做（明确留待后续）

- supervisor **自身**最终产物自动落 `products/supervisor/`：需按 §4.4 备选实现
  （`act=supervisor` 令牌调 `ws.write`）；当前由编排显式委派，未做自动归档；
- 已知限制 1：从 supervisor 委派 workspace 时**一次委派只签一个 `ws.*` 能力**
  （`_skill_for` 取文本中最早出现的受保护能力）。提示词已改为"ws.create 与
  ws.clone_repo 拆成两次委派"；一次多能力需让 `_ws_bundle_parts` 支持自指 +
  PEP 按 skill 选 token；
- 已知限制 2：partner 令牌 TTL ≤600s，长任务（>10min）在末尾调用 `ws_*` 会拿到
  "令牌已过期"的 awaiting，需要重新委派；若实际链路常超时，可考虑续签（未做）；
- 已知限制 3：bundle 文件按任务落 host 目录、只读不删，过期条目读取时过滤，
  但没有清理任务（磁盘占用很小，可择机加 timer）。

---

## 1. 一句话结论

workspace 现在事实上是 **supervisor 专用**：
- 模型发起的 `ws.*` 委派没有到达 partner（request 形状 bug，见 §2.1）；
- supervisor 收尾又直接用本地函数 `archive_products()` 写 workspace（旁路，见 §2.2）；
- partner 没有 workspace token，也没有 workspace 客户端工具。

因此 clone 下来的 repo、intermediate products 对 partner 不可见，"文件总线"没有闭环。
不建域的可行修法是：**supervisor 给 partner 签发 `act=partner` 的 ws.* delegation token**，
partner 代码侧持 token 调 workspace；workspace PEP 只按现有规则校验
`issuer=supervisor`、`aud=workspace`、`act==peer`、skill、task_id，不需要域。
这个方案已经在现有 PEP/issuer 代码里具备基础，缺的是接线和 partner 侧客户端。

---

## 2. 两个真实缺陷（有现场证据；均已修复）

### 2.1 模型发起的 `ws.*` 委派失败，实际没到 partner

现场 run trace（服务器 `ws-supervisor/logs/run-trace/run-trace-20261026.jsonl`）：

```text
tool_request  acps_delegate_authenticated request="..."（string）
delegate_auth_attached partner_aic=…N9M0PW… skill_id=ws.create task_scope=task-1026-e2e
tool_response error: request must be of type 'string', got list
...
tool_request  acps_delegate request=[{'type':'text',...}]
tool_response error: Invalid high-level content input
```

原因在 `acps_cluster/supervisor.py` 的 `ResourceDispatchAuthHooks.wrap_tool_call`
（约 3279–3360 行）：

```python
text = as_list(args.get("request"))
parts = []
if isinstance(text, str) and text.strip():
    parts.append(TextPart(text=text))
elif isinstance(text, (list, tuple)):
    parts.extend(text)
parts.append(DataPart(...))
args = {**args, "request": parts, "partner_aic": partner_aic}
```

它把 `TextPart/DataPart` **对象列表**塞回内置 `acps_delegate` 的参数。
内置工具的内容校验走 wire-format（dict / string），因此：
- `acps_delegate_authenticated` 顶层路径报 `request must be string, got list`；
- `acps_delegate` 报 `Invalid high-level content input`。

workspace 服务日志只有 skill-scope 收窄，没有成功的 `ws.create/ws.write`；
workspace DB 里也没有 `task-1026-e2e`。

**修复方向（已完成，见 §0.1）**：受保护伙伴不要在 hook 里改 args 后交给内置工具；在 hook 内
直接用 `runtime.aip_delegate(state, partner_aic=..., request=[TextPart, DataPart])`
发起（这正是 `acps_delegate_public` 和原 authenticated handler 的可靠路径）。
非受保护伙伴仍原样透传。修完加一条 e2e：workspace 侧 DB 必须出现该 task。

### 2.2 supervisor 直接写 workspace，绕过 partner

`acps_cluster/supervisor.py:1841` 的 `_mirror_products()`：

```python
import partner_workspace as pws
summary = pws.archive_products(entries, task_key)
```

`acps_cluster/partner_workspace.py:689` 的 `archive_products()` 直接调
`ws_create/ws_write` 本地函数，写 supervisor 所在机器的
`ws-workspace/workspaces/<task>/products/`。

现场证据：`task-1026-e2e/products/supervisor/0001...`、`manifest.jsonl` 存在，
但 workspace DB 无这个 task；workspace 服务没有收到对应的 ws.write。
这解释了用户观察到的“写入回传到 supervisor”。

**修复方向（已完成：取"删除本地直写"的形态，见 §0.2 / §4.4）**：
- 推荐：删除 supervisor 本地直写，supervisor 使用自己的 token 调 workspace
  的 `ws.write`（actor=supervisor）；partner 自己的产物由 partner 用 §3 的 token 写。
- 或：暂时 `ACPS_WS_MIRROR=0` 关闭自动镜像，只保留显式协议写入。
- 不建议保留“本地 import partner_workspace”的旁路。

---

## 3. 目标权限模型（无域）

### 3.1 信任链

```text
workspace PEP:
  issuer ∈ ACPS_PEP_LEADER_AICS（supervisor）   # 已经配置
  aud == workspace AIC
  act.aic == request.leader_aic（调用方本人）
  acps_skill_id == 请求 skill
  task_id claim == 请求 task_id

supervisor:
  签 token 时 delegate_aic=partner AIC
  target_aic=workspace AIC
  skill_id=ws.list / ws.read / ws.write
  task_id=<任务作用域>
```

不需要 domain / grant / parent_jti。现有 `build_issue_token_tool` 已支持
`delegate_aic=partner`、`target_aic=workspace`、`skill_id`、`task_id`；
workspace PEP 的 `leader_aics` 已包含 supervisor，`act==peer` 检查在
`delegation/pep.py` 约 947 行。

### 3.2 token 交付与 partner 侧客户端

- supervisor 在把任务委派给 partner 时，为 partner 需要的每个 ws.* skill
  各签一枚 token（read/list/write 三枚），放进委派 DataItem；
- partner 的 `DelegationTokenScrubHooks`/BridgeHooks 目前只保存**单枚**
  delegation，需要扩展为 **token bundle**（按 skill_id 索引）；token 仍不得进入
  模型上下文；
- partner 增加代码级 workspace 客户端工具：
  - `ws_list(task_id, path, depth)`
  - `ws_read(task_id, path, max_bytes)`
  - `ws_write(task_id, path, content, encoding, if_match)`
  工具内部从 token bundle 取对应 token，调用 workspace endpoint；模型只拿到
  路径/摘要/树，不拿 token。task_id 建议从 token claim 固定，不让模型自由改。
- 若不想改所有 partner，可先只给真正需要看仓库的 partner（scout / experiment /
  synthesis）加；其他保持只读结果。

### 3.3 为什么不用 grant/domain

- GPU/CPU 已经用域 grant 演示资源方签发；workspace 与 partner 同机、目标是
  内部文件总线，不需要再引入域章程；
- grant 的 `sub == request.leader_aic` 规则意味着 grant 只能由 supervisor 自己用，
  要让 partner 用还是得派生 child token；域/配额/成员表成本高于收益；
- 但**不要把 PEP 改成“只要 supervisor 签就放行”**：那会丢失 presenter 绑定。
  保留 `act==peer`，supervisor 签发 `act=partner` 的 token 即可，既不建域也不弱化安全。

---

## 4. 代码改动清单（已全部落地，见 §0.1；保留作设计说明）

### 4.1 先修委派 request（阻塞项，已完成）

文件：`acps_cluster/supervisor.py`，`ResourceDispatchAuthHooks.wrap_tool_call`
（约 3279–3360）。

- 解析 `partner_aic` / `skill_id` / `task_scope` 不变；
- 受保护伙伴：**不要构造 `new_call` 交给内置 `acps_delegate`**，改为直接：
  ```python
  content = [TextPart(text=request_text), DataPart(token...)]
  snap = await run.runtime.aip_delegate(run.state, partner_aic=partner_aic, request=content)
  return snap
  ```
  `request_text` 支持 string 和 list[dict]/list[TextPart]；list[dict] 转成
  `TextPart`，不要把 dict 原样塞进 `aip_delegate`。
- 非受保护伙伴保持现有透传。
- 顶层 run 已通过 `SupervisorRunHooks.on_run_start` 摘除 `acps_delegate_authenticated`；
  如果模型仍选它，hook 兼容即可，但不要再依赖它。
- 加回归测试：受保护伙伴的 hook 调用不会触发内置工具的 `ToolArgumentError`，
  并且实际发出包含 token DataPart 的请求。

### 4.2 partner token bundle / 客户端（已完成）

- 先读 `acps_cluster/delegation/client.py` 的 `DelegationTokenScrubHooks`
  与 `bridge.py`，确认当前单 token ContextVar 的存储位置；
- 扩展为 `dict[skill_id] -> {token,targetAic,...}`；或新 ContextVar；
- 新增共享模块（例如 `partner_workspace_client.py`）供 partner 使用；
  token 从 bundle 取，不显示给模型；
- 客户端调用 workspace 的 JSON-RPC/mTLS endpoint，复用 `aip_delegate` 或
  `direct_fetch`；错误按 `scope-mismatch` / `no-such-file` 等原样回传；
- 不要把 `partner_workspace` **import 到 partner 本地**执行文件操作（会重新走旁路）。

### 4.3 supervisor 签发 partner token（已完成）

- 在 supervisor→partner 的委派点（可在 `wrap_tool_call` 或新增 helper）：
  ```python
  issue.handler({
    "delegate_aic": partner_aic,          # act=partner
    "target_aic": workspace_aic,          # aud=workspace
    "skill_id": "ws.read",                # 每个 skill 一枚
    "task_id": task_scope,
  })
  ```
- token 从 vault 取出后用 `token_data_item` 放进委派 DataItem；
- 选择 partner 需要的 skills（建议 read/list/write 三枚），token 不进入模型文本；
- 如果一次委派无法携带多枚 DataItem，拆成多次或扩展内容组装；
  不要把多个 token 拼成一个字符串。

### 4.4 产物归档（已完成：取消自动镜像）

- 删除 `supervisor.py:_mirror_products` 里的 `import partner_workspace` 和
  `archive_products` 本地调用；
- 如仍要自动归档 supervisor 自己的最终产物：supervisor 用 `act=supervisor`
  token 调 workspace `ws.write`，actor=supervisor；
- partner 自己的 intermediate products：用 partner 的 `act=partner` token 写
  `products/<partner>/...` 或 `scratch/<partner>/...`；
- 若暂不自动归档，明确设置 `ACPS_WS_MIRROR=0` 并在文档标注。

### 4.5 不要改的东西

- **不要**把 workspace PEP 改成“只校验 issuer=supervisor，不管 act/peer”；
- **不要**给 ws.* 加域/visibility=domain（会改 ACS 指纹，需要新版本/AIC）；
- **不要**在 supervisor 里 import `partner_workspace` 做文件 I/O；
- **不要**把 token 返回给模型或写进 run trace。

---

## 5. 能力与限制（重要）

- `ws.read`：默认 64KB，能力层上限 1MB，**没有 offset/分块**；大文件只读开头。
- `ws.list`：≤200 项，深度上限 8；GitHub 仓库只能按需探索，不是一次全量。
- `ws.write`：products 追加版本化；scratch 强制 `scratch/<actor>/`；shared 支持
  `if_match`；repo 只读（clone 专用）。
- `ws.clone_repo` 目前 supervisor 可委派；partner 要读 clone 结果，必须拿到
  `ws.list/ws.read` 的 token；仓库很大时，建议 partner 只读关键文件，或在
  GPU/CPU 沙箱里跑扫描器，不要把全部文件灌进模型。
- partner 的 actor 由 token `act.aic` 派生（`partner_workspace._caller_actor`），
  所以 token 必须 `delegate_aic=partner`，不能复用 supervisor token，否则所有
  products/scratch 都归到 supervisor。

---

## 6. 测试与验收

1. **hook 回归**：受保护 partner + string request 不再出现
   `request must be of type 'string'` / `Invalid high-level content input`。
2. **token 绑定**：supervisor 签 `act=partner` token，workspace PEP 通过；
   换另一个 agent 出示同一 token 必须 `act-peer-mismatch`。
3. **E2E**：
   - supervisor 委派 `ws.clone_repo` 建 repo；
   - supervisor 给 scout/experiment 发 `ws.read/ws.list` token bundle；
   - partner 通过 workspace 客户端读到 `repo/<name>/README.md` 或指定文件；
   - workspace DB 出现 partner 的任务，manifest actor=partner；
   - workspace 目录里不再出现 supervisor 本地直写的文件。
4. **越权**：partner 用错 task_id / 用别的 partner 的 token / 用 supervisor token
   伪造成 partner，均应被 workspace PEP 拒绝。
5. **不影响公开链路**：scout/writer 等非 workspace 委派保持原行为；全量测试通过。

---

## 7. 部署/回滚

当前服务器状态（交接时）：

- workspace AIC `1.2.156.3088.1.BUPT.VTF97R.N9M0PW.ZB1CII.0B3X`，v1.1.0；
- workspace 服务 active，8470；mirror/sender active；11/11 agent `alive=true`；
- 最新本地 commit `f1897eb`（mirror/sender），workspace 优化已在服务器 active；
- 备份：
  - `/opt/wit/predeploy-workspace-20261025-223001`
  - `/opt/wit/predeploy-workspace-v11-233759`
  - `/opt/wit/predeploy-mirror-sender-20261026-113751`

部署顺序建议：
1. 本地改 supervisor hook + tests，跑全量；
2. 改 partner token bundle/client，先在本地单测；
3. 服务器停 supervisor → 备份当前 supervisor.py → scp → py_compile/单测 → 启动；
4. workspace 不需要改 PEP/ACS；如改了 partner_workspace.py，先按 runbook 验证
   ACS 指纹（`verify_overlay_matches_registered.py` / `verify_projection_stability.py`），
   再重启 workspace；
5. 逐个重启需要 workspace 客户端的 partner，先做 scout/experiment 灰度；
6. E2E 验收；保留回滚：恢复 supervisor.py / partner 代码并从备份目录重启。

注意：workspace 的 ACS/1.1.0 指纹已与平台注册版对齐，**本轮不要添加/改 ws.*
能力声明**；新增 partner 客户端不要改 workspace ACS。

---

## 8. 待用户/下一 agent 决策

1. 哪些 partner 需要 workspace 访问：scout / experiment / synthesis / 全部？
2. 权限范围：只读（ws.list/ws.read）还是读写（含 scratch/shared/products）？
3. supervisor 最终产物是否还自动归档；如果归档，用 supervisor token（actor=supervisor）
   还是完全取消自动镜像？
4. 多 skill token：先做 token bundle（推荐，不改 PEP/ACS），还是以后加一个
   `ws.access` 内部作用域能力（会改 ACS/版本）？

---

## 9. 关键文件索引

- `acps_cluster/supervisor.py`
  - `_mirror_products` 约 1841（直写旁路）
  - `ResourceDispatchAuthHooks.wrap_tool_call` 约 3279–3360（request 形状 bug）
  - `_protected_partners` 约 3470（workspace 的六个 skill）
- `acps_cluster/partner_workspace.py`
  - `_caller_actor` 约 123（act.aic → actor）
  - `_scope_check` 约 148（task_id claim）
  - `archive_products` 约 689（supervisor 本地直写入口）
  - `ws_write_cap` / `ws_read_cap` / `ws_list_cap` 约 743 / 767 / 789
- `acps_cluster/delegation/issuer_tool.py`
  - `delegate_aic` / `target_aic` / `skill_id` / `task_id` 签发；无需 domain。
- `acps_cluster/delegation/pep.py`
  - `allowed_issuers = leader_aics | domain.issuers` 约 904
  - `act.aic != request.leader_aic` 拒绝 约 947
  - grant 直接路径的 `sub == request.leader_aic` 约 1188（说明为何不建域）
- `acps_cluster/delegation/bridge.py`
  - `current_delegation` / `delegated_fetch`（partner 持 token 出站的基础）
- 现场 trace：`/opt/wit/my-project/acps_cluster/ws-supervisor/logs/run-trace/run-trace-20261026.jsonl`
- 现场 workspace DB：`/opt/wit/my-project/acps_cluster/ws-workspace/acps/aip-tasks.sqlite3`
