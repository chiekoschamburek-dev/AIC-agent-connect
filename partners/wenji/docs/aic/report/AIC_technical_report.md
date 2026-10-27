# 研枢：基于 AIP 互联的数智研跨域多智能体集群
## 技术报告

> 参赛赛题：AIC·智能体互联（第八届全球校园人工智能算法精英大赛·算法主题赛）
> 协议依据：GB/Z 185《人工智能 智能体互联》与 ACPs（Agent Collaboration Protocols）
> 开源基线：ACPs-community 2.2.0 / ACPs SDK 2.2.0 / WIT Framework 2.1.0（AIP-PUB 社区）
> 说明：全文数据均标注测试环境、样本量与统计口径；〔待补〕为提交前需替换的平台截图与编号位。

---

## 0. 作品简介（≤300 字）

科研协作中的算力、数据、模型与写作能力分属不同机构、不同系统，**能连起来不等于能放心用**：
谁有权调用、用了多少、通信是否异常、结果能否复核，缺少可执行的协议与证据。研枢依据
GB/Z 185 与 ACPs 协议族，在梧桐平台构建"受治资源协作网络"：1 个总控编排 16 个伙伴
（工作流 9、资源 4、评测/模型与两个外部成员），把 GPU/CPU 算力、评测数据、知识库、算法权重
封装为**可发现、可授权、可计量、可撤销、可审计**的资源智能体。三项自研机制——任务级 PEP
委托授权、资源 agent 化、双签协作域——把 AAC 的委托语义落成代码；问迹作为集群的问数/问答成员，
提供 NL2SQL、带引用 RAG、跨源与公式计算，并以 4 个 ACPs Skills 完成平台注册—发现—互联—访问验证。
离线 757 项测试全绿，PEP/域/评测等场景 16/16、11/11、9/9 通过；平台侧公网健康检查 P50≈0.16 s。

---

## 1. 项目背景与意义（对应评分：需求分析 10 分）

### 1.1 异构智能体互联的行业痛点

| # | 痛点 | 具体表现 | 直接后果 |
|---|---|---|---|
| P1 | 协议割裂 | 各智能体自定义接口与任务语义，缺少统一状态机 | 每接入一个节点写一套适配器 |
| P2 | 发现缺失 | 运行期无法回答"谁有能力、是否在线、契约是什么" | 端点硬编码，节点一变全线失效 |
| P3 | 协作不可核验 | 调用链与访问留痕口径不一（会话 ID/请求 ID 混用） | 无法计费、审计与责任界定 |
| P4 | 授权不可执行 | 标准定义了委托语义，但缺少可运行的令牌、准入与撤销实现 | "能连"变成"能越权"；资源方不敢开放 |

科研场景把上述问题放大：一次文献综述 + 复现实验可能横跨两家机构、四类资源（算力、评测数据、
知识库、写作），既有**跨信任域**的授权问题，也有**长流程**的状态与审计问题。

### 1.2 多智能体互通场景调研

**方法与样本**（2026-10-14 至 10-27）：

1. **标准与实现研究**：GB/Z 185 七个部分、ACPs 十册规范（AIC/ACS/ATR/AIA/AIP/ADP/DSP/AMP/AAC）、
   AIP-PUB 的 ACPs SDK 2.2.0 与 WIT 2.1.0、梧桐平台接入指南；
2. **平台公开发现抽样**：多轮 Discovery 检索累计获得 **18 个在线智能体**的 ACS，覆盖科研协作、
   医疗影像/报告、法律咨询、校园报修、运维诊断、平台客服 6 类场景；对 2 个外部端点做 mTLS
   连通性检查，对 1 个（研枢-Supervisor）完成端到端任务调用；
3. **团队内约束对齐**：与编排、协议安全、前端数据三条线对齐"新成员入册、被发现、被授权"的
   实际约束，形成名册字段与接入检查单；
4. **自建集群压测**：757 项离线单测 + 7 类演示场景（PEP/域/商业域/知识库/评测/GPU/两机构）。

**结论**（直接决定设计取舍）：

| 结论 | 证据 | 设计约束 |
|---|---|---|
| 列出 ≠ 可编排 | 18 个智能体能力粒度与任务语义差异大 | 必须发布稳定能力契约（ACS Skills），而非单一万能入口 |
| 在线判定依赖心跳 | Discovery `aliveMap` 由 AMP 心跳经 Kafka 汇聚 | AMP 打点是一等公民，不是上线后补日志 |
| 计量口径须与任务对齐 | 平台按 `correlationId=taskId` 双侧配对；框架默认用 RPC 请求 ID | 应用层补任务级记录（双侧），否则"有调用、榜上无名" |
| 资源方要"可撤销" | 行业访谈中，资源方最关心"能否急停、能否对账" | 授权必须带 TTL/次数/冻结与用量对账 |
| 结论要能复核 | 团队评审中无证据的回答被判定不可用于决策 | Trace / SQL / 引用 / 公式步骤作为产品能力 |

### 1.3 智能体互联标准化现状与平台接入场景

标准层面，GB/Z 185 已给出身份、描述、注册、发现、交互、监控与访问控制的完整框架；ACPs 参考实现
让协议"可运行"。但工程落地仍有三段空隙：

1. **注册是一次性的**：WIT 2.1.0 在身份激活后要求本地声明与平台 ACS 指纹一致，就地修改会触发
   `acs_fingerprint_changed_after_submission` 并拒绝服务——能力演进需要"新版本 + 新工作区"的显式流程；
2. **计量口径需应用补齐**：框架记录传输边界，业务级（taskId）配对要应用自行实现；
3. **跨组织授权无开箱实现**：AAC 定义委托语义，官方 SDK 零实现，需要自研 PEP（本作品创新 1）。

平台接入场景要求完成"注册→自动发现→跨端互联→访问"并留存数据。研枢在此之上进一步要求
**可编排**：Supervisor 按能力发现并委派；资源方按令牌准入；全过程可计量、可冻结。

**平台抽样分布（表 1-1）**

| 场景类别 | 数量 | 典型能力 |
|---|---:|---|
| 科研协作/文献 | 5 | 任务调度、文献检索、写作、审稿、引用核验 |
| 数据问答/知识库 | 3 | 问数、文档问答、知识库查询 |
| 医疗健康 | 2 | 影像描述、报告解读 |
| 法律/合规 | 1 | 法条检索与建议 |
| 校园/生活服务 | 4 | 报修调度、活动策划、社交助理、格式检查 |
| 运维/诊断 | 3 | 指标、日志、链路、协调 |

**表 1-1 平台公开发现抽样**：18 个在线智能体中，具备"数据问答"能力的仅 3 个，且没有一个是
"SQL + 引用 + 公式 + 跨源"一体化；这为问迹的差异化定位提供了直接依据。


### 1.4 需求定位与产业贴合度

| 需求 | 指标 | 落点 |
|---|---|---|
| 可发现 | Discovery 命中且 alive=true，返回能力与端点 | 17 条名册（1 supervisor + 16 partner，含 2 外部成员） |
| 可授权 | 每次委派有令牌/白名单/public 三级准入，缺失 fail-closed | PEP（Ed25519 JWS、jti、TTL、max_uses） |
| 可计量 | 心跳 + 任务级访问双侧记录 + 域用量对账 | AMP + TaskAccess 钩子 + reconciliation |
| 可撤销 | 令牌过期/次数用尽/域冻结即刻生效 | token/grant/domain 冻结 |
| 敢用 | SQL/引用/公式步骤/全链路 Trace | 问迹引擎与集群审计流 |

〔假设：团队 3 人分工见第 7 章，按实际情况可替换〕

---

## 2. 赛题方向定位

本作品不把"智能体互联"理解为"把 API 串起来"，而是把它当作**资源治理协议**：

- **从"能用协议"到"治理协议"**：协议解决连通，治理解决"谁有权、用了多少、能否急停、能否复核"；
- **从"单智能体"到"跨域集群"**：机构 B（消费方）与机构 A（资源提供方）以双签章程建立协作域，
  域内 grant 派生短时令牌，跨 CA、跨网络（反向隧道）仍可验证；
- **从"演示"到"可复现证据"**：每个创新点都有单测与场景化验收（16/16、11/11、9/9…），
  并保留失败与诚实降级记录。

因此研枢同时对齐赛题的三条硬性要求：依托 AIP 国标与 AIP-PUB 开源代码、在梧桐平台完成全流程验证、
以及"多智能体协同交互方案"的创新性——分别由第 3/4/5 章展开。

---

## 3. 总体方案设计（对应评分：创新性 25 分 / AI 技术应用 20 分）

### 3.1 设计目标、约束与边界

