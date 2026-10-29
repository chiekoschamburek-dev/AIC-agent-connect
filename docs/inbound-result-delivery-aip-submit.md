# 入站任务结果交付：aip_submit 缺失（2026-10-10）

> 现象：叮当发起的科研任务在服务器侧**实际跑完了**（67 步、完整综述），但用户侧**看不到结果**。

## 1. 现场证据（全部来自落盘数据，非推测）

| 事实 | 数据 |
|---|---|
| leader 侧运行 | `RUN END status=completed iterations=67`（04:10–05:40，约 87 分钟） |
| 最终产出 | 6809 字符完整综述（《扩散生成模型的技术谱系与四条相对独立的改造轴》），`product_gate ok=True` |
| 我方任务里的产物 | `products` 1 个，text 6809 字符 —— **产物是在的** |
| 任务状态 | **停在 `awaiting-completion`**，从未进入 `completed` |
| 对端（叮当）行为 | 发来 `start` **1 条** + `get` **2365 条**（每 2.3 秒轮询一次），04:10:24 → 05:40:26 |
| 通知机制 | notice 相关 5 张表全 0 行（未启用，对端靠轮询） |
| **`aip_submit` 调用次数** | **0** ← 决定性证据 |

工具使用统计（该次 run）：`acps_task` 28、`wait_partner` 15、`product_gate` 12、
`acps_delegate` 11、`wait_many` 8、`plan_gate` 1、`review_gate` 1、**`aip_submit` 0**。

## 2. 根因

`aip_submit` 是 wit 给**入站伙伴**自动提供的内置工具：

```
aip_submit  —  Submit one or more protocol-valid AIP Products for the current task.
aip_request_input — Pause the current AIP task and request more input from its Leader.
aip_fail    —  Fail the current AIP task with a safe reason.
```

**产物必须经 `aip_submit` 正式提交，任务才会离开 `awaiting-completion`。**
而我们的 playbook 里**完全没有这条要求**——leader 把总结写进最终回复就结束了，
于是：产物停留在任务里、状态不推进、调用方轮询 90 分钟后放弃。

契约（`wit.acps.aip`）：

```python
Product       = { "id": str, "name": str|None, "description": str|None, "data_items": [DataItem] }
TextDataItem  = { "type": "text", "text": str }
# aip_submit 入参：{ "products": [Product, ...] }   （array, minItems=1）
```

## 3. 修复（两层保险）

### ① 纪律（`_INBOUND_INTAKE` 新增一节「入站任务必须提交产物」）

明确要求：任务做完后**必须**调 `aip_submit`，给出完整调用示例，并写明
"只把总结写进最终回复而不调用 `aip_submit`，等于没交付"；失败则用 `aip_fail` 如实上报。

### ② 代码兜底（`SupervisorRunHooks`）

`on_run_start` 记下入站执行器的 `aip_submit` handler；`on_tool_request` 记录模型是否提交过；
`on_run_end` 时若**模型没提交且 run 正常结束且有文本** → 用最终文本自动提交一次
（内容前缀标注"由研枢-Supervisor 在任务收尾时自动提交"，便于区分）。

**踩坑**：`Tool` 是 **frozen dataclass**，不能给 `handler` 赋值
（单测实测 `FrozenInstanceError: cannot assign to field 'handler'`）。
第一版实现想"包住 handler"，结果包装失败 → 检测不到模型已提交 → **会重复提交**（单测用例 1 抓到 2 次）。
改为用 `on_tool_request` 事件检测后，用例全绿。

单测 `tests/test_inbound_product_submit.py` 覆盖 6 种情形：
已提交不重复 / 未提交自动兜底且内容完整 / 失败不提交 / 空文本不提交 /
非入站 run 无 `aip_submit` 工具时不报错 / 模型提交抛错时兜底仍尝试（用正确字段结构重试）。

## 6. 补充：模型调用卡死的**第二层**硬闸（2026-10-10 下午）

