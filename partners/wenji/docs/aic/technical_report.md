# AIC·智能体互联技术报告

**作品名称：问迹——AIP 智能体互联的可解释问数/问答智能体**

> 本报告按《AIC·智能体互联技术报告参考大纲》撰写。文中标注【待补】的条目需在平台联调完成后补齐
> （平台截图、访问数据、端到端时延），其余结论均可在本仓库复现（复现入口见附录 A）。
> 材料中不出现团队所属单位、个人与指导教师信息。

---

## 一、作品概述

### 1.1 行业与技术背景

大模型驱动的智能体正从单点工具走向协同网络：企业需要的不再是"一个会聊天的模型"，而是
"一组能互相发现、互相调用、结果可追溯的智能体"。然而当前落地面临三类现实障碍：

1. **协议割裂**：数据智能体、文档智能体、业务系统智能体各自封装，接口与任务模型不统一，
   跨系统协作只能靠点对点适配；
2. **发现机制缺失**：即使能调用，调用方也无法在运行期回答"谁具备这项能力、它现在是否在线、
   它的输入输出契约是什么"；
3. **协作过程不可核验**：调用链、任务状态、访问留痕分散在各系统日志中，无法形成可信证据链。

GB/Z 185《人工智能 智能体互联》国标与 ACPs 协议族（AIC/ACS/ATR/AIP/ADP/AMP）为上述问题
给出统一框架：身份码唯一标识智能体，ACS 描述能力与端点，ATR 完成可信注册与证书签发，
AIP 承载任务交互，ADP 支持能力发现，AMP 提供运行与访问可观测性。

本作品以 **梧桐智能体互联平台**为验证场景，将一套已经过多轮评测、具备完整可解释性的
问数/问答引擎（问迹）通过 AIP 国标接入平台网络：平台上的其它智能体可以"发现"问迹、
向其委派数据类任务并取回**带引用与 Trace 证据**的结果；问迹也可以反向发现并调用平台上的
队友智能体，完成更复杂的跨域任务。作品验证了"通用数据智能体 + AIP 互联"这一标准化路径的可行性。

### 1.2 作品目标定位

**核心目标**：在不重写业务引擎的前提下，把既有能力"互联化"——用最小适配层完成注册、发现、
互联、访问全流程，并让协作过程可观测、可审计。

**硬性目标（对照评分"必要验证场景"）**：

| # | 目标 | 完成标志 |
|---|---|---|
| H1 | 智能体注册 | 梧桐平台注册通过，获得 AIC；本地取得 mTLS 客户端/服务端证书与信任链 |
| H2 | 自动发现 | 平台/队友 Leader 依据能力描述检索到问迹，返回可用端点 |
| H3 | 跨端互联访问 | 队友智能体发起 `wenji.ask` 任务，问迹经 mTLS 接单并返回结构化产物 |
| H4 | 访问数据留存 | 平台后台可见访问记录；AMP access 日志可抽样展示 |
| H5 | 反向委派 | 问迹以 Leader 身份发现并调用队友智能体，汇总多来源结果 |

**业务场景**：企业数据问答（销量/客户/员工等结构化问题）、制度文档问答（带页码引用）、
跨源多跳分析（先查库、再检索文档、融合给出结论）、公式计算（从文档登记公式并代入数据复算）。

**预期效益**：

- 技术上，沉淀"引擎能力 → ACPs Skill"的可复用接入范式，接入层代码约 900 行；
- 工程上，平台侧无需了解问迹内部实现，仅凭 ACS 即可完成发现与调用；
- 社会效益上，为"异构数据智能体互通"提供可复制的标准化样例，推动 AIP 落地实践。

---

## 二、需求分析

### 2.1 行业痛点剖析

**（1）协议不统一、互操作成本高。** 各厂商智能体多用自定义 HTTP/JSON 接口，任务语义
（开始/进行/等待输入/等待确认/取消）各不相同，集成方需要为每个智能体写适配器。