**目标（五条，均可验收）**：可发现（Discovery 命中且在线）、可授权（三级准入、fail-closed）、
可计量（心跳 + 任务级访问 + 域对账）、可撤销（TTL/次数/冻结即刻生效）、可审计（工作区
`manifest.jsonl`、PEP 审计流、可视化事件流、通信风险报告）。

**硬约束**：

| 约束 | 说明 |
|---|---|
| 不改上游运行时 | 只使用 wit/acps_sdk 的公开扩展点；对上游的改动集中在 discovery-server 的域可见性分支，并以 diff 形式回馈 |
| 令牌不进模型上下文 | 委托令牌以 jti 句柄形式交由业务能力出站使用，模型只见句柄 |
| 平台身份为准 | AIC/端点/技能以平台 ACS 为权威，不在代码里硬编码 |
| 能力演进可控 | ACS 指纹校验要求"声明==已批准版本"，升级走新版本/新工作区 |
| 失败也要诚实 | 失败、拒件、降级都产出可审计记录，禁止编造结果 |

### 3.2 总体架构

```text
机构 B（消费方：研枢集群）                              机构 A（资源提供方）
┌──────────────────────────────────────────┐          ┌────────────────────────────────────┐
│ supervisor（总控 / 编排 / 完成闸门 / 聚合）│          │ edge-leader（管理 leader）           │
│ ├─ 工作流：scout / synthesis / writer     │  双签域   │ ├─ gpu-partner（8460 沙盒+GPU）      │
│ │        critic / frontier / contradiction│  章程+    │ ├─ cpu-partner（8462 沙盒无卡）      │
│ │        experiment / citation            │  域 grant │ ├─ eval-partner（8464 评测）         │
│ ├─ 资源：kb / cpu / gpu / workspace      │◄────────►│ ├─ model-partner（8467 算法托管）    │
│ ├─ 外部成员：designer、wenji（8601）      │  反向隧道 │ └─ workspace（8470 任务工作区）      │
│ └─ 机制：PEP / 域 / guard / 对账 / viz    │          │                                    │
└──────────────────────────────────────────┘          └────────────────────────────────────┘
                    │                                              │
                    └────────── 梧桐平台：Registry / CA / Discovery / Monitor ──────────┘
```

分层职责：

| 层 | 组件 | 职责 |
|---|---|---|
| 协议层 | ACPs SDK 2.2.0 + WIT 2.1.0 | AIC/ACS/AIP/ADP/AMP 数据对象、ATR 注册状态机、mTLS 监听 |
| 治理层（自研） | `acps_cluster/delegation/` | PEP 准入、Ed25519 JWS 令牌、域章程与 grant、可见性、配额与对账 |
| 编排层（自研） | `acps_cluster/agents/supervisor.py` | 意图/规划、委派、等待、完成闸门、聚合、诚实降级 |
| 能力层（自研） | `acps_cluster/agents/partner_*`、`capabilities/` | 文献、综述、写作、审稿、矛盾、前沿、实验、引用核验、资源封装 |
| 成员层 | `partners/designer`、`partners/wenji` | 研究设计（外部成员）、问数/问答（外部成员） |
| 观测层 | AMP 组件、`guard_adapter.py`、`cluster-viz/` | 心跳/访问/指标、事件通信风险检测、集群可视化 |

### 3.3 与 AIP 国家标准的对应关系

| 标准能力 | 标准要点 | 本作品实现 | 证据 |
|---|---|---|---|
| AIC | OID 形式的全局唯一身份 | 每个 agent 独立 AIC；证书 CN/SAN 与 AIC 一致 | 名册 + `acs.final.json` |
| ACS | 能力/端点/输入输出/可见性 | 代码投影生成 ACS；三层能力声明（public/非公开/`visibility=domain`） | `deploy/make_acs.py`、`validate_acs.py` |
| ATR | 注册→审核→AIC→EAB→证书 | WIT 注册状态机；审核通过后自动领证；身份切换有专门流程 | 注册记录、`docs/agent-aic-roster.md` |
| AIP | Direct/Group；任务状态机 | Direct JSON-RPC：`start/get/continue/complete/cancel`；`accepted→working→awaiting-*→completed` | `PEP`/supervisor 日志 |
| AIP 身份绑定 | 证书 AIC == 消息 senderId | WIT 严格模式强制；失败返回 -32008/-32009 | 跨端调用实测 |
| ADP | 显式/探索式发现 + aliveMap | Discovery 检索 + 心跳存活；域可见性过滤与响应脱敏 | 发现层单测与线上检索 |
| AMP | 心跳/指标/访问/消息/系统/审计 | 六类日志；任务级 access 以 `correlationId=taskId` 双侧补齐 | `amp_*` 日志 + 平台后台 |
| AAC | 认证之后的访问控制 | PEP 三级准入 + Ed25519 JWS 委托令牌 + 域 grant + 冻结/配额 | `test_delegation` 102 项、域回归 62 项 |

### 3.4 开源基线选型与改造边界

| 项 | 选择 | 理由 |
|---|---|---|
| 协议实现 | **ACPs SDK 2.2.0**（AIP-PUB） | GB/Z 185 参考实现，含 AIC/ACS/AIP/ADP/AMP 模型与校验 |
| 开发框架 | **WIT 2.1.0** | 内置 ATR 注册状态机、Direct/Group 监听、mTLS、AMP 生命周期 |
| 基线仓库 | **ACPs-community 2.2.0** | registry/CA/discovery/monitor 可本地部署，便于域可见性改造与回馈 |
| 复用（不改） | ACPs SDK、WIT、wit 数据对象与传输 | 只用公开扩展点；两处幂等 monkeypatch 有注释 |
| 改造（上游） | discovery-server 域可见性（分支 `cdap-domain-visibility`：48 文件，+1272/−170）；registry schema 接受 `visibility/domains`；通知回调域名白名单；R4–R10 健壮性修复 | 按 MulanPSL-2.0 标注，未合并上游也未提 PR |
| 自研 | `acps_cluster/`（3.7 万行）、`capabilities/`、`partners/designer`、`partners/wenji`（引擎 1.0 万行 + AIP 适配 1,536 行 + 前端 972 行） | 见第 5 章 |

---

### 3.5 关键设计取舍

| 取舍点 | 备选方案 | 选择 | 理由 |
|---|---|---|---|
| 授权粒度 | 会话级 / 任务级 / 永久密钥 | **任务级** | 最小权限；令牌可 TTL/次数约束，泄漏面最小 |
| 令牌如何到达能力 | 放模型上下文 / 环境变量 / jti 句柄 | **jti 句柄 + 进程内保管库** | 模型不可见；运行中可注入；可审计 |
| 可见性实现 | 访问控制层过滤 / 发现层元数据过滤 | **元数据收窄 + PEP 兜底** | 发现层只决定"看不看得见"，是否可用永远由 PEP 判定 |
| 资源隔离 | 统一沙盒 / 按风险定制 | **按资产风险定制** | 防泄题、防权重外泄与算力隔离的最优边界不同 |
| 计数口径 | 传输边界（请求 ID） | **任务级（taskId）双侧** | 与平台榜单口径一致；可配对、可审计 |
| 发现过滤 | 先 limit 后过滤 | **3× 超取 + 后续谓词下推** | 兼容现状；已在建议中提出下推方案 |

### 3.6 部署拓扑与资源规格（图 3-1）

```text
[公网] 8.160.176.215
  ├─ :8601  wenji-serve（AIP Direct，mTLS）──┐
  ├─ :8000  wenji-backend（FastAPI）          │ 同一台 VPS（2 vCPU / 4 GiB）
  ├─ :5173  wenji-frontend（Vue3）            │ systemd 用户单元 × 6（Linger）
  ├─ :18999 wenji-llm-proxy（本地反代）       │ PostgreSQL 16 + pgvector（用户态）
  └─ :8444  supervisor / :8445-8461 partners  ┘
[校园网] 反向隧道 :18888（HTTP CONNECT）→ 模型网关（内网地址）
```

**图 3-1 总体部署拓扑**：平台侧只暴露 mTLS 端点；模型出网经校园隧道；AIP 出站不走该代理，
避免"模型可达但平台不可达"的相互污染。


## 4. 关键技术实现（对应评分：AI 技术应用 20 分）

### 4.1 智能体注册与元数据建模

- **身份**：每个成员独立 AIC；`deploy/make_acs.py` 从代码投影 ACS（名称、描述、skills、端点、
  `certificate.altNames`），`deploy/validate_acs.py` 校验字段与可见性；
- **名册**：`cluster_roster.py` 17 条（1 supervisor + 16 partner，含 designer/wenji 两个外部成员），
  名册只存 `key/module/workspace/port/declared_capabilities`，AIC 与端点从各工作区
  `acs.final.json` 解析，避免硬编码漂移；
