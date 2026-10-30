# 静态资源节点的确定性路由（CPU/GPU 原型，2026-10-29）

> 结论：资源 agent 默认不应为每次能力调用跑一次 LLM。保留 AIP 身份/ACS/PEP/令牌，
> 把"自然语言 → 能力参数"换成**结构化 JSON → ToolCall**；解析失败才可选回落 LLM。

## 1. 做法

`acps_cluster/static_router.py` 提供一个 `StaticRouterProvider`：

- 从 AIP 信封的 `commandParams` 取 `{"skillId": "...", "args": {...}}`；
  或从 dataItems 文本里取 `{"skill": "...", "args": {...}}`；
- 按 `skillId` 找到对应能力工具，做 `input_schema` 校验（未知/缺失字段 fail-closed）；
- 直接发一次 `ToolCall`，能力结果作为最终文本提交；
- 解析失败且 `ACPS_<PREFIX>_LLM_FALLBACK=1` 时用原 provider 兜底；`0` 则返回用法错误。

AIP 注册、ACS、PEP、令牌、心跳、审计全部不变；变的只是"路由"。

## 2. 启用方式

```bash
# CPU
ACPS_CPU_STATIC_ROUTER=1
ACPS_CPU_LLM_FALLBACK=1     # 0 = 完全不用 LLM

# GPU
ACPS_GPU_STATIC_ROUTER=1
ACPS_GPU_LLM_FALLBACK=1
```

当前只接在 `_serve_existing` 路径（平台已批身份直接服务）；原来的
`up_until_ready` 注册路径保持原样。

结构化请求示例：

```json
{
  "type": "task-command",
  "commandParams": {
    "skillId": "gpu.run",
    "args": {
      "files": {"main.py": "import torch; print(torch.cuda.is_available())"},
      "entrypoint": ["python", "main.py"]
    }
  },
  "dataItems": [
    {"type": "text", "text": "运行", "metadata": {"skillId": "gpu.run"}}
  ]
}
```

非公开能力（`cpu.run`/`gpu.run`）仍需在 dataItems 里附委托令牌。

## 3. 原型实测（生产服务器 / 本机 RTX 5060）

| 调用 | LLM 路由 | 静态路由 | 执行本身 |
|---|---|---|---|
| `cpu.env` | 约 22s | **2.6s** | — |
| `gpu.env` | 约 20–40s | **0.36s** | — |
| `cpu.run` | 约 30–60s | **6.8s** | 3.3s |
| `gpu.run` | 约 30–90s | **3.5s** | 3.2s |

静态路由产物里 `toolCallId` 形如 `static-...`，不再有模型生成的中间文本；`cpu.run`
返回 `hello-cpu-run` + `output/result.txt`，`gpu.run` 返回 `cuda=True` + `output/gpu.txt`。

## 3.1 调用方接入（2026-10-29）

- **supervisor 资源调用统一走 `resource_call`**：
  `resource_call(partner="gpu", skill="gpu.run", args_json="{\"files\":{...},\"entrypoint\":[\"python\",\"main.py\"]}")`。
  工具内部复用带令牌委派（自动签 `act=leader` 的令牌），把 request 固定成
  `{"skill":"<skill>","args":{...}}`；返回 `task_ref` 后 `wait_partner` 等待；
- **supervisor 公开能力**：`acps_delegate_public` 对 cpu/gpu/kb/hpc 的公开技能
  （`cpu.env`、`gpu.status`…）自动把 request 改写成
  `{"skill":"<skill>","args":{}}`，对端直接执行；
- **supervisor 非公开能力**：继续用内置 `acps_delegate`（代码自动附令牌），但 request
  必须写成 JSON：
  `{"skill":"gpu.run","args":{"files":{...},"entrypoint":["python","main.py"]}}`；
  提示词已明确要求，自然语言只在开启 LLM 兜底时可用；
- **experiment**：普通实验（非 fetch 模式）已改为直接发
  `{"skill":compute_skill,"args":{"files":files,"entrypoint":["python","main.py"]}}`；
  fetch 模式需要先 `gpu.data.fetch` 再 `gpu.run` 两步，保留自然语言走 LLM 兜底；
- `acps_delegate_authenticated` 在受托上下文里也支持可选 `args_json`（顶层 run 已摘除该工具，
  顶层用内置 `acps_delegate`）。

