# 研枢（Yanshu）· AIC 智能体互联

> 面向科研协作的多智能体集群：把 GPU 算力、CPU、知识库、评测、算法模型与任务工作区
> 封装成**可发现、可授权、可计量、可撤销、可审计**的协作资源；在"连得上"之外，
> 进一步回答"**谁有权用、用了什么、通信是否异常**"。

协议依据 GB/Z 185《人工智能 智能体互联》与 ACPs（Agent Collaboration Protocols），
运行底座为 WIT Framework 2.1.0。

- 项目总览与实验证据：[project.md](project.md)
- 架构与创新点拆解：[docs/architecture.md](docs/architecture.md)
- 协议设计：[docs/PEP_design.md](docs/PEP_design.md)、[docs/RFC_delegation_domain_draft.md](docs/RFC_delegation_domain_draft.md)
- 文档索引：[docs/README.md](docs/README.md)

---

## 一、目录结构

```text
yanshu-AIC/
├── acps_cluster/                 # 集群核心（agent + 互联机制 + 运行时）
│   ├── agents/                   # supervisor 编排器、partner_*（研究/资源）、workspace 客户端
│   ├── core/                     # bootstrap、名册、发现元数据、模型路由、扩展点、viz_live
│   ├── runtime/                  # GPU/HPC/评测执行、知识库、作业台账、任务派发
│   ├── tools/                    # 复现/冒烟脚本（非常驻服务）
│   ├── delegation/               # ★ 互联机制：PEP 准入、委托令牌、协作域、工作区令牌
│   ├── guard/                    # ★ 事件通信风险检测器（纯拷贝，剥 .git）
│   ├── guard_adapter.py          # ★ 新代码：guard → supervisor 旁路适配层
│   ├── covert_event_guard.py     # 兼容 shim（让队友原模块路径继续可用）
│   ├── deploy/                   # 运行单元、AMP 上报组件、发布/预检脚本
│   ├── policy-projections/       # 域授权的策略投影（公开声明）
│   └── tests/                    # 集群测试（含 guard_adapter 测试）
├── capabilities/                 # 能力实现（arXiv、矛盾侦测、前沿、数据集、记忆等）
├── partners/
│   ├── designer/                 # 设计 agent（src / tests / config.examples / systemd）
│   └── wenji/                    # 问迹（团队自研问数/问答智能体，含其自有 frontend/）
├── cluster-viz/                  # 集群总览前端（React + Vite + ReactFlow）
├── guard-api/                    # guard 的 HTTP 接口层（FastAPI；独立进程或挂载 supervisor）
├── docs/                         # 架构、PEP/RFC、工作区、AMP、名册、运行手册
├── project.md                    # 项目总览、实验与量化证据
├── requirements.txt / pyproject.toml
├── vendor/acps_sdk-*.whl         # 随仓库分发（MulanPSL-2.0）；wit_framework 不分发
└── .github/workflows/ci.yml      # 静态检查 + 单元测试 + 前端构建
```

> `partners/wenji/frontend/` 与 `cluster-viz/` **职责不同、不合并**：前者是问迹
> agent 的内部界面（上传、问答、Trace），后者是全集群总览与任务回放。
>
> **ACS 声明稿不随仓库分发**：各 agent 的 `acs.*.filled.json` / `.resubmit.json`
> 快照已移除，避免仓库堆积多版本公开声明；需要时用
> `acps_cluster/deploy/make_acs.py` 按当前代码投影生成，再由
> `deploy/validate_acs.py` 校验。仓库保留的 `registered_acs_overlay.json`
> 是发现层覆盖表，`policy-projections/` 是域策略投影。

---

## 二、五部分导览

### 1. Agents（15 个参赛 agent + 1 个编排器）

**工作流 agent（9）**

| agent | 模块 | 职责 |
|---|---|---|
| scout | `acps_cluster/agents/partner_scout.py` | 文献检索、arXiv 取源、检索记忆 |
| synthesis | `acps_cluster/agents/partner_synthesis.py` | 多来源资料综合与去重 |
| writer | `acps_cluster/agents/partner_writer.py` | 综述/报告写作 |
| critic | `acps_cluster/agents/partner_critic.py` | 审查、证伪、质量闸门 |
| experiment | `acps_cluster/agents/partner_experiment.py` | 复现实验、沙盒执行、结果回传 |
| frontier | `acps_cluster/agents/partner_frontier.py` | 速度前沿/趋势侦测 |
| contradiction | `acps_cluster/agents/partner_contradiction.py` | 跨文献矛盾侦测 |
| arxiv | `capabilities/arxiv.py` | arXiv 检索/取论文能力（ACS 由代码投影按需生成） |
| citation-integrity | `acps_cluster/agents/partner_citation.py`、`citation_verify.py` | 引用真实性与元数据核验 |