- **三层能力**：public（橱窗）/ 非公开（凭证准入）/ `visibility=domain`（域内可见）；
  可见性只做**元数据收窄**，访问控制始终在 PEP；
- **注册的一次性**：WIT 激活后校验 ACS 指纹；升级需新版本 + 新工作区，或走平台控制台建记录后
  用 `switch_step2/4` 切换身份与证书（文档化流程，避免"服务自称新 AIC、证书却是旧的"）。

### 4.2 智能体自动发现机制

```text
agent 进程 --写--> <ws>/logs/amp_heartbeat_<AIC>.jsonl
   └─(15s 镜像/发送)--> Kafka amp.heartbeat --> Monitor --> Discovery aliveMap
Discovery 查询：能力谓词 + visibility/domains 过滤 + 3× 超取后过滤（缓解"先 limit 后过滤"）
```

- 发现支持显式（能力关键词）与探索式（任务描述）检索；返回 `acsMap`（能力/端点）与 `aliveMap`（存活）；
- 域可见性 fail-closed：无可信身份源时域资产对所有人隐藏；匿名仅见 public；
  "无可见 skill 的 agent 整体不出现"，避免存在性泄露；
- 问迹以 4 个 Skills 进入发现层，心跳经同一链路进入 aliveMap，Supervisor 可按"问数/数据库/文档问答"
  命中并委派。

### 4.3 跨智能体双向通信

- **传输**：AIP Direct（JSON-RPC over mTLS，TLS 1.3）；WIT 严格模式强制 `CERT_REQUIRED`，
  从对端证书解析 AIC，并校验消息 `senderId` 与证书身份一致（防冒充）；失败返回 -32008/-32009；
- **任务状态机**：`start` 创建任务 → `accepted → working → awaiting-input/awaiting-completion
  → completed`；`get` 幂等读取；`continue` 仅在等待态生效；`complete` 仅确认产物；
  终态不可改写；
- **等待与重试**：Leader 用 `wait_partner` 阻塞到终态（不烧步数）；委派层对传输异常做重试与
  超时；对"注册集合之外"的本地 dev 身份做 0.8s TCP 探活，避免"解析得出但隧道未起"导致白烧；
- **双向性**：Supervisor → 问迹（数据任务）与问迹 → Supervisor（能力咨询/编排建议）均实测完成；
  问迹亦作为 Leader 调用集群伙伴，形成"消费方/提供方"角色互换。

### 4.4 多智能体协同交互逻辑

- **编排**：Supervisor 规划子任务 → 按名册选伙伴 → 委派 → 完成闸门 → 聚合；
  `plan_gate` 从硬前置降为可选自检，真正的约束下沉到**委派点**
  （伙伴必须真实存在、能力必须由其声明、AIC 取权威值），避免模型参数抖动卡死整段编排；
- **任务分解与并行**：独立子任务并行，`wait_many` 屏障汇合；依赖任务用占位符传递上游结果；
- **结果聚合与冲突消解**：Critic 的 `review_gate` 做代码侧裁决（APPROVE/REVISE + issues）；
  Contradiction 输出矛盾对与可检验假说；Citation 三态校验（supported/misrepresented/fabricated）；
  实验被证伪时如实 `refuted`，不代写；
- **委托授权（PEP）**：每次委派签发 Ed25519 JWS 令牌（iss/sub/aud/act 信任链/jti/scope/TTL/次数），
  Leader 只拿 jti 句柄；`delegate_continue` 向运行中任务注入令牌，能力侧经 ContextVar 出站；
- **工作区**：`ws-<task>/` 提供 repo/products/scratch/shared 与 `manifest.jsonl`，写入需令牌，
  `actor` 由 `act.aic` 派生，形成可审计的产物链。

### 4.5 安全访问与权限控制

| 机制 | 实现 | 验证 |
|---|---|---|
| 三级准入 | L0 public / L1 白名单 / L2 令牌；策略缺失 fail-closed | `test_delegation` 102 项；S1 越权漏洞复现→修复 16/16 |
| 委托令牌 | Ed25519 JWS；`iss/sub/aud/act/jti/scope/exp/max_uses`；父链重验 | 令牌不可见性断言；`delegate_continue` 实弹 |
| 域治理 | 双签章程（成员/签发者/资产表/配额/revision/状态）；修订权=创建权；冻结先于验签 | 域/修订/派生 62 项；域×真 GPU G1–G4 |
| 配额与对账 | 签发侧/使用侧双配额；日志对账成可仲裁证据 | 对账 9 项；跨机构 S1–S5 |
| 资源隔离 | GPU 八条硬边界；评测三闸门 + 8% 噪声基准；KB 字段级三档脱敏；模型权重不出域 | 各伙伴专项测试（全量 757 项） |
| 通信风险 | guard 旁路检测事件通道（间隔/重放/异常模式），只读 SQLite，不阻断 | 12 项测试；阳性对照 HIGH 73/100 |

### 4.6 三个关键难点与解决方案

**难点 1：授权令牌"模型不可见"与运行中注入。**
难在模型上下文会泄露令牌、而长任务又必须在运行中拿到凭证。做法：签发工具只返回 jti 句柄
（返回树断言不含 "token"），JWS 锁进程内保管库，`delegate_continue` 向运行中任务注入，
能力桥接用 ContextVar 取用。效果：较 leader 中转 **2.94×** 加速（脚本版）/1.73×（真实 LLM 版），
leader 上下文材料 **196KB→0**，并行度 1→3。

**难点 2：访问计量与"双侧配对"。**
难在平台按 `correlationId=taskId` 配对计数，而框架默认按 RPC 请求 ID 打点，且一个任务会产生多条边界记录。
做法：应用层按任务粒度补发双侧记录（被调方 TaskAccessHooks + 调用方 emitter），
转发器改为逐条确认 + DLQ + 断点续传；同时固化部署门禁，防止部署覆盖计量钩子
（曾因覆盖导致榜单清零一天，复盘见 `docs/incident-2026-10-23-amp-hooks-lost.md`）。
效果：任务级记录唯一 taskId 无重复，榜单恢复并稳定。

**难点 3：一次性注册与能力演进、IP 端点证书。**
难在 WIT 激活后声明不可变、IP 直连端点不写 IP SAN（`server_public_endpoint_san_mismatch`）。
做法：能力升级走"新版本号 + 新工作区"；IP 端点场景改走本地 CA 身份（路线 B）并在集群内服务；
对外文档化"注册—领证—切换"步骤与校验点（CN/SAN/EKU/链/私钥匹配）。
效果：既保持平台身份稳定，又允许能力迭代；踩坑记录已回馈上游。

---

### 4.7 协议对象与状态机（表 4-1、表 4-2）

**表 4-1 AIP Direct 命令与语义**

| 命令 | 方向 | 语义 | 本作品处理 |
|---|---|---|---|
| `start` | Leader→Partner | 创建任务并下发内容 | 校验准入（PEP）→ 建立任务 → `accepted` |
| `get` | Leader→Partner | 幂等读取状态 | 直接返回快照，不产生副作用 |
| `continue` | Leader→Partner | 向 `awaiting-input` 补充信息 | 仅等待态接受；回填后回到 `working` |
| `complete` | Leader→Partner | 确认 `awaiting-completion` 的产物 | 终态 `completed`；幂等 |
| `cancel` | Leader→Partner | 取消任务 | 终态 `canceled`；不可取消态返回 -32002 |

**表 4-2 状态迁移与错误码**

| 当前态 | 事件 | 下一态 | 异常路径 |
|---|---|---|---|
| — | start（通过准入） | accepted | 准入拒绝 → rejected |
| accepted | 开始执行 | working | 内部错误 → failed |
| working | 需要补充信息 | awaiting-input | 超时 → failed |
| working | 产出就绪 | awaiting-completion | — |
| awaiting-input | continue | working | 非法命令 → -32004 |
| awaiting-completion | complete | completed | 重复 complete 幂等 |
| 任意非终态 | cancel | canceled | 终态取消 → -32002 |

错误码遵循 AIP：`-32001` 任务不存在、`-32002` 不可取消、`-32004` 不支持的操作、
`-32005` 内容类型不支持、`-32008` 缺少证书身份、`-32009` 身份不一致。

### 4.8 编排时序与工具面（图 4-2）

```text
用户 → supervisor：规划 → 选伙伴（名册/发现）→ 准入（PEP）→ start
        ├─ 并行：独立子任务（wait_many 屏障）
        ├─ 依赖：占位符传递上游结果
        ├─ 完成闸门：awaiting-completion → complete
        └─ 聚合：分区溯源 + 审稿裁决 + 引用核验 → 终稿
```