`agents._with_response_deadline`（provider 层包装）**已生效但仍不够**：实测伙伴进程
CPU 0.0%、主线程 `select()` 等 I/O、入站任务卡 **9 小时**，连接停在 MiniMax 的 IP 上。
卡点在 **SDK 发出请求 → 首个响应到达之前** 这段（`docs/` 记录的第一层闸只管
`provider.stream/complete` 的产出节奏），而 httpx 的 timeout 是**读间隔**不是总时长。

因此把总闸打到 wit 调用模型的**唯一出口** `WitRuntime._call_model_stream`：

```python
# acps_cluster/cluster_common.py
_patch_model_call_deadline()   # asyncio.wait_for 包住上游方法，默认 900s
# 开关：ACPS_MODEL_DEADLINE=0 关闭；ACPS_MODEL_CALL_DEADLINE=900 调整
```

与既有 `_patch_discovery_filter` 同款做法，幂等、不修改 wit 包。

验证：`tests/test_model_call_deadline_patch.py`（非破坏式，从补丁闭包取回原函数）
——包装已生效 / 卡住 1s 内抛 TimeoutError / 正常调用不受影响 / 重复补丁不套娃 / 可配置。

## 6. **重要纠正：AIP 任务只接受一次 `aip_submit`**（2026-10-10 深夜实测）

### 曾经走错的方向

为提高"在调用方等待窗口内交付"的概率，一度改成**增量交付**（先交资料清单、再交初稿、
最后交终稿）。实测结果（真实任务，15:15–15:40）：

| 提交 | 时刻 | 返回 |
|---|---|---|
| 第 1 次（资料清单） | 15:16:51 | ✅ `success`，任务 → `awaiting-completion` |
| 第 2 次（初稿） | 15:25:08 | ❌ `AipError: This operation is not supported` |
| 第 3 次 | 15:25:19 | ❌ 同上 |
| 第 4 次（**终稿**） | 15:39:17 | ❌ 同上 |

**结论：第一次提交成功后任务即为终态，之后任何提交都被协议拒绝。**
用户侧只拿到资料清单，**终稿被协议拒收** —— 这个方向是错的，已回退。

### 正确做法

**在任务真正做完之后，一次性提交完整终稿**（同一次调用的 `products` 数组里可以放多个元素，
被禁止的是"多次调用"）。已据此重写 playbook 纪律。

### 兜底逻辑相应加强

原来的兜底只看"模型是否调过 submit"，会**误判**：模型提前交了半成品（成功），
兜底就以为"已交付"而不补终稿 —— 实测正是这样静默丢掉了终稿。

现在按两个条件判断（`on_tool_response` 记录 `aip_submit` 的成败与提交正文长度）：

| 情况 | 兜底行为 |
|---|---|
| 从未提交 | 提交最终文本 ✔ |
| 提交过但**失败** | 仍补交 ✔ |
| 成功提交且内容 ≥ 最终文本 | 不动（避免无谓重复） |
| 成功提交但只是半成品（比最终文本短） | 补交完整终稿（会失败，但会打日志而非静默丢失） |

单测：`tests/test_submit_once_semantics.py`（5 例）、`tests/test_inbound_product_submit.py`（6 例）。

> 附带修正：`tests/test_discovery_metadata.py` 里"已注册 agent 追加检索面"的旧断言已改为
> "**逐字等于注册文本**"——后者才是 ACS 指纹保护要求的不变量
> （平台上的 supervisor 注册文本本身已含检索面，故不能断言"不含检索面"）。

## 7. 已知边界（如实记录）

- 兜底只提交 **run 的最终文本**。若模型把完整交付物分散在多条消息里、最后一条只是摘要，
  兜底提交的就是那段摘要——所以**纪律是主、兜底是保险**；建议实际使用中观察一两次。
- 提交失败（例如产品结构不被接受）会打日志 `[supervisor] 自动提交产物失败: ...` 并记入留痕，
  不会让 run 崩掉。