**（2）平台适配成本高。** 即使采用 HTTP，身份认证、证书轮换、健康检查、日志格式仍需各自实现；
本作品通过 ACPs SDK + WIT 框架把注册状态机、mTLS 监听、AMP 打点内置化，显著降低接入成本。

**（3）协作过程不可观测。** 评分与合规都要求"可回溯"：谁调用了谁、花了多久、结果出自哪里。
AIP 定义了任务状态机与身份绑定，AMP 定义了访问/消息/审计日志，使协作链路第一次成为可查询的数据。

**（4）数据智能体特有的可信性问题。** 问数结果必须可核验（SQL + 执行结果），文档问答必须给引用，
不能是"看起来合理"的自然语言。这与 AIP 强调的可信交互天然一致。

### 2.2 场景与功能需求调研

调研路径：AIP-PUB 开源仓库（规范、SDK、CLI、leader/partner 示例）、梧桐平台使用指南、
团队内其它智能体（队友负责不同领域）的能力边界、既有问迹引擎的评测数据。

**功能需求**（划分为四类指标）：

| 类别 | 指标 | 本作品取值 |
|---|---|---|
| 功能 | 能力清单与输入输出契约 | 4 个 Skill：`wenji.ask` / `data_query` / `doc_qa` / `schema` |
| 功能 | 任务状态机覆盖 | 支持 start/get/continue/complete/cancel 与全部稳定状态 |
| 性能 | 接入响应时延 | 【待补：平台联调后统计 /health 与 task 往返 P50/P95】 |
| 性能 | 引擎内部时延 | 检索 10→1000 篇 P50 7→133ms；Schema Linking 11→71 表 P50 55→160ms |
| 兼容 | 标准符合性 | GB/Z 185（AIC/ACS/ATR/AIP/ADP/AMP）；ACPs protocolVersion 02.02 |
| 兼容 | 交付形态 | `AcpsDirect(rpc)` JSON-RPC + mTLS；单端口 8443 独立于业务端口 8000 |
| 部署 | 一键启动 | `deploy/quick_start.sh`（PG+知识库+前后端）；ACPs 装 SDK 后 5 条命令接入 |

**关键约束**：

- 注册、证书签发、审核是平台行为，作品侧必须做到"可等待、可重试、状态可观察"；
- 公网端点必须使用 HTTPS，证书 SAN 覆盖公开主机名；内网地址不能作为发布端点；
- AMP 是访问计量的唯一数据源，字段必须与平台统计口径一致（`correlationId=taskId`）。

---

## 三、智能体互联技术选型与方案设计

### 3.1 基础开发资源选型

| 资源 | 选型 | 理由 |
|---|---|---|
| 协议实现 | **ACPs SDK 2.2.0** | GB/Z 185 官方参考实现，覆盖 AIC/ACS/AIP/ADP/AMP 数据对象与校验 |
| 开发框架 | **WIT（梧桐）2.1.0** | 内置注册状态机（ATR）、Direct/Group 监听器、mTLS 校验、AMP 生命周期，避免重复造轮子 |
| 运行环境 | Python 3.12 + FastAPI + PostgreSQL/pgvector | 与既有引擎一致，零迁移成本 |
| LLM（引擎内核） | BUPT 校园网关（`deepseek-v4-flash` / `qwen3.8-27b`，OpenAI 兼容，经校园网隧道代理） | 免费额度；关闭 thinking 后时延与 DeepSeek 持平（13.1s vs 13.2s），并发吞吐场景 qwen3.8-27b 更优（20~30s/任务）；账本按 0 价记账 |
| LLM（AIP 包装层） | 本地反向代理 → BUPT 网关（`app/acps/llm_proxy.py`） | WIT provider 不支持自定义代理且绕过记账层；本地代理解决 DNS/隧道并注入 `enable_thinking=false`，两层成本均为 ¥0 |
| 许可证 | MulanPSL-2.0 | 允许随作品分发，团队统一放在仓库根 `vendor/`（见根 `NOTICE`） |