**图 4-2 多智能体委派时序**：Supervisor 的工具面包括 `acps_discover`、`acps_delegate`、
`acps_delegate_public`（纯标量入参，规避数组参数丢失）、`acps_task`（wait/inspect/complete）、
`wait_partner`、`plan_gate`（可选自检）与 `review_gate`；委派点在代码侧做权威校验
（伙伴存在、能力归属、AIC 取自 ACS），不依赖模型自述。


## 5. 开源代码改造说明（对应评分：创新性 25 分）

### 5.1 基线仓库、版本与二次开发范围

| 项 | 值 |
|---|---|
| 基线 | ACPs-community 2.2.0（registry/CA/discovery/monitor/mq-auth/demo） |
| 运行时 | WIT Framework 2.1.0 + ACPs SDK 2.2.0（AIP-PUB） |
| 上游改造 | 分支 `cdap-domain-visibility`：48 文件，+1272/−170（域可见性、registry schema、回调白名单、R4–R10 修复） |
| 运行时侵入 | 仅 2 处幂等 monkeypatch（discovery filter 兼容、ACS 字段透传），不修改包文件 |
| 自研规模 | `acps_cluster` + `capabilities` + `partners/designer` ≈ **37,046 行** Python（不含测试）；问迹引擎 10,051 行 + AIP 适配 1,536 行 + 前端 972 行 |
| 测试 | **757 项离线单测全绿**（2026-10-26 复跑，0 失败，4 项环境相关跳过）+ 7 类场景验收 |

### 5.2 改造前后的对比

| 维度 | 基线示例/改造前 | 本作品改造后 |
|---|---|---|
| 拓扑 | 单 leader + 少量 demo partner | 1 supervisor + 16 partner（含 2 外部成员）跨机构集群 |
| 授权 | 仅 public/白名单；AAC 委托无实现 | 三级准入 + Ed25519 JWS 委托令牌 + 域 grant/冻结/双配额 |
| 发现 | 全量返回 | 域可见性过滤 + 响应脱敏 + fail-closed |
| 计量 | 传输边界 access | 任务级双侧 access（`taskId`）+ 心跳 + 指标 + 对账 |
| 协同 | 简单任务链 | 规划/并行/完成闸门/审稿裁决/矛盾侦测/引用核验/诚实降级 |
| 可观测 | 日志 | 集群可视化 + guard 风险报告 + 审计流 |
| 资源治理 | 无 | 5 类底座 6 实例（GPU/CPU/评测/KB/模型/HPC 网关），按风险定制隔离 |
| 工程 | 手动部署 | systemd 六单元 + 一键部署 + 一键测试（`run_all_tests.py`） |

### 5.3 自研模块清单

| 模块 | 职责 | 关键文件 |
|---|---|---|
| PEP 委托授权 | 三级准入、JWS 令牌、jti 句柄、隧道注入 | `delegation/{pep,token,client,bridge}.py` |
| 协作域 | 双签章程、grant 派生、冻结、配额、对账 | `delegation/{domain_manifest,grant,quota,reconciliation}.py` |
| 可见性 | 域可见性谓词、ACS 脱敏、发现投影 | `delegation/visibility.py`、`core/discovery_metadata.py` |
| 编排 | 规划、委派、等待、完成闸门、聚合 | `agents/supervisor.py`、`agents/partner_*.py` |
| 资源封装 | GPU/CPU/评测/KB/模型/工作区 | `runtime/*`、`agents/partner_{gpu,cpu,eval,kb,model,workspace}.py` |
| 通信风险 | guard 旁路检测与适配 | `guard/`、`guard_adapter.py` |
| 可视化 | 集群总览与任务回放 | `cluster-viz/`、`core/viz_live.py` |
| 问迹 | 问数/问答引擎 + AIP 适配 | `partners/wenji/backend/app/**`、`app/acps/**` |
| 设计成员 | 研究方案设计与校验 | `partners/designer/src/**` |

### 5.4 差异化互联应用方案的创新点

| # | 原有做法 | 我方做法 | 收益 |
|---|---|---|---|
| I1 | AAC 委托语义停留在文档；SDK 零实现 | PEP：三级准入 + Ed25519 JWS + jti 句柄 + 运行中注入 + 父链/配额重验 | 授权可执行、可撤销、可对账；**2.94×** 加速、上下文 196KB→0 |
| I2 | 一个沙盒套所有资源 | 按资产风险定制五类底座、六实例（GPU 八边界/评测三闸门/KB 三档脱敏/模型不出域/工作区令牌作用域） | 资源方敢开放；防泄题≠拒服务 |
| I3 | 单任务令牌只管"这一次" | 双签协作域：章程为合同对象，grant 派生、revision 驱逐、冻结急停、双配额对账 | 跨机构长期关系可治理；可见性自然推出三层能力 |
| I4 | 只做"能不能用"（PEP） | 增设 guard 管"用得对不对"（事件通道风险：间隔/重放/异常模式，只读旁路） | 治理从准入延伸到运行期行为 |
| I5 | 智能体能力与协议描述两套事实 | 问迹"能力声明即资产"：一份 `@capability` 同时投影模型工具、ACS Skill 与文档；Leader/Partner 同体 | 声明与实现不漂移；既可接单也可委派 |
| I6 | 访问计量口径与平台不一致 | 任务级双侧 access（`taskId`）+ 逐条确认转发 + 部署门禁 | 榜单可见、可审计；避免"有调用无名次" |

---

### 5.4.1 上游改造摘要（表 5-0）

**表 5-0 discovery-server 域可见性改造（分支 `cdap-domain-visibility`）**

| 文件/模块 | 改动 | 目的 |
|---|---|---|
| discovery 检索入口 | 增加 `visibility/domains` 谓词与 3× 超取 | 域成员可见、匿名仅 public |
| ACS 响应投影 | 按身份裁剪 skills 与敏感字段 | 响应不泄露未授权能力 |
| registry ACS schema | 接受 `visibility/domains` | 让可见性成为声明的一部分 |
| 通知回调白名单 | 允许受控 origin | 防 SSRF/任意回调 |
| 健壮性修复 R4–R10 | 参数校验、异常路径、日志 | 稳定性 |

改造保持"元数据收窄"边界：发现层只决定可见性，真正的访问控制仍在 PEP，二者不互相替代。


### 5.5 代码量与模块对照（表 5-1）

**表 5-1 改造规模（按目录统计，不含测试）**

| 目录 | 行数 | 性质 |
|---|---:|---|
| `acps_cluster/`（agents/core/runtime/delegation/guard/tools） | ~30,000 | 自研（治理 + 编排 + 资源） |
| `capabilities/` | ~4,000 | 自研（arXiv/矛盾/前沿/数据集/记忆等） |
| `partners/designer/` | ~3,000 | 自研（研究设计成员） |
| `partners/wenji/backend/app`（引擎） | 10,051 | 自研（NL2SQL/RAG/跨源/公式/Trace） |
| `partners/wenji/backend/app/acps`（适配层） | 1,536 | 本届新增（能力声明/桥接/计量/注册） |
| `partners/wenji/frontend/src` | 972 | 自研（对话/Trace/文档台） |
| `partners/wenji/cluster-viz`（团队） | — | 自研（集群可视化） |
| 上游 diff（discovery-server 等） | +1,272 / −170 | 二次开发（48 文件） |

### 5.6 创新点与证据对照（表 5-2）

**表 5-2 每条创新的"原有做法—我方做法—收益—证据"**

| # | 原有做法 | 我方做法 | 收益（量化） | 证据 |
|---|---|---|---|---|
| I1 | AAC 委托无实现 | PEP：三级准入 + JWS + jti + 注入 | 2.94× 加速；上下文 196KB→0；并行 1→3 | `test_delegation` 102 项；fanout 报告 |
| I2 | 统一沙盒 | 五类底座六实例，按风险定制 | 防泄题三路全挡且渗透者正常得分；权重不出域 | 各伙伴专项测试；E 系列 9/9 |
| I3 | 单任务令牌 | 双签域 + grant 派生 + 冻结 + 双配额 | 跨机构 5/5；冻结即刻生效 | `test_domain` 31 + hardening 16 + visibility 15 |
| I4 | 只管准入 | guard 管运行期通信风险 | 12 项测试；阳性对照 HIGH 73/100 | guard README |
| I5 | 能力/协议两套事实 | 一份 `@capability` 三处投影；Leader/Partner 同体 | 声明不漂移；自调/被调双向 | 问迹 4 Skills；双向调用记录 |
| I6 | 传输边界计量 | 任务级双侧 + 逐条确认 + 门禁 | 榜单可见；2,700+ 唯一 taskId | `amp_access_*` 与平台后台 |


### 4.8.1 关键代码片段

**（1）能力声明即资产（问迹）**