**资源 agent（4）**

| agent | 模块 | 资源语义 |
|---|---|---|
| kb | `agents/partner_kb.py`、`runtime/kb_store.py`、`runtime/kb_runner.py` | 知识库：受限查询 DSL、行级/字段级脱敏 |
| cpu | `agents/partner_cpu.py` | CPU 算力：同沙盒、无 GPU |
| gpu | `agents/partner_gpu.py`、`runtime/gpu_runner.py` | GPU 算力：Docker 八条硬边界、SSRF 防线 |
| workspace-partner | `agents/partner_workspace.py`、`agents/partner_workspace_client.py` | 任务工作区：四分区存储资源实例化 + task_id 作用域 |

**编排器**：`acps_cluster/agents/supervisor.py`（入口 `partner_supervisor.py`），负责发现 →
计划 → 委派 → 等待产物 → 汇总。名册另含 `eval` / `model` 两个早期资源节点
（`agents/partner_eval.py`、`agents/partner_model.py`），以及 `designer` / `wenji` 外部成员条目。

**外部 agent（2）**：`partners/designer/`（Research Designer）、`partners/wenji/`（问迹）。

### 2. 互联机制（创新点）

**① 域（Domain）** —— 协作域 / 商业域：域级授权 + 发现层按域过滤。
跨机构以双签章程建立域，域内以 grant 表达授权，发现层按 `visibility`/`domains`
过滤并对响应脱敏。实现：`acps_cluster/delegation/domain_manifest.py`、`grant.py`、
`visibility.py`、`policy-projections/`。

**② PEP 准入（Policy Enforcement Point）** —— 两道口径：

- **任务级（点对点委托）**：一次委派一次判定。public / 白名单 / 令牌三级准入，
  Ed25519 JWS 委托令牌（iss/sub/aud/act/jti），策略缺失时 fail-closed。
- **周期级（域级授权与对账）**：域章程授权的周期内复用 + 使用量对账
  （`reconciliation.py`、`quota.py`），把"一次一授权"的成本摊到域周期上。

实现：`acps_cluster/delegation/pep.py`、`token.py`、`client.py`、`bridge.py`、
`reconciliation.py`、`quota.py`、`vault.py`。设计见
[docs/PEP_design.md](docs/PEP_design.md)、[docs/RFC_delegation_domain_draft.md](docs/RFC_delegation_domain_draft.md)。

**③ workspace** —— 双重身份：

- **存储资源实例化**：workspace partner 作为平台注册的资源 agent
  （`agents/partner_workspace.py`），按任务开辟服务器端工作区；
- **任务可审计、可保留、有规律组织**：`ws-<task>/` 四分区（`repo/ products/
  scratch/ shared/`）+ `manifest.jsonl`，写操作经 `ws.write`（持令牌、actor 由
  token 的 `act.aic` 派生），文件不进模型上下文。

规范见 [docs/workspace-partner.md](docs/workspace-partner.md)、
[docs/design-task-workspace.md](docs/design-task-workspace.md)、
[docs/handoff-workspace-partner-access.md](docs/handoff-workspace-partner-access.md)。
注意：仓库只保留**目录组织规范与代码**，`ws-*` 运行时数据不入库。

### 3. 衍生机制：能力边界与可见范围

- **三级能力边界**：`L0 public`（公开可发现）/ `L1 白名单`（名单内可调）/
  `L2 令牌`（必须持委托令牌）——见 `acps_cluster/core/arg_shapes.py` 与各 `partner_*.py`
  的 `capability_visibility` 接线。
- **能力分层与可见范围**：`acps_cluster/core/discovery_metadata.py` 生成发现层元数据，
  按域/成员过滤；`capabilities/` 提供能力实现（arXiv、矛盾、前沿、数据集、记忆等）。

### 4. 框架与运行时组件

- **WIT 运行时 2.1.0**：**只声明依赖**（`requirements.txt` / `pyproject.toml`），
  wheel 本体不入库（上游未提供许可证元数据）。