## 5. 相关修复清单（这条链路上的全部改动）

| # | 问题 | 修复 | 验证 |
|---|---|---|---|
| 1 | 入站只有 8 步预算 | `on_run_start` 抬到 200（`ACPS_SUPERVISOR_INBOUND_MIN_STEPS`） | 单测 + 日志 |
| 2 | 本地工具模型看不见 | 改用 `ctx.add_tool()`（`config.tools` 在 hook 前已成快照） | 真实 ToolManager 探测 |
| 3 | 模型看不到任务里的用户诉求 | `_INBOUND_INTAKE` 纪律：从编排话术里提取诉求 | 实跑验证 |
| 4 | 用 `acps_task(wait)` 轮询烧步数 | 纪律 + `wait_partner` 耐心计数（12 轮） | 实跑：15 次 wait、0 次轮询 |
| 5 | LLM 调用无限挂起 | 模型调用硬闸（首片段 120s / 总 600s） | 单测 5 例 |
| 6 | **产物未提交、调用方拿不到** | **`aip_submit` 纪律 + 自动兜底** | 单测 6 例 |

## 8. 文件形式交付（AIP FileDataItem，2026-10-29）

AIP 产物除 `text` 外还支持 `file` 类型的 DataItem：

```json
{"type": "file", "name": "报告.md", "mime_type": "text/markdown", "bytes": "<base64>"}
```

- `uri` 与 `bytes` 二选一；内联 `bytes` 上限 512KB（`wit.acps.aip.MAX_INLINE_FILE_BYTES`）；
- 校验入口：`wit.acps.aip.validate_products()`；工具 schema 同时接受 `mime_type`/`mimeType`。

两种附文件方式（都在 `supervisor.py`，默认关闭）：

1. **人工投放**：`ACPS_SUPERVISOR_ATTACH_FILES=1`，把文件放进
   `ws-supervisor/deliver/`，下一次 `aip_submit` 会把它们附到第一个产物上，
   提交成功后移入 `deliver/sent/`（每个文件只交付一次）；
2. **长文本自动转文件**：`ACPS_SUPERVISOR_ATTACH_TEXT_FILES=1`，当产物里
   `text` 总长 ≥ `ACPS_SUPERVISOR_ATTACH_TEXT_MIN_CHARS`（默认 1500 字符）时，
   代码自动追加一个同名 `.md` `file` DataItem（内联 base64，文本仍保留），
   上限 `ACPS_SUPERVISOR_ATTACH_MAX_KB`（默认 256KB）。

现网已开 2（`.env.production`）：`ACPS_SUPERVISOR_ATTACH_TEXT_FILES=1`。

**2026-10-29 实测**：用内联 `bytes` 附上 `.md` 后，任务产物 `products` 里确实有
`type=file` 的 DataItem（22KB base64），但叮当前端不显示附件。因此增加 `uri` 模式：

```bash
ACPS_SUPERVISOR_ATTACH_TEXT_MODE=uri
ACPS_SUPERVISOR_FILE_DIR=/opt/wit/cluster-viz/files
ACPS_SUPERVISOR_FILE_BASE_URL=http://8.160.176.215:5178/files
```

- 文本落盘为 `files/deliverable-<sha256前10位>.md`（5178 静态服务，公网已验证 200）；
- DataItem 改成 `{"type":"file","name":"…md","mime_type":"text/markdown","uri":"http://…/files/…md"}`；
- 同时在正文里追加一行 `📎 文件下载：<url>`——即使 UI 不渲染 file item，链接一定可见；
- `uri` 与 `bytes` 二选一；文件落盘失败会自动退回内联 `bytes`。

验证方式：让叮当发一句「查作业 job-…」，看返回产物里是否同时有 `text`（末尾带下载链接）
和 `file`（`uri` 指向 5178），以及对话框是否出现可下载附件。