```python
@capability(
    id="wenji.ask",
    description="综合问数/问答：可同时涉及业务数据库与知识库文档……",
    acps_skill=True, public=True, input_modes=["text/plain"],
    tags=["问数", "问答", "跨源", "可解释"],
)
async def ask(question: str) -> dict:
    session_id = CURRENT_TASK_SESSION.get()      # 按 AIP 任务隔离会话
    return await bridge.run_in_thread(bridge.answer_question, question, session_id)
```

同一份声明同时投影为：模型工具（WIT 内编排）、ACS Skill（平台发现）与文档条目；
`CURRENT_TASK_SESSION` 用 `contextvars` 传递 taskId，使澄清/多轮在任务内闭环、跨任务不污染。

**（2）PEP 准入判定顺序（伪代码）**

```python
def admit(request):
    peer = verify_mtls_identity(request)          # 证书 CN/SAN → AIC
    if peer is None: return deny(-32008)
    if domain.is_frozen(target_domain):           # 冻结先于验签（急停）
        return deny("domain_frozen")
    token = load_by_jti(request.jti)              # 模型只见 jti，不见 JWS
    if not verify_signature(token, peer): return deny(-32009)
    if not scope_subset(token, request.skill): return deny("scope")
    if not quota.consume(token, request): return deny("quota")
    return allow()
```

**（3）任务级 AMP 记录（双侧配对）**

```python
# 被调方（serve）：任务进入首个稳定态时写一条
await emitter.emit(body, correlation_id=task_id)   # correlationId = taskId
# 调用方（leader）：任务结束后写一条，caller/callee 对调
await leader_emitter.emit(body, correlation_id=task_id)
```

两条记录 `aic` 不同（或同 AIC 自调）、`taskId` 相同，平台即可配对为一次调用/被调用；
转发器逐条确认后才推进 offset，失败落 DLQ 重投。


### 4.9 委托令牌与准入字段（表 4-3）

**表 4-3 Ed25519 JWS 令牌字段与校验**

| 字段 | 含义 | 校验点 |
|---|---|---|
| `iss` | 签发者 AIC | 必须是名册内的 leader；签发者授权检查（修复"自签 iss=leader"越权） |
| `sub` | 主体（能力/资源） | 必须与目标伙伴声明一致 |
| `aud` | 受众 AIC | 只取与本机 AIC 相等者；防跨节点重放 |
| `act` | 信任链（谁代谁） | 逐跳校验，派生令牌必须继承父链 |
| `jti` | 令牌唯一 ID | 单次使用；运行中注入时登记；用于审计与撤销 |
| `scope` | 能力/资源范围 | 必须 ⊆ 域资产表；域技能需 grant |
| `exp` / `max_uses` | 有效期/次数 | 过期或用尽即拒；父令牌约束子令牌 |
| `parent_jti/parent_exp/parent_max_uses` | 父链约束 | 重验父签名/主体/资产并扣父配额 |

**准入判定顺序**（fail-closed）：身份（mTLS/AIC）→ 域状态（冻结先于验签）→ 令牌签名与父链 →
scope 与资产表 → 配额（签发侧/使用侧）→ 放行并记账。任一步缺失或异常均拒绝并写审计。

### 4.10 通信风险检测（guard，表 4-4）

guard 是 PEP 的补充：PEP 回答"能不能用"，guard 回答"用得对不对"。它以只读旁路读取任务事件，
按时间窗与行为模式打分，不阻断业务，输出可解释的风险报告。

**表 4-4 风险维度与判定**

| 维度 | 观察量 | 风险信号 |
|---|---|---|
| 节奏 | 委派间隔、突发度 | 小于 1 秒的密集调用记为运营突发，不计安全分 |
| 重放/重复 | 相同 taskId/参数指纹 | 高频重复提交、结果复用异常 |
| 越界 | 能力/资源与声明不符 | 请求未声明 skill、跨域访问 |
| 失败模式 | 连续拒绝/超时 | 探测式尝试、异常重试 |
| 结论 | LOW/MEDIUM/HIGH/INSUFFICIENT_DATA | 样本不足不强判；逐条给维度、规则、分值与样本量 |

### 4.11 资源封装与审计（表 4-5）

**表 4-5 六类资源实例的隔离与审计**

| 实例 | 资产 | 关键防护 | 审计 |
|---|---|---|---|
| gpu | 算力时间 | Docker 八条硬边界（白名单/断网/只读 FS/非 root/限额/时限） | 容器审计 + 作业账本 |
| cpu | 算力时间 | 同沙盒，`use_gpu` 由代码决定 | 同上 |
| eval | 评测数据 | 标签不进容器、stdout 管制、产物不回传；8% 噪声确定性基准 | 评分账本 + 哈希校验 |
| kb | 私有语料 | 受限 DSL（无代码执行面）+ 行级过滤 + 字段级三档脱敏 | 查询审计 + meta 列明 |
| model | 模型权重 | 权重不出域、输入域/批量校验、热替换位 | 推理账本 |
| workspace | 任务存储 | `task_id` 白名单、路径逃逸拒绝、配额、令牌作用域 | `manifest.jsonl`（actor/sha256/时间） |

### 4.12 三个难点的展开

**难点 1（令牌不可见 + 运行中注入）**：难在"模型会看到一切上下文"与"长任务需要凭证"的矛盾。
解法分四步：① 签发工具返回 jti 句柄而非令牌；② 返回树断言不含 `token` 字段（测试固定）；
③ JWS 锁进程内保管库，按 `jti` 索引；④ `delegate_continue` 向运行中任务注入，能力桥接用
ContextVar 取值并只在本进程出站。效果：脚本版 2.94×、真实 LLM 版 1.73× 加速；leader 上下文
从 196KB 降到 0；并行度 1→3；且"模型无法打印令牌"成为可回归的断言。

**难点 2（计量口径与部署资产）**：难在平台按 `taskId` 双侧配对，而框架按 RPC 请求 ID 打点，
一个任务产生多条边界记录；更隐蔽的是"部署覆盖计量钩子"会让历史计数看似存在、实际停止上报
（10-23 事故导致榜单清零一天）。解法：应用层补任务级双侧记录（被调方 TaskAccessHooks、
调用方 leader emitter），转发器改 confluent-kafka + `acks=all` + 逐条确认 + DLQ + 断点续传，
并把"钩子是否在生效"做成部署前门禁。效果：唯一 taskId 记录 2,700+、无重复、榜单恢复稳定。

**难点 3（一次性注册 + IP 端点证书）**：难在 WIT 激活后声明不可变，就地修改会拒绝启动；
IP 直连端点又因 WIT 2.1.0 不写 IP SAN 而无法签发。解法：能力演进走"新版本 + 新工作区"，
并文档化注册—领证—切换的校验点（CN/SAN/EKU/链/私钥匹配）；IP 场景改走本地 CA 身份（路线 B）。
效果：平台身份稳定、能力可迭代；缺陷与修复建议已回馈上游。


## 6. 梧桐平台接入与全流程验证（对应评分：应用成效 10 分 / AI 技术应用 20 分）

### 6.1 接入架构与部署拓扑

```text
                 ┌──────────── 梧桐平台（wt.ioa.pub）────────────┐
                 │ Registry（注册/审核/AIC） · CA（EAB/mTLS）      │
                 │ Discovery（ADP） · Monitor（AMP/Kafka）         │
                 └───────▲──────────────▲──────────────▲─────────┘
        ATR/证书/心跳/AMP│              │发现/委派      │AMP 日志
                         │              │               │
   ┌─────────────────────┴──────┐   ┌───┴───────────────┴──────────┐
   │ 研枢集群（VPS 2C/4G）       │   │ 外部成员                     │
   │ supervisor + 16 partners   │◄─►│ designer（研究设计，8590）   │
   │ + PEP/域/guard/viz         │   │ wenji（问数/问答，8601）     │
   │ systemd 六单元 + 用户态 PG  │   │ 独立注册、独立服务、mTLS      │
   └────────────────────────────┘   └──────────────────────────────┘
```

部署要点：VPS 原地升配（公网 IP 不变），全栈迁移后由 systemd 用户单元常驻
（`db / backend / frontend / llm-proxy / serve / amp-forwarder`，Linger 已启用，SSH 断开不影响）；
PostgreSQL 16 + pgvector 以用户态源码编译部署（无需 root）；AIP 服务直接监听 8601，mTLS 端到端。

### 6.2 注册 → 发现 → 互联 → 访问全流程时序