- **AMP 上报**：heartbeat → `amp.heartbeat`、access → `amp.access` 的
  sender / mirror / forwarder 组件：
  `acps_cluster/deploy/amp_heartbeat_sender.py`、`amp_heartbeat_mirror.py`、
  `amp_access_sender.py`，以及 `partners/designer/src/amp_kafka_forwarder.py`
  （JSONL → Kafka，断点续传）。见 [docs/amp-compliance-and-gaps.md](docs/amp-compliance-and-gaps.md)。
- **WIT 官方扩展点**：集群的日志、重试、上下文注入与 AIP 入参外壳化全部通过
  `WitHooks` 实现（见 `acps_cluster/agents/supervisor.py`、`acps_cluster/core/aip_guard.py`），
  未修改框架源码；最小示例见 `acps_cluster/hooks_example.py`（纯示例，不接入主链路），
  启动引导（`.env`、模型路由、LLM 调用硬闸）在 `acps_cluster/core/bootstrap.py`。

### 5. 前端 viz

- **集群总览**：`cluster-viz/`（React + Vite + ReactFlow），启动时读取
  `public/scenarios/manifest.json`，回放真实 run；工作区四分区面板消费
  `ws_zone` / `ws_file` 事件。
- **数据后端**：`acps_cluster/core/viz_live.py` 把 supervisor run 记录成 Scenario
  （`ACPS_VIZ_BASE_URL` / `ACPS_VIZ_SCENARIO_DIR`，默认关闭）。
- **安全告警栏位**：`viz_live.Recorder.guard_alert()` 把通信风险告警写进事件流；
  `guard_adapter` 另发布 `cluster-viz/public/scenarios/guard-alerts.json` 快照。

---

## 三、外部组件的创新点

这些能力不是简单拼盘：每个组件解决的是集群自身不覆盖的一段问题，
并以可运行代码 + 可复核数据的形式落在本仓库里。

### 1. Research Designer：把"研究方案"变成可校验的契约

| 创新点 | 机制 | 区别于常见做法 |
|---|---|---|
| 结构化设计契约 | 14 个必需章节 + 5 类研究类型的专项检查（empirical / ML / survey / qualitative / systematic review） | 缺章节会被 `research.validate_plan` 直接判无效，不靠模型自觉 |
| 证据约束 | `literature_evidence` 每条必须有 DOI 或 `source_url`；缺证据返回 `needs_evidence` 并生成委派任务 | 无出处的"文献"进不了设计，避免编造引用 |
| 可计算的可研 | 内建样本量估算（配对/独立、连续/二分类、alpha/power）与可行性评估（数据/样本/周期/算力） | 样本量与可行性是算出来的字段，不是形容词 |
| 输出契约 | 8 个必需字段 + 5 种状态（complete / needs_evidence / needs_data / needs_pilot / needs_multiple_inputs） | Leader 可据此判定"继续 / 补数据 / 退回"，编排可重试 |
| 计量不丢 | AMP JSONL → `amp.heartbeat` / `amp.access`，broker ack 后原子记录 offset | 重启断点续传，计量链路不重不漏 |

### 2. ACPS Event Channel Guard：从"权限可信"到"通信行为可信"

| 创新点 | 机制 | 证据 |
|---|---|---|
| 覆盖 PEP 之外的风险 | 即使 AIC 合法、令牌有效，仍检测异常载荷、时序与状态机 | 与 PEP 互补（见下一节） |
| 三维通道映射到 ACPs | Storage：长度 / Base64 / 熵；Timing：间隔规律 / 长短交替；Behavioral：状态倒退 / 机械切换 | 直接读 `aip_task_events` / `aip_tasks` 真实字段 |
| 稀疏事件跨任务检测 | 按 direction + local + remote 聚合，1 小时空闲切会话，≥6 个有效间隔才评分 | 140 个历史任务（每任务 3–5 事件）→ 7 个可分析会话 |
| 运行突发与安全风险分离 | 小于 1 秒的间隔记 `operational_burst`，不计安全分 | 减少把并发/重试误判为攻击 |
| 保守且可解释 | LOW / MEDIUM / HIGH / INSUFFICIENT_DATA，逐条给维度、规则、分值与样本量 | 样本不足不强判 |
| 只读旁路 | SQLite `mode=ro`，不写任务库、不自动阻断 | 12 项测试；阳性对照 HIGH 73/100 |