对 AIP 国标的遵循思路：**不自行发明协议**。所有互联动作通过 WIT 的公开 API 完成，
数据对象（TaskCommand/TaskResult/Product/DataItem）由 SDK 校验，身份以证书 SAN 为准而非请求自报。

### 3.2 核心技术方案设计

#### 3.2.1 整体架构

```
                    ┌──────────────────── 梧桐智能体互联平台 ────────────────────┐
                    │ Registry（注册/审核/AIC） · CA（EAB/mTLS 双用途证书）        │
                    │ Discovery（ADP 能力检索） · Monitor（AMP 六类日志）          │
                    └───────▲──────────────────────▲───────────────────▲───────┘
                            │ ATR 注册/证书          │ ADP 发现          │ AMP 上报
              ┌─────────────┴──────────┐   ┌─────────┴───────────────────┴─────────┐
              │ 问迹 · ACPs 适配层       │   │ 队友智能体（Leader / Partner）          │
              │  (WIT runtime, :8443)   │◄──┤  按 ACS 发现问迹，经 mTLS 委派任务       │
              │  AIP Direct Partner     │   └───────────────────────────────────────┘
              │  Leader 委派（discover/ │
              │  delegate/task）        │
              └────────────┬────────────┘
                           │ 同进程函数调用（能力桥接）
              ┌────────────▼────────────────────────────────────────────────────┐
              │ 问迹引擎（FastAPI :8000 + Vue3 :8090）                            │
              │ AgentKernel（意图/路由/澄清）· NL2SQL · RAG · 跨源 DAG · 公式引擎    │
              │ Trace 收集器（全链路） · 成本账本 · 评测体系                        │
              └──────────────────────────────────────────────────────────────────┘
```

**分层说明**：

- **协议适配层**（`backend/app/acps/`）：唯一新增的互联代码，向下调用引擎，向上对接平台；
- **业务引擎层**：不变，仍由 FastAPI + Vue 提供交互式演示与全链路 Trace；
- **平台接入层**：Registry/CA/Discovery/Monitor 由梧桐平台提供，本作品只作为客户端接入；
- **部署形态**：全栈（引擎 + ACPs 服务 + AMP 转发 + 流量运行器）常驻云服务器
  （2C/4G），`systemd` 用户级六单元值守（`wenji-db/backend/frontend/serve/amp-forwarder/traffic`），
  AIP 服务**直接监听公网 8601 端口**（mTLS 端到端，无中间代理）。

#### 3.2.2 关键技术选型

| 问题 | 方案 | 实现位置 |
|---|---|---|
| 能力如何被平台发现 | WIT 由 `@capability(acps_skill=True)` 生成 ACS Skills，含描述/标签/示例/输入输出模态 | `capabilities.py` |
| 任务如何被接收 | `AcpsDirect(rpc)` Partner listener：JSON-RPC + mTLS + 身份绑定 + `/health` | `agent.py` + WIT |
| 身份如何可信 | ATR integrated：Registry 审核 → AIC → EAB → CA 签发 clientAuth/serverAuth 双证书 → 本地校验后激活 | `runtime.py` + WIT |
| 组合任务如何编排 | 双层编排：AIP 层（Leader 发现/委派/等待）+ 引擎层（意图路由/HYBRID DAG/澄清） | `agent.py` + `agent/planner.py` |
| 协作如何可观测 | WIT 自动产生 heartbeat/metrics/system/access；自研 `TaskAccessHooks` 按任务粒度补发 access（correlationId=taskId，含真实 durationMs 与终态状态码） | `task_access.py` |
| 访问量如何计量 | 平台按 taskId 配对计 1 次调用/被调用；wit 边界记录为 rpc 请求 ID，故按任务粒度补发（每任务恰一条）；metrics 实时上报真实成功率与时延分位 | `task_access.py` + `bridge.py` |

#### 3.2.3 开源代码改造与自主创新划分