## 3.2 supervisor 真实任务验收（2026-10-29）

用 `run_inbox_task` 跑了一条真实 supervisor 任务（不是直连脚本），要求只调用
`resource_call(cpu/cpu.env)` 和 `resource_call(gpu/gpu.env)`。结果：

- 首次调用报 `Partner has no discovered Direct JSON-RPC endpoint`——新 AIC 还没进本机发现缓存；
- 模型调用一次 `acps_discover` 后重试成功，两个任务 `payload_mode=static-json`；
- `wait_many` 14.4s 汇合，产物为真实环境：
  - CPU：`cpuCount=2`、Python 3.12.10、Docker true、镜像 `pytorch:cu128`；
  - GPU：RTX 5060 Laptop、8151MB、驱动 582.05、Docker true、托管数据集 5 个；
- run 状态 `completed`，steps=7。

因此 `resource_call` 已加**自动 discovery 一次**，避免第一条资源调用白失败。

## 3.3 workspace / KB 接入（2026-10-29）

- **workspace**：`ws_direct` 本来就是 mTLS + 令牌直连存储（`ws.create/read/list/write/
  clone_repo/gc`），单次 9–27ms，不经过 LLM；AIP 路径也已加
  `ACPS_WORKSPACE_STATIC_ROUTER=1`，平台若直接调 `ws.*` 同样走静态路由。
- **KB**：已加 `ACPS_KB_STATIC_ROUTER=1`；`kb.schema` 用
  `{"skill":"kb.schema","args":{}}` 直连验证，产物 `toolCallId=static-7468457bd251`，
  `status=success`，端到端 **约 0.34s**，没有模型回合。`kb.query` 的结构化入口是
  `{"query_spec":{"mode":"search","query":"..."}}`；自然语言入口保留
  `ACPS_KB_LLM_FALLBACK=1` 只做 NL → query_spec。
  - 原 `near "AS"` 错误已修：服务器 Python 自带 SQLite 3.26 不支持生成列/FTS5 trigram，
    `kb_store` 优先用 `pysqlite3-binary`（3.51），并修正了 `kb.sqlite3` 的 root 属主；
    本地/CI 无 pysqlite3 时回退标准库。
  - `kb.query` 已真实跑通域路径：用当前 KB AIC + supervisor 双签发布
    `ws-kb/acps/domains/yanshu-kb.r1.json`（`active members=2 assets=2 issuers=1`），
    KB 自签 grant 给 supervisor（max_uses=3 / 600s），再用结构化请求直连：
    `{"query_spec":{"mode":"search","query":"federated learning","top_k":3}}`。
    产物 `toolCallId=static-28dfde0865b3`、`status=success`、`callerRole=consumer`、
    `durationMs=18`，没有模型回合；`matched=0` 是因为当前 KB 还是空库。
  - 平台证书是 leaf+intermediate，签章程前必须把 intermediate 补进
    `ws-kb` / `ws-supervisor` 的 `trust-bundle.pem`；否则会 `chain-untrusted`，
    章程被隔离为 frozen。

## 4. 边界

- 一次任务只路由**一个**能力调用；资源 agent 不需要多步推理。多步属于 supervisor/experiment。
- 参数必须符合能力 `input_schema`；未知字段会被拒绝并返回允许字段，不会猜。
- 自然语言请求在 `LLM_FALLBACK=0` 时会得到明确用法错误，而不是静默失败。
- 目前覆盖 CPU/GPU/workspace/KB；HPC 可复用同一 provider，KB 保留自然语言兜底。
- GPU 本机进程的环境变量需在启动时设置（当前是会话内进程，持久化要另做服务/计划任务）。
- 本机 `ws-gpu` 保留原 dev 身份给单测用；平台身份的 GPU 跑在独立工作区
  `ws-gpu-platform`，启动时用 `ACPS_GPU_WORKSPACE` 指定，避免测试与线上身份互相污染。

## 5. 后续

1. 把 supervisor 对资源 agent 的委派从自然语言改成结构化 `args`；
2. HPC/KB 接入同一路由；
3. 观察 `run-guard` 与静态路由的交互（资源调用不再产生模型步，超时规则更干净）。