### 3. 问迹（wenji）：可解释的多模态问数 / 问答

**AIP 互联**：问迹以外部成员身份接入梧桐平台（AIC `…BFZ0FH.F2KZXJ.15ZZZG.19CN`，
端点为 `https://8.160.176.215:8601/rpc`，mTLS），对外发布 4 个 ACPs Skills
（`wenji.ask` / `data_query` / `doc_qa` / `schema`）；Supervisor 可经 Discovery 发现并委派，
问迹也可作为 Leader 反向调用集群伙伴。任务级 AMP access 按 `correlationId=taskId` 双侧记录，
心跳经平台 Kafka 进入 Discovery aliveMap。对接细节与验证记录见
[partners/wenji/INTEGRATION.md](partners/wenji/INTEGRATION.md)。

| 创新点 | 机制 | 量化 |
|---|---|---|
| 检索式要素中台 | 别名 / 指标 / 实体 / Schema 语义外置，字面 + 向量召回后精排 | 候选表 7.5→2.1，Schema token −74%，31/31 |
| 真依赖 DAG | 子任务声明 `depends_on`，按拓扑波次并行与汇合 | 五类跨源 11/11，Trace 可见依赖边 |
| 原生可解释 Trace | 意图 / 改写 / 计划 / 工具 / 模型 / 校验 / 计算 / 融合统一成事件树，只展示真实执行证据 | 时间线 + DAG、SQL / 引用 / 公式步骤、`turn_id` 回放 |
| 证据约束 RAG | Dense + BM25 + RRF + Rerank，回答后逐句忠实度自检 | 检索 / 答案 / 忠实度各 8/8，对抗修复 3/3 |
| 可执行公式资产 | FormulaIR + 三通道参数绑定 + LaTeX→SymPy 与受限求值双通道校验 | 4/4 数值一致 |
| 质量—成本一体化 | L0/L1/L2 分级 + 响应缓存 / 内容哈希 + purpose 账本 | 检索 10→1000 篇 P50 7→133ms；前缀缓存命中 ~89%、省 53% |

> 以上指标均来自各组件自带的测试 / 评测（版本化固定用例），口径与复现命令见
> [acps_cluster/guard/README.md](acps_cluster/guard/README.md)、
> [partners/designer/SUBMISSION_README.md](partners/designer/SUBMISSION_README.md)、
> [partners/wenji/README.md](partners/wenji/README.md)；不代表开放域泛化保证。

---

## 四、guard 与 PEP：互补而非替代

| | PEP（准入） | Event Channel Guard（通信风险） |
|---|---|---|
| 时间点 | **事前**：调用发生前 | **事中 / 事后**：事件已落库之后 |
| 回答 | "这一次调用**有没有资格**" | "即使有资格，**通信过程是否异常**" |
| 输入 | 身份、令牌、域策略、配额 | `aip_task_events` / `aip_tasks`（只读） |
| 输出 | allow / deny + 规则 | LOW / MEDIUM / HIGH + 可解释发现 |
| 动作 | 拦截或放行 | **只告警，不阻断** |
| 实现 | `acps_cluster/delegation/pep.py` | 检测器 `acps_cluster/guard/covert_event_guard.py`；接口层 `guard-api/`；留档 `acps_cluster/guard_adapter.py` |

数据流：

```text
ACPs / 梧桐任务事件
        ↓
aip_tasks / aip_task_events  ←── PEP 事前判定（allow/deny）
        ↓
acps_cluster/guard/covert_event_guard.py  ←── 只读旁路（mode=ro）
        ├── guard-api/（FastAPI）──→ /health /scan /tasks /peer-timing /events
        │                            在线查询，仅返回 JSON、不落盘
        └── guard_adapter.py ──────→ 周期报告 JSON + 结构化日志 + viz 告警（留档）
        ↓
Supervisor / 人工复核
        ↓
（必要时，人工决定）走既有 PEP 流程限速 / 撤销授权 / 冻结域
```

**两条通道，同一份报告契约**：

- `guard-api`：面向在线查询与前端/运维；默认关闭，可独立进程
  （`python -m guard_api`）或**挂载进 supervisor 的 asyncio 事件循环**
  （`ACPS_GUARD_API_PORT`，与宿主同生共死）；
- `guard_adapter.py`：面向周期留档、结构化日志与 viz 告警（`--interval` / CLI）。