| 模块 | 来源 | 说明 |
|---|---|---|
| ACPs 数据对象/校验/传输 | **上游复用** | `acps_sdk`（未修改） |
| 注册状态机/监听器/AMP 生命周期/Leader 工具注入 | **上游复用** | `wit`（未修改） |
| 能力定义与 Skill 投影 | **自主开发** | `capabilities.py`（4 个对外能力 + 公式引擎桥接，一次声明三处复用） |
| 引擎桥接与任务计数 | **自主开发** | `bridge.py`（线程池隔离阻塞流水线；任务计数/成功率/时延分位） |
| 任务级 access 与会话隔离 | **自主开发** | `task_access.py`（任务粒度 access 补发 + contextvar 按 AIP task 隔离内核会话，澄清/continue 在任务内闭环） |
| AIP 状态面板数据 | **自主开发** | `status_api.py` + 前端 `AipStatusCard.vue`（AIC/在线/调用统计实时可视） |
| 长期值守与流量 | **自主开发** | systemd 六服务（含用户态 PostgreSQL 16+pgvector 编译部署）+ 多工作并发流量运行器（77 题库，实测 ¥0.0043/任务） |
| ACPs 配置模型与注册 CLI | **自主开发** | `settings.py`/`runtime.py`/`cli.py`（check/show-acs/register/serve/status/smoke） |
| AMP 上报转发器 | **自主开发** | `tools/amp_forwarder/`（journal → 平台 Kafka，含 dry-run 与 offset 持久化） |
| 问迹引擎（NL2SQL/RAG/跨源/公式/Trace） | **自主开发（赛前积累）** | 本作品的业务内核，见第四章 |

---

## 四、项目实施

### 4.1 整体实施方案

**开发流程**：引擎盘点 → 能力边界定义 → Skill 声明与桥接 → ATR 注册 → 平台联调 →
全流程验证与留证 → 报告/视频/打包。

**关键路径管理**：注册审核是唯一外部依赖，因此把"生成 ACS 草稿 → 提交"放在第一位，
审核等待期并行完成引擎演示素材、报告骨架与视频第 1 遍；证书到手后立即联调。

**软硬件配置**：WSL2 Ubuntu 24.04；Python 3.12；PostgreSQL 16 + pgvector；Node 20/pnpm；
GPU 消融实验使用 RTX 5090（可选，默认 CPU 部署）。

**团队分工**：本智能体（问迹）由 1 人负责端到端；其余队友负责各自领域智能体，
通过梧桐平台互联协作（Leader/Partner 交叉调用）。

### 4.2 关键技术落地实现

#### 4.2.1 引擎能力到 ACPs Skill 的映射

```python
# backend/app/acps/capabilities.py（节选）
@capability(
    id="wenji.ask",
    description="综合问数/问答：问题可同时涉及业务数据库与知识库文档……",
    acps_skill=True, public=True, input_modes=["text/plain"], tags=["问数", "问答", "跨源", "可解释"],
)
async def ask(question: str, session_id: str = "aip") -> dict:
    return await bridge.run_in_thread(bridge.answer_question, question, session_id)
```

一次声明同时产生：① 模型工具（WIT 内部编排用）；② ACS Skill（平台发现用）；
③ 本仓库文档中的能力说明。避免了"协议描述"与"实现"两份事实不同步的经典问题。

#### 4.2.2 可信注册全流程

注册由 WIT 状态机推进：`needs_login → draft_ready → submitted → pending_review →
approved → issuing_certificates → finalizing → active → serving`。

```bash
python -m app.acps.cli check         # 本地：声明校验 + ACS 投影 + 公网端点检查
python -m app.acps.cli show-acs      # 生成 ACS 草稿（可网页上传）
python -m app.acps.cli register --watch   # 登录→提交→盯审核
python -m app.acps.cli serve         # active 后启动 AIP Direct + AMP
```