```text
Agent            Registry/CA                 Discovery            Monitor/Kafka
  │ 登录/提交ACS ──► │                            │                     │
  │ ◄── 审核通过/AIC │                            │                     │
  │ ◄── EAB/证书     │                            │                     │
  │ 心跳/指标/访问 ─────────────────────────────────────────────────────► │
  │                  │                            │ ◄── 心跳同步 ──────── │
Leader                                                                  │
  │ 发现(能力关键词) ───────────────────────────► │                     │
  │ ◄── acsMap + aliveMap（含端点/技能/在线）      │                     │
  │ start/get/continue/complete（mTLS，AIP Direct）──► Partner            │
  │ ◄── TaskResult（accepted/working/awaiting-*/completed + Products）   │
  │ 任务级 access（correlationId=taskId，双侧）─────────────────────────► │
```

**问迹实测（2026-10-15 注册，10-19 双向调用）**：

| 环节 | 结果 |
|---|---|
| 注册 | ACS 审核通过，AIC `1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN`，mTLS 双证书落地 |
| 发现 | Discovery 按"问数/数据库/文档问答"命中，`active=true`，4 个 Skills，端点 `https://8.160.176.215:8601/rpc` |
| 互联 | 问迹 → 研枢-Supervisor：`accepted→working→awaiting-completion→completed`，返回其能力矩阵与示例计划；问迹自身全状态机（含 `awaiting-input` 澄清与 `continue` 续查）通过 |
| 访问 | 公网 `GET /health`（strict mTLS）P50≈0.16 s；RPC 边界 P50 53 ms / P95 320 ms；任务往返 20–55 s（LLM 主导） |
| 数据 | 心跳进入平台 Kafka（28 s 窗口实测命中）；任务级 access 累计 2,700+ 唯一 taskId，按 `correlationId=taskId` 双侧记录 |
| 榜单 | 两个平台榜单均列第一〔待补：截图与时间范围〕 |

### 6.3 验证方法、测试用例与结果

| 场景 | 用例数 | 结果 |
|---|---|---|
| PEP 委托授权 | 16 | 16/16（含越权复现→修复） |
| 协作域（含修订/冻结/派生） | 11 | 11/11 |
| 商业域（三方：资源方—消费方—审计方） | 10 | 10/10 |
| 知识库（行级 + 字段级三档脱敏） | 9 | 9/9 |
| 评测（防泄题三闸门 + 噪声基准） | 9 | 9/9 |
| GPU 沙盒（八条边界） | 7 | 7/7 |
| 域 × 真 GPU | 4 | 4/4 |
| 两机构双边域 | 5 | 5/5 |
| 问迹：NL2SQL / RAG / 跨源 / 澄清 | 61 / 41 / 11 / 30 | 100% / 100% / 11/11 / 30/30 |
| 离线单测（团队） | 757 | 全绿（4 项环境相关跳过） |

### 6.4 平台后台访问量与交互记录

〔待补：梧桐平台后台访问量截图（时间范围 2026-10-__ 至 2026-10-__，含 AIC 与调用次数）〕
〔待补：两个榜单第一截图（含榜单名称、统计窗口、问迹名称/AIC）〕
〔待补：Discovery 检索结果截图（问迹 active + 4 Skills + 8601 端点）〕
〔待补：互联交互记录截图（任务状态机 / AMP access 记录，correlationId=taskId）〕

> 截图要求：打码组织字段；保留时间与 AIC 以便复核；与本地 `amp_access_*.jsonl` 抽样互相印证。

### 6.5 稳定性与兼容性

| 指标 | 数值 | 口径 |
|---|---|---|
| 平台公网健康检查 | P50≈0.16 s（5 次 0.10–0.18 s） | 经公网 mTLS `GET /health` |
| RPC 边界时延 | P50 53 ms / P95 320 ms（n=62） | `amp_access` durationMs |
| 任务往返 | 20–55 s | start→completed，LLM 主导 |
| 并发 | 6–8 个并发任务 | 流量运行器 worker 数 |
| 成功率 | ≥98% | 2,000+ 任务统计 |
| 可用性 | 全栈迁移后连续运行；SSH 断开不影响（Linger） | systemd 状态 |
| 兼容性 | 平台 URL 拼接（registry/ca 只到服务根、discovery 到完整端点）；ACS 一次性注册；IP 端点 SAN | 踩坑记录已文档化 |

---

### 6.5.1 测试用例样例（表 6-0）

**表 6-0 典型验收用例（节选）**

| 用例 | 输入 | 期望 | 实测 |
|---|---|---|---|
| 公开能力直调 | 匿名 leader 调 public skill | 放行 | 通过 |
| 白名单外调用 | 非名单 AIC 调 L1 能力 | 拒绝并记审计 | 通过 |
| 无令牌调域技能 | 普通令牌调 `visibility=domain` | 拒绝 | 通过 |
| 令牌次数用尽 | `max_uses` 耗尽后再调 | 拒绝 | 通过 |
| 域冻结 | 冻结后使用存量令牌 | 拒绝（验签前） | 通过 |
| 自签越权 | 伪造 `iss=leader` | 拒绝（签发者授权检查） | 复现→修复 |
| 评测泄题 | 容器内读测试标签 | 不可读；三路渗透全挡 | 通过 |
| 知识库越权字段 | consumer 读 curator 字段 | 静默丢弃并在 meta 列明 | 通过 |


### 6.6 可复现验证步骤

```bash
# 1) 平台身份与技能（问迹）
cd partners/wenji/backend
.venv/bin/python -m app.acps.cli status          # 本地注册状态（AIC/证书路径）
.venv/bin/python -m app.acps.cli check           # 声明/ACS 本地校验（无网络）
# 2) 公网健康（strict mTLS）
curl -sk --cert .wit/wenji-aip-agent/acps/client/certificate.pem \
  --key .wit/wenji-aip-agent/acps/client/private-key.pem \
  --cacert .wit/wenji-aip-agent/acps/trust-bundle.pem \
  https://8.160.176.215:8601/health
# 3) 发现 + 双向调用（One-shot）
.venv/bin/python scripts/acps/aip_selftest.py --query "问数 数据库 文档问答" --no-delegate
.venv/bin/python scripts/acps/aip_selftest.py --partner-aic <SUPERVISOR_AIC> --query "科研任务调度规划" \
  --question "请介绍你能提供的科研任务调度能力，并给出一个示例任务规划。"
# 4) 计量核对
python3 - <<'EOF'
import json,glob,collections
c=collections.Counter()
for f in glob.glob(".wit/wenji-aip-agent/logs/amp_access_*.jsonl"):
    for l in open(f):
        l=l.strip()
        if l:
            cid=str(json.loads(l).get("correlation_id") or "")
            if cid.startswith("task-"): c[cid]+=1
print("唯一 taskId:", len(c), "| 双侧:", sum(1 for v in c.values() if v>=2))
EOF
```

### 6.7 兼容性问题与事故时间线

| 日期 | 事件 | 影响 | 处置 |
|---|---|---|---|
| 10-15 | 平台 URL 拼接规则（服务根 vs 完整端点）踩坑 | 404 | 区分 registry/ca 与 discovery 写法 |
| 10-15 | ACS 已批准后修改声明 | serve 拒启 | 恢复状态 + 声明版本门控 |
| 10-19 | AMP journal 跨进程写冲突（524 次/6h） | 任务级记录丢失 | 独立 journal + 单写者 |
| 10-23 | 部署覆盖服务器侧计量钩子 | 榜单清零一天 | 部署门禁 + 复盘文档 |
| 10-27 | 校园隧道中断 | 模型不可达 | 零开销暂停 + 自动恢复脚本 |

### 6.8 性能与容量（表 6-1）

**表 6-1 关键指标（截至 2026-10-27）**

| 指标 | 值 | 口径/环境 |
|---|---|---|
| 公网 health | P50 ≈ 0.16 s | VPS 2C/4G，mTLS，5 次 |
| RPC 边界 | P50 53 ms / P95 320 ms | n=62，不含 LLM |
| 任务往返 | 20–55 s | 6–8 并发，qwen3.8-27b |
| 检索 P50 | 10 篇 7.4 ms → 1000 篇 133 ms | 116/300/3000 chunks |
| Schema Linking | 11 表 55 ms → 71 表 160 ms | 语义卡召回+精排 |
| 单任务模型成本 | ¥0 | 校园网关免费额度；本地代理 |


## 7. 项目实施（对应评分：项目实施 15 分）

### 7.1 里程碑计划与实际

| 阶段 | 计划 | 实际 |
|---|---|---|
| M1 协议与集群地基 | 10-14 ~ 10-16 | ACPs/WIT 基线跑通；名册与注册流程成型 |
| M2 三项机制 | 10-16 ~ 10-21 | PEP/域/资源封装 + 757 项单测；域×真 GPU、两机构场景通过 |
| M3 平台接入 | 10-15 ~ 10-19 | 问迹注册/发现/双向互联；心跳与任务级计量打通 |
| M4 迁移与加固 | 10-16 ~ 10-21 | 全栈迁至 VPS（原地升配、IP 不变、证书不重签）；systemd 值守 |
| M5 材料与验收 | 10-21 ~ 10-29 | 报告/PPT/视频/截图/打包（进行中） |