两者共用 `GuardReport` 字段（含 `disposition` / `pep_semantics`），均只读、均不自动处置。

**适配层不改变 PEP 语义**：不签发、不撤销、不改令牌、不改域策略、不自动阻断。
报告里 `disposition: "advisory-only"`、`pep_semantics: "untouched"` 是显式契约。

### 运行中守卫（RunGuardHooks）：把"事后审计"变成"过程中止损"

guard-api / guard_adapter 读到的是**已经落库的历史事件**，只有建议权；运行中守卫
（`RunGuardHooks`，priority 20）在 supervisor / 作业进程内按代码侧观测实时判定，
`ACPS_RUN_GUARD=enforce` 时可以中止 run、`aip_fail` 如实上报、viz 标 failed。

| 层 | 位置 | 能看到什么 | 能否处置 |
|---|---|---|---|
| PEP | 准入点 | 身份 / 令牌 / 域策略 / 配额 | 拦截或放行 |
| `guard-api` / `guard_adapter` | 只读旁路 | 已完成任务的历史事件 | **不能**（advisory-only） |
| `RunGuardHooks` | 运行进程内 | 当前 run 的工具响应、步数、错误、等待轮次、墙钟 | **能**：中止 + `aip_fail` + viz failed |

规则与默认阈值（全部代码侧观测，不靠模型自觉）：

| 规则 | 默认 | 触发 |
|---|---|---|
| `wall-clock` | 13800s | run 墙钟超限（4h TTL 前 10 分钟收尸） |
| `no-activity` | 1500s | 没有任何工具/步活动 |
| `progress-stall` | 5400s | 长时间没有实际推进（无产物、无新委派、状态不变） |
| `error-streak` | 8 | 连续 8 次工具报错 |
| `repeat-loop` | 16 | 同一非等待工具 + 同参数重复 16 次 |
| `step-budget` | 195 | 已用步数 ≥195（max_steps 200） |
| `partner-not-finishing` | 30 | 同一 `task_ref` 等待/查询 30 轮未收尾 |

- 模式：`ACPS_RUN_GUARD=observe`（默认，只落审计）/ `enforce`（真中止）/ `off`；
- 审计：`<workspace>/logs/run-guard-audit.jsonl` + run-trace `run_guard_abort`；
- 回放校准：18/18 成功作业零误杀，3/3 卡到 TTL 的作业被 `progress-stall` 捕获；
- 细节见 [docs/run-guard.md](docs/run-guard.md)。

---

## 五、快速开始

环境：Python ≥ 3.12（开发验证用 3.13）、Node ≥ 20（仅前端）。

```bash
# 1) 依赖
python -m pip install -r requirements.txt      # 需要 wit_framework 时先从官方渠道装入

# 2) 全量测试（干净克隆：签名/数据集/平台快照相关用例会 skip，不是 fail）
python run_all_tests.py
# 或：python -m pytest acps_cluster/tests acps_cluster/guard/tests -q
```

当前干净克隆的实测结果见 [docs/architecture.md](docs/architecture.md) 的"验证"一节。

```bash
# 3) guard 适配层最小链路（读任务库 → 检测 → 风险报告 + viz 告警）
python -m acps_cluster.guard_adapter --db /path/to/aip-tasks.sqlite3
# 周期模式（常驻，类似 systemd）：
python -m acps_cluster.guard_adapter --db /path/to/aip-tasks.sqlite3 --interval 300

# 4) guard HTTP API（可选依赖：pip install -r guard-api/requirements.txt）
python -m guard_api --db /path/to/aip-tasks.sqlite3 --port 8778
#   挂载进 supervisor（默认关闭，设了才启用；与宿主同生共死）：
#   export ACPS_GUARD_API_PORT=8778
#   export ACPS_GUARD_DB=acps_cluster/ws-supervisor/acps/aip-tasks.sqlite3

# 5) 扩展组件自带测试
python -m pytest acps_cluster/guard/tests -q          # 12 项
python -m pytest guard-api/tests -q                   # 20 项（FastAPI/挂载/鉴权）
```

```bash
# 6) 前端 viz
cd cluster-viz
npm ci && npm run dev        # http://localhost:5178
```

扩展组件：

```bash
# 问迹：按 partners/wenji/README.md 启动（Docker + FastAPI + Vue3）
cat partners/wenji/README.md

# 设计 agent
cat partners/designer/SUBMISSION_README.md
```