证书校验（由 WIT 在激活前强制执行）：leaf CN 与 AIC 一致、恰好一个 `acps://{AIC}` URI SAN、
EKU 分别为 clientAuth/serverAuth、私钥与证书匹配、证书链可达信任链、server 证书 SAN 覆盖公开端点。
**任何一项不通过，身份不会进入 active，服务不会启动**。

#### 4.2.3 AIP Direct 服务端与身份绑定

Partner 监听器由 `serve_direct_partner()` 启动，strict 模式强制 TLS 1.3 与 `CERT_REQUIRED`，
从对端证书解析调用方 AIC，并校验消息中的 senderId 与证书身份一致（防冒充）。
业务互操作只发生在任务层：`TaskCommand(start/get/continue/complete/cancel)` →
`TaskResult(accepted/working/awaiting-input/awaiting-completion/completed/failed/rejected/canceled)`，
产物经 `Product`/`TextDataItem`/`StructuredDataItem` 返回。

#### 4.2.4 Leader 侧发现与委派

`AipDelegationHooks` 在每次运行时注入三个工具：`acps_discover`（Discovery 检索并缓存 Partner）、
`acps_delegate`（创建 Direct 任务）、`acps_task`（wait/inspect/continue/complete/cancel）。
指令约束模型：问数/问答优先用本地能力；超出能力范围（如队友负责的领域）才允许发现并委派；
委派后必须 `action="wait"` 等结果，最终答复标注来源。

#### 4.2.5 兼容性问题与优化

| 问题 | 现象 | 处理 |
|---|---|---|
| 公网部署与端口直连 | 开发机无公网/证书 SAN 校验 | 两种形态均验证过：① 过渡期 socat+SSH 隧道桥接（原始 TCP 保留 mTLS 端到端）；② 终态全栈迁至云服务器直接监听 8601（无代理层，证书 SAN 覆盖公网 IP，未触发 ACS 重审/重签） |
| ACS 与运行声明一致性 | wit 状态机要求 draft==final 才能启动服务，改动声明会导致 serve 拒启（实测踩坑并恢复） | 声明版本门控（v1.0.0=已批准形态）；升级需走平台 ACS 更新流程，本作品保持 v1.0.0（公式计算已由 wenji.ask 内核路由覆盖） |
| DeepSeek base_url | 官方地址带/不带 `/v1` 皆可 | 包装层统一补 `/v1` 走 OpenAI 兼容面（`provider.py`） |
| 阻塞流水线阻塞事件循环 | NL2SQL/RAG 为同步实现，单次数秒 | 能力 handler 统一 `asyncio.to_thread` 隔离（`bridge.py`） |
| 平台统计口径 | AMP access 需 `correlationId=taskId` | 采用 `AmpAccessLoggingHooks` 并使用平台口径核对；必要时补应用侧 emit |
| 模型双跳延迟 | WIT 编排层 + 引擎层各一次 LLM 调用 | 双通道拆分：包装层走公网 DeepSeek、内核走校园免费额度；实测任务往返 20~55s（BUPT 内核），RPC 边界仍为 P50 53ms |
| AMP journal 跨进程冲突 | 多个进程为同一 AIC 各绑一套 AMP runtime 时，wit 追加校验报 `journal changed during validation` | 流量器与自测脚本改为**纯客户端**（AipRpcClient + 原生 discovery），只有 serve 进程写 journal，冲突归零 |
| 校园网模型接入 | 校内模型网关解析到内网地址，公网服务器不可达 | 复用 18888 反向隧道（HTTP CONNECT 代理，`Proxy-Agent: campus-llm-proxy`）；内核 provider 挂 httpx 代理，AIP 出站不走代理 |

---

## 五、应用成效

### 5.1 功能成果展示

1. **引擎能力**（交互式演示）：问数返回 SQL + 数据卡片；文档问答返回文档/页码引用；
   跨源问题返回分区溯源答案；全过程 Trace 时间线/DAG 可视化。
   【待补：前端截图 4 张】