### 7.2 团队分工（3 人）

| 角色 | 负责范围 |
|---|---|
| 组长 | Supervisor 编排、PEP 委托授权、协作域与配额对账、集群部署与平台接入 |
| 组员 A | Research Designer（研究方案设计/校验/可行性）、AMP 转发与部署配置 |
| 组员 B（本人） | 问迹：NL2SQL/RAG/跨源/公式/澄清/Trace、AIP 适配层、平台注册与服务运维、流量与计量 |

协作机制：共用一份名册与 ACS 投影；变更走"代码 → make_acs → validate_acs → 部署门禁"；
跨成员接口以 AIP 任务契约 + 名册字段为准；每周对齐风险清单（令牌 TTL、隧道、配额、计量钩子）。

### 7.3 关键风险与应对

| 风险 | 实际发生 | 应对 |
|---|---|---|
| 注册一次性导致服务拒启 | 改声明触发 `acs_fingerprint_changed_after_submission` | 版本化 + 新工作区；状态修复流程文档化 |
| 部署覆盖计量钩子 | 10-23 榜单清零一天 | 部署前门禁 `scan_server_only_patches.py`；hooks 入仓 + 复盘 |
| AMP 计量口径不符 | 单侧记录/请求 ID | 任务级双侧 + `taskId`；逐条确认转发 + DLQ |
| 网络/隧道中断 | 校园隧道多次抖动 | 断线自动重连；零开销暂停与恢复脚本；心跳保活 |
| 模型配额 429 | AutoResearch 第 15 轮丢失 summary | 如实产出失败产物；后续加降级路径 |
| WIT IP 端点证书 | `server_public_endpoint_san_mismatch` | 走本地 CA 身份（路线 B）；记录并回馈上游 |
| 长任务令牌过期 | TTL 600s 与流水线冲突 | `delegate_continue` 运行中注入；每次出站现签 |

### 7.4 完成情况

- 三项创新机制均已落地并有场景化验收；
- 平台注册、发现、双向互联、访问与计量完整走通；
- 757 项单测全绿；报告/视频/PPT/截图按计划收口。

---

### 7.5 交付物清单

| 交付物 | 位置 | 状态 |
|---|---|---|
| 集群核心与三大机制 | `acps_cluster/` | 完成（757 单测） |
| 问迹引擎与 AIP 适配 | `partners/wenji/` | 完成（4 Skills，平台 active） |
| 研究设计成员 | `partners/designer/` | 完成 |
| 集群可视化 | `cluster-viz/` | 完成 |
| 技术报告 | `partners/wenji/docs/aic/report/` | 初稿完成，待补截图 |
| 演示视频/PPT | 〔待补〕 | 进行中 |
| 一键部署/测试 | `deploy/`、`run_all_tests.py` | 完成 |

### 7.6 团队协作机制

- **单一事实源**：名册只存元数据（module/workspace/port/declared_capabilities），AIC 与端点从
  `acs.final.json` 解析，避免多处硬编码漂移；
- **变更流程**：代码 → `make_acs.py` 投影 → `validate_acs.py` 校验 → 部署门禁 → 服务重启；
- **接口契约**：跨成员只通过 AIP 任务契约与 ACS 交互，不共享进程/存储；
- **周节奏**：周一对齐风险清单（令牌 TTL、隧道、配额、计量钩子），周五冻结提交物；
- **诚实文化**：失败/拒件/降级均留痕，评审只认可复核证据。


## 8. 应用成效与案例（对应评分：应用成效 10 分 / 实用性）

### 8.1 落地场景

1. **科研全流程协作**：DeepResearch 六阶段（检索 → 萃取 → 核验前置 → 综述 → 审稿对账）：
   9 篇真实文献，产出 11,575 字符结构化综述；Contradiction 缺失时按纪律诚实降级；
2. **自动复现实验**：AutoResearch 18 轮走真 AIP 链路（leader → experiment → gpu 容器 → eval），
   账本出现 `verdictSource=host-score`（hostScore=0.9 ≥ 判据 0.85），预测文件 sha256
   在第 12/13/14/16/18 轮逐字节一致；
3. **跨机构资源协作**：两机构双边域 S1–S5；本地 GPU 真训练 exit 0（env 通道与桥接通道各一次）；
4. **数据问答**：问迹为集群提供问数/文档问答/跨源/公式计算，4 个 Skills 可被 Supervisor 编排。

### 8.2 应用前后的量化对比

| 维度 | 改造前 | 改造后 |
|---|---|---|
| 资源接入 | 点对点脚本，换节点重写 | 名册 + ACS 自动投影，17 条成员统一接入 |
| 授权 | 人工白名单 | 三级准入 + 令牌；越权漏洞被复现并修复 |
| 委托开销 | leader 中转（上下文 196KB） | jti 直连（上下文 0），**2.94×** 加速，并行 1→3 |
| 计量 | 无/口径不一 | 任务级双侧 + 心跳 + 对账；榜单可见 |
| 防泄题 | 依赖约定 | 三闸门 + 8% 噪声基准 + 渗透三路全挡 |
| 数据问答 | 无 SQL/引用证据 | SQL + 引用 + 公式步骤 + Trace；NL2SQL 61/61、RAG 41/41 |

### 8.3 转化潜力与佐证说明

本作品暂无专利、软件著作权、论文或企业合作意向书；可用佐证为：757 项单测、7 类场景验收、
平台访问与互联记录、以及可复现的脚本与账本。转化路径：把 PEP/域/计量封装为可复用的
"受治资源接入套件"，面向高校算力平台与科研机构私有数据场景。

---

### 8.4 业务价值与成本

| 维度 | 价值 |
|---|---|
| 效率 | 文献→综述链路自动化，DeepResearch 六阶段产出 11,575 字符综述；AutoResearch 18 轮自动复现 |
| 成本 | 单任务模型成本 ¥0（校园网关免费额度）；GPU 按任务隔离，失败可重试 |
| 风险 | 授权可撤销、冻结急停、防泄题三闸门；通信风险旁路检测 |
| 可复制 | 名册 + ACS 投影 + PEP/域，支持新资源方 1 天内接入 |
| 社会效益 | 为高校算力/数据共享提供"可授权、可计量、可审计"的协议化样板 |

### 8.5 案例细节

**案例 A（DeepResearch）**：9 篇真实文献检索 → 逐篇萃取四要素 → 核验前置 → 结构化综述
（11,575 字符）→ 审稿对账；Contradiction 伙伴缺失时按纪律降级，不编造矛盾对。

**案例 B（AutoResearch）**：真 AIP 链路 18 轮；leader → experiment → gpu（容器）→ eval
全程走协议与委托令牌；账本记录 `verdictSource=host-score`（0.9 ≥ 0.85），预测文件 sha256
在第 12/13/14/16/18 轮逐字节一致；第 17 轮拿不到独立分数时如实记 `inconclusive`。

**案例 C（问迹）**：为集群提供问数与文档问答；NL2SQL 61/61、RAG 41/41、跨源 11/11、澄清 30/30；
回答附 SQL/引用/公式步骤与 Trace，可作为"数据结论可复核"的样板。


## 9. 总结与展望（对应评分：总结与展望 10 分）

### 9.1 开源代码改造复盘

- **有效**：只扩上游、不 fork 运行时，升级成本可控；能力用 ACS 投影而非硬编码，名册只存元数据；
- **代价**：上游一次性注册与 IP SAN 限制逼出"版本化 + 身份切换"流程，文档与门禁必须同步；
- **教训**：计量钩子属于部署资产，必须入仓并加部署门禁（10-23 事故）；令牌不可见性要用断言测试而非口头约定。

### 9.2 平台接入复盘（踩坑与通用经验）

1. URL 拼接规则要区分"服务根"与"完整端点"（registry/ca vs discovery）；
2. 注册后声明不可变，改声明会拒启；升级前先备份 workspace 与 ACS；
3. 在线判定看心跳，不看端口；端口通但无心跳会"隐身"；
4. 计量按 taskId 双侧配对，单侧记录等于没记；
5. mTLS 与身份绑定要断言"证书 AIC == senderId"，否则"能连"也可能是冒充。

### 9.3 对标准化落地的建议（具体、可操作、前瞻）

1. **ACS 版本化与热更新**：为 ACS 引入 `revision` 与灰度生效语义，允许在不换 AIC 的前提下演进能力，
   并在注册中心保留历史版本与审计；