---

## 六、安全与合规

**一律不入库**（`.gitignore` + 推送前人工核验）：

- 密钥与证书：真实 `.env`（只允许 `.example`）、`tokens/`、`*.pem`、`*.token`、
  `ws-*/acps/grants/`、`.campus_key`；
- 中间/运行产物：`__pycache__`、`.venv`、`node_modules`、`dist`、`*.log`、`output/`、
  `datasets/`、`demo_run/`、`ws-*` 运行时数据、`_archive/`、`_audit/`、`eval-private/`；
- 二进制 wheel：`wit_framework`、交付包里的 `wheels/`（`acps_sdk` 例外，随仓库分发）；
- `partners/wenji` 为团队自研模块（无独立 `.git`），与集群共用本仓库版本管理。

**许可证**：本仓库代码以 **MulanPSL-2.0** 发布，全文见 [LICENSE](LICENSE)；
上游组件、扩展组件的来源与许可状态见 [NOTICE](NOTICE)。

**问迹与集群的协作**：问迹以外部成员身份登记在 `acps_cluster/core/cluster_roster.py`，
由集群 Supervisor 通过 AIP Discovery 发现并调用；问迹自身也可作为 Leader 调用集群伙伴。
对接方式与验证命令见 [partners/wenji/INTEGRATION.md](partners/wenji/INTEGRATION.md)。

---

## 七、文档索引

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 架构、创新点映射、模块职责、适配层设计、验证结果 |
| [docs/PEP_design.md](docs/PEP_design.md) | PEP 准入设计（任务级 + 周期级） |
| [docs/RFC_delegation_domain_draft.md](docs/RFC_delegation_domain_draft.md) | 委托与协作域 RFC |
| [docs/workspace-partner.md](docs/workspace-partner.md) | 工作区资源实例：四分区、task_id 作用域 |
| [docs/handoff-workspace-partner-access.md](docs/handoff-workspace-partner-access.md) | 伙伴读写工作区：token bundle + 客户端 |
| [docs/design-task-workspace.md](docs/design-task-workspace.md) | 任务工作区与产物归档设计 |
| [docs/agent-aic-roster.md](docs/agent-aic-roster.md) | agent 身份（AIC）清单 |
| [docs/amp-compliance-and-gaps.md](docs/amp-compliance-and-gaps.md) | AMP 上报合规与差距 |
| [docs/third-party-compute.md](docs/third-party-compute.md) | 第三方算力 / 商业域 |
| [docs/runbook-add-new-agent.md](docs/runbook-add-new-agent.md) | 新增 agent 的铸造/注册/验收 |
| [acps_cluster/guard/README.md](acps_cluster/guard/README.md) | 三维事件通信风险检测器 |
| [guard-api/README.md](guard-api/README.md) | guard HTTP API：端点、鉴权、supervisor 挂载 |
| [docs/run-guard.md](docs/run-guard.md) | 运行中守卫：规则、阈值、中止链路、observe/enforce 与回放校准 |
| [partners/designer/SUBMISSION_README.md](partners/designer/SUBMISSION_README.md) | 设计 agent 提交说明 |
| [partners/wenji/README.md](partners/wenji/README.md) | 问迹（多模态可解释问答 agent） |

---

## 八、已知限制

- 干净克隆不含 dev CA 私钥、平台快照、评测私有数据与 `ws-*` 运行时状态，
  相关用例会 **skip**（不是 fail）；需要完整覆盖时按
  [docs/runbook-add-new-agent.md](docs/runbook-add-new-agent.md) 铸造 dev 身份。
- ACS 声明稿不在仓库内：`deploy/make_acs.py` 按当前代码投影生成，
  再交 `deploy/validate_acs.py` 校验。
- `delegation/` 仅保留被复现脚本与测试引用的两个 demo（`demo_pep.py`、
  `demo_autoresearch.py`）；其余演示脚本已精简，验收以测试与真实链路复现为准。
- guard 目前是规则型检测，阈值来自首轮真实任务基线，尚未在大规模标注数据集上
  统计 Precision / Recall；其结果只作异常筛查，不作攻击定论。
- `wit_framework` 不随仓库分发，因此 CI 在缺少该 wheel 时会跳过测试并给出告警。
- 各组件各自的运行依赖（PostgreSQL/pgvector、Docker、Kafka）需按各自 README 准备。