2. **平台接入**：注册平台智能体详情（AIC）、Discovery 检索结果、mTLS 服务健康检查。
   【待补：平台截图 3 张（脱敏组织信息）】
3. **多智能体协同**：队友 Leader 委派任务到问迹并取回结果；问迹反向委派队友智能体的时序记录。
   【待补：联调记录/截图】

### 5.2 量化成效分析

**引擎侧（已有实测，可复现）**：

| 指标 | 数值 | 来源 |
|---|---|---|
| NL2SQL 执行准确率（61 例 L2） | 100%（主备重复 3 次零方差） | `backend/var/eval/` 基线 |
| RAG 答案命中（41 例） | 100%（检索同为 100%） | 同上 |
| 跨源多跳（11 例，5 类） | 11/11 | `eval/run_cross_source.py` |
| 澄清（30 例） | 30/30 | `eval/run_clarify.py` |
| 混合检索时延（10→1000 篇） | P50 7.4→133.0 ms | `docs/milestones/w5_efficiency/` |
| Schema Linking（11→71 表） | P50 55.2→160.4 ms | 同上 |
| 前缀缓存命中 | ~89%，成本节省约 53% | `GET /api/usage` |
| 模型总成本 | 引擎内核 ¥0（BUPT 免费额度）；包装层经本地代理后 ¥0 | 余额核对 + 代理统计 |
| 成本 | 全部实验实付 ¥4.19（预算 ¥1000 内） | 成本账本 |

**互联侧（平台联调后补）**：

| 指标 | 数值 | 证据 |
|---|---|---|
| AIP `/health` 探测时延（公网经 VPS 桥接） | P50 ≈ 0.16s（5 次实测 0.10~0.18s） | `curl --cert ... https://8.160.176.215:8601/health` |
| JSON-RPC 边界时延（不含 LLM） | P50 53ms · P95 320ms · max 525ms（n=62） | `amp_access_*.jsonl` durationMs |
| 任务往返时延（start→completed，LLM 主导） | 问数 ≈10.5s · 问答 ≈10.6s · 跨源 ≈14s · 澄清→awaiting-input ≈8s | 任务 status_history |
| 任务级 access 记录（correlationId=taskId） | 持续积累（流量运行器 120 任务/日，四类场景） | `amp_access_*.jsonl` + `docs/aic/evidence/` |
| 平台累计访问/调用次数 | 【待补：平台后台截图】 | 梧桐平台后台 |
| 接入响应完整性 | 注册/发现/互联/访问 **4/4 完成** | 本节验证清单 |

**三维度效益分析**：

- **技术落地**：以约 900 行适配代码换取平台级发现与可信交互能力，证明存量智能体可低改造接入；
- **标准化推广**：能力声明（ACS）与实现解耦，为同类数据智能体提供模板；
- **场景赋能**：跨组织数据问答从"人工对接"变为"自动发现 + 按任务调用"，为数据要素流通提供协议基础。

### 5.3 必要验证场景完成情况

| 验证项 | 状态 | 佐证 |
|---|---|---|
| 智能体注册（AIC） | ✅ `1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN` | 注册平台详情页（截图时打码 organization） |
| 自动发现 | ✅ 平台 discovery 按"问数/数据库/文档问答"命中问迹（active=true，4 Skills） | Discovery 响应 JSON + 平台页截图 |
| 跨端互联访问 | ✅ 双向：问迹→研枢-Supervisor（Leader 委派获完整产出）；自测全状态机（含 awaiting-input 澄清流） | `aip_selftest.py` 终端输出 + 任务 status_history |
| 后台访问数据 | ✅ 心跳进平台 Kafka（30s 窗口实测命中）；access 按 taskId 口径上报 | 平台后台截图（待截）+ `amp_access_*.jsonl` |
| 反向委派（加分项） | ✅ 问迹以 Leader 身份调用队友智能体并融合结果 | selftest `--partner-aic` 记录 |

### 5.4 开放开源贡献