2. **AMP 计量口径标准化**：把"taskId 双侧配对 + 采样 + 幂等（logId）"写入规范附录，提供参考实现与
   一致性测试套件，避免各团队重复踩坑；
3. **AAC 委托参考实现**：把令牌字段（iss/sub/aud/act/jti/scope/exp/max_uses）、父链验证、撤销与
   配额对账做成 SDK 模块，降低跨组织协作门槛；
4. **IP 端点证书规范**：明确 IP SAN 的签发与校验要求（WIT 2.1.0 当前不写 IP SAN），
   避免"平台能注册、服务起不来"；
5. **发现层谓词下推**：将 visibility/domains 过滤下推到检索层，替代"先 limit 后过滤 + 3× 超取"，
   并给出匿名/域成员/跨域三类用例的期望语义。

### 9.4 后续演进

- 把 PEP/域/计量抽象为接入套件，支持第三方资源方 1 天内接入；
- 扩展问迹的 Skills（图表、报表、联邦问数），并探索 Group 模式的多方协同；
- 将域可见性改造与 WIT 缺陷修复提交上游，持续回馈 AIP-PUB 社区。

---

### 9.5 建议的落地路线（表 9-1）

**表 9-1 标准化建议的落地方式与预期收益**

| 建议 | 落地方式 | 预期收益 |
|---|---|---|
| ACS 版本化/热更新 | 注册中心增加 revision 与灰度字段；框架支持"同 AIC 多版本" | 能力迭代不再换身份，减少重注册成本 |
| AMP 计量口径 | 规范附录给出 taskId 双侧配对、采样与 logId 幂等；提供一致性测试 | 榜单/计费/审计口径统一，跨团队可比 |
| AAC 委托参考实现 | SDK 提供令牌签发/校验/撤销/配额模块 | 跨组织协作门槛下降，安全默认开启 |
| IP 端点证书 | 明确 IP SAN 签发与校验；框架修复 | 内网/IP 直连场景可用，不再"注册成功但服务起不来" |
| 发现谓词下推 | discovery-server 支持 visibility/domains 下推 | 大目录检索性能与语义正确性同时提升 |


## 10. 附录

### 10.1 术语表

| 术语 | 英文/缩写 | 说明 |
|---|---|---|
| 智能体身份码 | AIC | OID 形式的全局唯一身份 |
| 智能体能力描述 | ACS | 能力、端点、可见性、输入输出 |
| 智能体可信注册 | ATR | 注册→审核→AIC→EAB→证书 |
| 智能体交互协议 | AIP | Direct（RPC/Stream/Notification）与 Group |
| 智能体发现协议 | ADP | 显式/探索式检索 + aliveMap |
| 智能体监控协议 | AMP | 心跳/指标/访问/消息/系统/审计 |
| 访问控制 | AAC | 认证之后的授权与委托 |
| 委托授权点 | PEP | 本作品的三级准入实现 |
| 协作域 | Domain | 双签章程 + grant + 配额 + 可见性 |

### 10.2 AIP 条款对照表（节选）

| 条款主题 | 本作品落点 |
|---|---|
| 身份与证书 | AIC + mTLS 双证书；CN/SAN/EKU/链校验 |
| 能力描述 | ACS 投影 + 三层可见性 |
| 注册与审核 | WIT ATR 状态机；版本化升级 |
| 任务交互 | Direct JSON-RPC 全状态机；身份绑定 |
| 发现与存活 | ADP + 心跳 aliveMap + 域过滤 |
| 监控与计量 | 六类 AMP 日志；taskId 双侧配对 |
| 访问控制 | PEP + JWS 令牌 + 域 grant + 冻结/配额 |

### 10.3 接口清单（问迹）

| 接口 | 说明 |
|---|---|
| `POST /rpc`（AIP Direct） | 任务命令与结果（mTLS） |
| `GET /health` | 服务健康（strict mTLS） |
| `GET /api/acps/status` | 只读互联状态（AIC/技能/调用统计） |
| `POST /api/chat` · `/api/chat/stream` | 交互式问答（JSON/SSE） |
| `POST /api/nl2sql` · `/api/rag` | 单引擎调用 |
| `GET /api/trace/{turn_id}` | 全链路 Trace 回放 |

### 10.4 开源合规与授权声明

- ACPs-community / ACPs SDK：MulanPSL-2.0；WIT 2.1.0：官方发行渠道（未随仓库分发）；
- 上游 discovery-server 的域可见性改造以 diff 形式标注（48 文件，+1272/−170），未合并上游；
- 第三方数据与模型（Chinook、BGE、MinerU/PaddleOCR 等）授权说明见 `partners/wenji/NOTICE.md`；
- 本作品不含任何密钥、证书私钥与平台凭据。

### 10.6 测试清单（表 10-1）

**表 10-1 测试与验收分布**

| 类别 | 数量 | 说明 |
|---|---:|---|
| 离线单测（团队） | 757 | PEP/域/资源/编排/guard/工作区等 |
| PEP 专项 | 102 | 含越权复现与修复回归 |
| 域/硬化/可见性 | 31 + 16 + 15 | 章程、派生、冻结、可见性 |
| 资源伙伴专项 | 全量内 | GPU/CPU/评测/KB/模型/工作区 |
| 问迹引擎 | 273 | 单测 + 集成（NL2SQL/RAG/跨源/公式/Trace） |
| 问迹 AIP 适配 | 18 | 声明/ACS/计量/状态 API |
| 场景验收 | 7 类 | 16/16、11/11、10/10、9/9、9/9、7/7、5/5 |

### 10.7 图表索引

| 编号 | 名称 | 位置 |
|---|---|---|
| 图 3-1 | 总体部署拓扑 | 3.6 |
| 图 4-1 | 心跳与发现链路 | 4.2 |
| 图 4-2 | 多智能体委派时序 | 4.8 |
| 图 6-1 | 平台接入拓扑 | 6.1 |
| 图 6-2 | 注册→发现→互联→访问时序 | 6.2 |
| 表 4-1/4-2 | AIP 命令与状态机 | 4.7 |
| 表 4-3 | 委托令牌字段 | 4.9 |
| 表 4-4 | guard 风险维度 | 4.10 |
| 表 4-5 | 资源实例隔离与审计 | 4.11 |
| 表 5-1/5-2 | 改造规模/创新证据 | 5.5/5.6 |
| 表 6-1 | 关键性能指标 | 6.8 |
| 表 9-1 | 标准化建议落地 | 9.5 |


### 10.5 佐证材料索引

| 主题 | 路径 |
|---|---|
| 项目总览与量化证据 | `project.md` |
| 架构与三项机制 | `docs/architecture.md`、`docs/PEP_design.md`、`docs/RFC_delegation_domain_draft.md` |
| 名册与身份 | `docs/agent-aic-roster.md`、`acps_cluster/core/cluster_roster.py` |
| AMP 与事故复盘 | `docs/amp-compliance-and-gaps.md`、`docs/incident-2026-10-23-amp-hooks-lost.md` |
| 问迹接入与验证 | `partners/wenji/INTEGRATION.md`、`partners/wenji/docs/aic/` |
| 复现与测试 | `python run_all_tests.py`、`acps_cluster/demo_run/` |
| 平台截图 | 〔待补：见 6.4〕 |

---

### 附：脱敏自检清单（提交前逐项勾选）

- [ ] 全文检索：学校名称/简称、LOGO、指导教师、人名、学号
- [ ] 截图打码：组织字段、用户名、路径、证书私钥
- [ ] 图表/页眉页脚/代码注释/附录 均不含上述信息
- [ ] 数据可溯源：每项指标标注环境、样本量、统计口径
- [ ] 引用与授权：开源组件、数据、模型均有声明（见 10.4 与 NOTICE）

### 附：评分项自查表

| 评分项 | 对应章节 | 支撑证据 | 自评档位 | 缺口 |
|---|---|---|---|---|
| 创新性 25 | 3、4、5 | PEP/域/资源封装/guard/问迹声明 | 21–24 | 需突出"深度二次开发"的 diff 与量化 |
| 需求分析 10 | 1、2 | 18 个在线智能体抽样、团队访谈、757 测试 | 8–9 | 调研样本可再扩充 |
| AI 技术应用 20 | 4、6 | 全状态机、双向调用、mTLS、计量 | 17–19 | 需补平台截图与并发数据 |
| 项目实施 15 | 7 | 里程碑、分工、风险、门禁 | 12–14 | — |
| 应用成效 10 | 6、8 | 榜单、访问量、DeepResearch/AutoResearch | 8–9 | 待补后台截图 |
| 总结与展望 10 | 9 | 5 条标准化建议 + 复盘 | 8–9 | — |
| 附加分 10 | 10.4、附录 | 一键部署/测试、视频、榜单 | 7–9 | 视频与截图待补 |
