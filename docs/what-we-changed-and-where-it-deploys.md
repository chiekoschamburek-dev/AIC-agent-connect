# 我们改的是哪一层？要不要把"优化后的 ACPs"上传服务器？

> 提问背景：本地对 ACPs 框架做了一些修改，服务器上是原生 ACPs；
> 是否需要上传"我们优化后的 ACPs"才能体现设计？叮当调用 leader 是否受影响？
> 结论依据：2026-10-09 现场逐文件核对（见下）。

## 1. 结论：**框架一行都没改，因此没有"优化后的 ACPs"需要上传**

**实测证据**：本地 `site-packages/wit` 与 `my-project/vendor/wit_framework-2.1.0-cp312.cp313.cp314-none-any.whl`
逐文件 sha256 对比 —— **一致 98 个 / 不同 0 个**。`acps_sdk` 同样是官方 2.2.0 wheel。
`ACPs-community` 仓库（框架源码）本地也**没有代码级改动**（未提交的 8 项全是
`*/config/*.toml`、`registry-server/pyproject.toml`、`uv.lock` 与两个未跟踪目录
`acps-infra/dev-infra/svc-certs/`、`registry-server/win_compat/` —— 都是**本地开发/Windows 兼容**用）。

所以：**服务器上的 wit / acps_sdk 与我们本地完全同一版本、同一内容。**

## 2. 我们的"优化"实际分布在四层，各自的落地位置不同

| 层 | 内容 | 在哪生效 | 要不要"上传到服务器" |
|---|---|---|---|
| **① 框架运行时补丁** | `RunConfig` 工具超时补丁（抬到 300s）、`discovery_filter` 补丁（平台已废弃 `filter` 字段） | **我们代码里**（`cluster_common.py` 顶部 monkey patch），不改 wit 包 | ✅ **已经在服务器上生效**（服务器同文件里可见这两处补丁） |
| **② 应用层扩展** | 委托授权令牌 + PEP 准入、协作域/商业域、审计、`plan_gate`/`review_gate`/`product_gate`、引用核验三步、DeepResearch/AutoResearch 剧本… | 全部基于**官方 SDK 扩展点**实现（`aip_hooks` / `hooks` / `WitAgent.capabilities`），不 patch 框架 | ⚠️ **需要在服务器上更新我们的应用代码**——就是 `delegation/`（已部署）+ `partner_*.py` / `supervisor.py` / `cluster_common.py`（**已回滚，仍是旧版**，见 §4） |
| **③ 智能体描述/技能（ACS）** | 门面文本、tags、examples、能力声明 | **平台侧数据**，不在 wit 里 | ✅ 已通过注册生效（8 个 agent 现网描述 269–820 字、examples 2–15 条） |
| **④ 协议级研究贡献** | 域可见性过滤（`ACPs-community` 里那条 `feat: domain visibility filtering` 提交） | 若平台采纳，应部署在**平台的 discovery-server** | ❌ 那是**平台侧组件**，不是我们服务器的；只能提交给平台（研究贡献/论文素材），不能我们自己装到 app 服务器上。2026-10-20 本仓已补齐同语义纯函数（`delegation/visibility.py`）、`make_acs` 透传与 registry schema 字段；平台是否真正可见仍取决于平台侧更新 |

**一句话**：能"上传服务器"的只有 ②（我们的应用代码），① 已经在跑，③ 是平台侧数据，④ 是平台侧组件。

## 3. 叮当调用 leader：不受这个问题影响（已实测通路）

| 检查项 | 实测结果 |
|---|---|
| leader 对外端点 | 平台 ACS 声明 `https://192.0.2.10:8444/rpc` |
| 8444 是否监听 | ✅ `LISTEN 0.0.0.0:8444`（进程 `partner_supervisor.py`） |
| mTLS 实打一次 | ✅ `HTTP 200`，返回 JSON-RPC 错误 `-32601 Method not found`（我故意用错方法名）→ **服务在、协议通、能应答** |
| 平台是否认为在线 | ✅ 按 AIC 查得到（`alive:true`），叮当能检索到并展示 |
| 公开能力 | `research.orchestrate`（v1.2.0，2 条 examples）——平台可见 |

**结论**：叮当发现并调用 leader 的链路是「平台 discovery → 注册的端点 `:8444/rpc` → mTLS 调用」，
这条链路**与"我们有没有改 ACPs 框架"无关**；只要端点通、身份对（现在都对），就能被调用。

> 注：supervisor 另有一条**本地文件收件箱**（`ws-supervisor/inbox/<名字>.txt`）作为第二条通道，
> 那是服务器本机的补充入口，与平台调用互不影响。

## 4. 真正还没完成的：应用层代码只同步了一半

| 已部署到服务器 | 仍是旧版（当日回滚） |
|---|---|
| `delegation/` 整棵树（PEP/委托令牌/协作域/配额/对账） | 7 个 `partner_*.py`、`supervisor.py` |
| `discovery_metadata.py`（新增，未被引用） | `cluster_common.py`（含对端身份派生） |
| 运维脚本 + 文档 | `cluster_roster.py`、`aip_guard.py`（含壳标记中和）、`agents.py` |

**为什么回滚**：那批文件会改动"代码投影出来的 ACS"，触发 wit 的
`acs_fingerprint_changed_after_submission` 保护（详见 `incident-2026-10-09-code-sync-acs-guard.md`）。

**要上线它们，二选一**：

1. **让代码声明与注册版逐字一致**：写一层"注册文本覆盖表"（从现网 ACS 生成），
   让 `discovery_metadata` 在服务器上直接返回注册文本 → 投影不变 → 保护不触发。
   这是**推荐方案**，一次解决所有后续部署；
2. **接受"改声明 = 重新注册"**：每次改门面/能力都要重新提交 ACS + 换身份（今天已熟练，但成本高）。

## 5. 如果要把 ④（域可见性）体现出来

那条协议扩展要体现，只有两条路：

- **作为研究贡献提交给平台**（他们若采纳，部署在他们的 discovery-server 上）；
- 或在**论文/技术报告**里作为协议扩展论证（我们本地有实现与测试）。

它**不适合**装到我们自己的 agent 服务器上——因为 agent 侧不跑 discovery-server。