- 本作品的 AIP 接入层计划整理为可复用模板（"引擎能力 → ACPs Skill"清单化步骤）；
- 仓库包含完整源码、一键部署脚本与全部评测用例；
- 【待补：AIP-PUB 社区 issue/分享记录（如有）】。

---

## 六、总结与展望

### 6.1 经验总结

**踩坑记录（对社区有参考价值）**：

1. wit 边界 access 记录的 correlationId 是 rpc 请求 ID 且一任务多条，不满足平台 taskId 配对口径——需应用层按任务粒度补发；
2. wit 2.1.0 注册状态机为一次性设计：已激活身份下改动 ACS 声明会使 serve 拒启（`acs_fingerprint_changed_after_submission`），升级 ACS 须走平台更新流程并注意切换时序；
3. AIP 任务应按 task 隔离业务会话（我们用 contextvar 传递 task_id），否则前一任务的澄清挂起会污染后续任务。

**成功经验**：

1. **"能力声明即资产"**：一份 `@capability` 声明服务模型工具、平台发现与文档三处，
   消除协议描述与实现漂移；
2. **把协议正确性交给框架**：注册状态机、证书校验、身份绑定、AMP 生命周期由 WIT 保证，
   团队专注业务语义，接入层保持在千行以内；
3. **等待期并行**：注册审核是唯一外部阻塞，工程侧提前完成其余全部可交付物。

**不足**：

1. AMP 上报链路仍依赖平台 Kafka 参数开放，当前以本地 journal + dry-run 联调；
2. 公网部署（DNS/证书 SAN/反向代理保留 mTLS）对参赛者有一定门槛，缺少一键脚本；
3. 评测集为自建回归/验收集，不能外推为开放式环境准确率（已在报告中如实声明）。

### 6.2 未来规划

1. **协议扩展**：引入 AIP Group 模式支持多方协同会议式任务；stream 模式输出增量结果；
2. **能力扩展**：把图表生成、报表导出、多库联邦问数注册为新的 Skills；
3. **工程优化**：公网部署一键脚本（自动申请 DNS 挑战证书 + 反向代理配置）；AMP 转发器产品化；
4. **生态贡献**：将接入层抽象为"数据智能体 AIP 接入模板"回馈 AIP-PUB 社区。

---

## 七、附录

### 附录 A：复现入口

```bash
./deploy/quick_start.sh                     # 一键：DB + 知识库 + 后端 + 前端 + 冒烟
cd backend
.venv/bin/python -m pytest tests/ -q        # 引擎全量测试
.venv/bin/python -m app.acps.cli check      # AIP 声明/ACS 校验
.venv/bin/python -m app.acps.cli smoke --question "销量最高的前五首曲目"
.venv/bin/python eval/run_levels.py --level L2 --providers deepseek,qwen --repeats 3
.venv/bin/python scripts/acps/aip_selftest.py --question "员工年假有多少天？"   # 注册后自测
python3 tools/amp_forwarder/forwarder.py --journal-dir .wit/wenji-aip-agent --dry-run
```

### 附录 B：关键配置与数据

- ACPs 配置模板：`.env.acps.example`；平台 CLI：`deploy/acps/acps-cli.toml.example`
- Trace 契约：`docs/contracts/trace.md`（v0.2 冻结）
- 评测用例：`backend/eval/cases/*.jsonl`（161 例）；效率矩阵：`docs/milestones/w5_efficiency/`
- 开源组件与许可：仓库根 `vendor/` 与 `NOTICE`

### 附录 C：参考文献

1. GB/Z 185《人工智能 智能体互联》系列（总体架构/身份码/身份管理/智能体描述/发现/交互/工具调用）
2. ACPs 协议规范 v2.2.0（AIC/ACS/ATR/AIP/ADP/AMP/AAC），AIP-PUB 开源社区
3. 梧桐智能体互联平台使用指南
4. ACPs 智能体快速开发指南、AMP 可观测性接入教程
5. Chinook 示例数据库；BGE-small-zh-v1.5 / BGE-M3 嵌入模型
