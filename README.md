# 研枢（Yanshu）· AIC 智能体互联

> 面向科研协作的多智能体集群：把 GPU 算力、CPU、知识库、评测、算法模型与任务工作区
> 封装成**可发现、可授权、可计量、可撤销、可审计**的协作资源；在"连得上"之外，
> 进一步回答"**谁有权用、用了什么、通信是否异常**"。

本仓库是比赛的**最终整合作品**：由三方代码合并为一个可评审、可开源的单仓，
remote 指向 `chiekoschamburek-dev/AIC-agent-connect`。协议依据
GB/Z 185《人工智能 智能体互联》与 ACPs（Agent Collaboration Protocols），
运行底座为 WIT Framework 2.1.0。

- 项目总览与实验证据：[project.md](project.md)
- 架构与创新点拆解：[docs/architecture.md](docs/architecture.md)
- 协议设计：[docs/PEP_design.md](docs/PEP_design.md)、[docs/RFC_delegation_domain_draft.md](docs/RFC_delegation_domain_draft.md)
- 文档索引：[docs/README.md](docs/README.md)

---

## 一、三方整合

| 来源 | 角色 | 在本仓库的位置 | 引入方式 |
|---|---|---|---|
| 我的主项目（研枢集群） | 13 个 agent + PEP / 域 / workspace 互联机制 + 可视化 | `acps_cluster/`、`capabilities/`、`cluster-viz/` | 工作区状态按文件拷贝 |
| 队友 A：Research Designer（研枢 Research Designer / design-partner） | 设计 agent（ACPs Direct 服务 + AMP→Kafka 转发） | `partners/designer/` | 纯拷贝 |
| 队友 A：ACPS Event Channel Guard | 三维事件通信风险检测（Storage / Timing / Behavioral） | `acps_cluster/guard/` | 纯拷贝（已剥离其自带 `.git`） |
| 队友 B：「问迹」wenji | 多模态可解释问答 agent（FastAPI + Vue3 + pgvector） | `partners/wenji/` | **git subtree**，保留上游联系 |

**署名与边界**：队友代码**未做业务改动**。三方之间的接缝全部落在本仓库一侧的
适配层（`acps_cluster/guard_adapter.py`、`acps_cluster/covert_event_guard.py` 兼容
shim、`.gitignore` / CI 的路径适配）。唯一对队友文件所在目录的"改动"是**不拷贝**
其 wheels/ 与真实 `.env`，详见 [NOTICE](NOTICE) 与交付报告。

---

## 二、目录结构

```text
yanshu-AIC/
├── acps_cluster/                 # 集群核心（supervisor + 13 agent + 互联机制）
│   ├── delegation/               # ★ 互联机制：PEP 准入、委托令牌、协作域、工作区令牌
│   ├── guard/                    # ★ 队友 A：事件通信风险检测器（纯拷贝，剥 .git）
│   ├── guard_adapter.py          # ★ 新代码：guard → supervisor 旁路适配层
│   ├── covert_event_guard.py     # 兼容 shim（让队友原模块路径继续可用）
│   ├── partner_*.py              # 各研究 / 资源 agent 与 workspace 存储实例
│   ├── supervisor.py             # 编排器：发现 → 计划 → 委派 → 汇总
│   ├── cluster_roster.py         # 名册（本地 + 队友外部成员）
│   ├── discovery_metadata.py     # 能力分层、可见范围与发现层元数据
│   ├── viz_live.py               # 前端数据后端：run → Scenario、ws_file/ws_zone、guard_alert
│   ├── deploy/                   # 运行单元、AMP 上报组件、发布/预检脚本
│   ├── policy-projections/       # 域授权的策略投影（公开声明）
│   ├── acs.*.filled.json         # 各 agent 的 ACS 声明稿（公开字段）
│   └── tests/                    # 集群测试（含 guard_adapter 测试）
├── capabilities/                 # 能力实现（arXiv、矛盾侦测、前沿、数据集、记忆等）
├── partners/
│   ├── designer/                 # 队友 A：设计 agent（src / tests / config.examples / systemd）
│   └── wenji/                    # 队友 B：问迹（subtree，含其自有 frontend/）
├── cluster-viz/                  # 集群总览前端（React + Vite + ReactFlow）
├── docs/                         # 架构、PEP/RFC、工作区、AMP、名册、运行手册
├── hooks.py                      # WIT 官方扩展点（运行日志 / 工具重试 / 资料注入）
├── agents.py                     # 单 agent 示例入口（早期示例，保留可运行）
├── project.md                    # 项目总览、实验与量化证据
├── requirements.txt / pyproject.toml
├── vendor/acps_sdk-*.whl         # 随仓库分发（MulanPSL-2.0）；wit_framework 不分发
└── .github/workflows/ci.yml      # 静态检查 + 单元测试 + 前端构建
```

> `partners/wenji/frontend/` 与 `cluster-viz/` **职责不同、不合并**：前者是问迹
> agent 的内部界面（上传、问答、Trace），后者是全集群总览与任务回放。

---

## 三、五部分导览

### 1. Agents（15 个参赛 agent + 1 个编排器）

**工作流 agent（9）**

| agent | 模块 | 职责 |
|---|---|---|
| scout | `acps_cluster/partner_scout.py` | 文献检索、arXiv 取源、检索记忆 |
| synthesis | `acps_cluster/partner_synthesis.py` | 多来源资料综合与去重 |
| writer | `acps_cluster/partner_writer.py` | 综述/报告写作 |
| critic | `acps_cluster/partner_critic.py` | 审查、证伪、质量闸门 |
| experiment | `acps_cluster/partner_experiment.py` | 复现实验、沙盒执行、结果回传 |
| frontier | `acps_cluster/partner_frontier.py` | 速度前沿/趋势侦测 |
| contradiction | `acps_cluster/partner_contradiction.py` | 跨文献矛盾侦测 |
| arxiv | `capabilities/arxiv.py` + `acs.arxiv-partner.filled.json` | arXiv 检索/取论文能力与 ACS 声明稿 |
| citation-integrity | `acps_cluster/partner_citation.py`、`citation_verify.py` | 引用真实性与元数据核验 |

**资源 agent（4）**

| agent | 模块 | 资源语义 |
|---|---|---|
| kb | `partner_kb.py`、`kb_store.py`、`kb_runner.py` | 知识库：受限查询 DSL、行级/字段级脱敏 |
| cpu | `partner_cpu.py` | CPU 算力：同沙盒、无 GPU |
| gpu | `partner_gpu.py`、`gpu_runner.py` | GPU 算力：Docker 八条硬边界、SSRF 防线 |
| workspace-partner | `partner_workspace.py`、`partner_workspace_client.py` | 任务工作区：四分区存储资源实例化 + task_id 作用域 |

**编排器**：`acps_cluster/supervisor.py`（入口 `partner_supervisor.py`），负责发现 →
计划 → 委派 → 等待产物 → 汇总。名册另含 `eval` / `model` 两个早期资源节点
（`partner_eval.py`、`partner_model.py`），以及队友的 `designer` / `wenji` 外部成员条目。

**队友 agent（2）**：`partners/designer/`（Research Designer）、`partners/wenji/`（问迹）。

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
  （`partner_workspace.py`），按任务开辟服务器端工作区；
- **任务可审计、可保留、有规律组织**：`ws-<task>/` 四分区（`repo/ products/
  scratch/ shared/`）+ `manifest.jsonl`，写操作经 `ws.write`（持令牌、actor 由
  token 的 `act.aic` 派生），文件不进模型上下文。

规范见 [docs/workspace-partner.md](docs/workspace-partner.md)、
[docs/design-task-workspace.md](docs/design-task-workspace.md)、
[docs/handoff-workspace-partner-access.md](docs/handoff-workspace-partner-access.md)。
注意：仓库只保留**目录组织规范与代码**，`ws-*` 运行时数据不入库。

### 3. 衍生机制：能力边界与可见范围

- **三级能力边界**：`L0 public`（公开可发现）/ `L1 白名单`（名单内可调）/
  `L2 令牌`（必须持委托令牌）——见 `acps_cluster/arg_shapes.py` 与各 `partner_*.py`
  的 `capability_visibility` 接线。
- **能力分层与可见范围**：`acps_cluster/discovery_metadata.py` 生成发现层元数据，
  按域/成员过滤；`capabilities/` 提供能力实现（arXiv、矛盾、前沿、数据集、记忆等）。

### 4. 框架与运行时组件

- **WIT 运行时 2.1.0**：**只声明依赖**（`requirements.txt` / `pyproject.toml`），
  wheel 本体不入库（上游未提供许可证元数据）。
- **AMP 上报**：heartbeat → `amp.heartbeat`、access → `amp.access` 的
  sender / mirror / forwarder 组件：
  `acps_cluster/deploy/amp_heartbeat_sender.py`、`amp_heartbeat_mirror.py`、
  `amp_access_sender.py`，以及队友的 `partners/designer/src/amp_kafka_forwarder.py`
  （JSONL → Kafka，断点续传）。见 [docs/amp-compliance-and-gaps.md](docs/amp-compliance-and-gaps.md)。
- **hooks.py 官方扩展点**：`RunLogger`、`ToolRetry`、`MaterialContext` 三个 `WitHooks`
  实现；AIP 入参外壳化在 `acps_cluster/aip_guard.py`。

### 5. 前端 viz

- **集群总览**：`cluster-viz/`（React + Vite + ReactFlow），启动时读取
  `public/scenarios/manifest.json`，回放真实 run；工作区四分区面板消费
  `ws_zone` / `ws_file` 事件。
- **数据后端**：`acps_cluster/viz_live.py` 把 supervisor run 记录成 Scenario
  （`ACPS_VIZ_BASE_URL` / `ACPS_VIZ_SCENARIO_DIR`，默认关闭）。
- **安全告警栏位**：`viz_live.Recorder.guard_alert()` 把通信风险告警写进事件流；
  `guard_adapter` 另发布 `cluster-viz/public/scenarios/guard-alerts.json` 快照。

---

## 四、guard 与 PEP：互补而非替代

| | PEP（准入） | Event Channel Guard（通信风险） |
|---|---|---|
| 时间点 | **事前**：调用发生前 | **事中 / 事后**：事件已落库之后 |
| 回答 | "这一次调用**有没有资格**" | "即使有资格，**通信过程是否异常**" |
| 输入 | 身份、令牌、域策略、配额 | `aip_task_events` / `aip_tasks`（只读） |
| 输出 | allow / deny + 规则 | LOW / MEDIUM / HIGH + 可解释发现 |
| 动作 | 拦截或放行 | **只告警，不阻断** |
| 实现 | `acps_cluster/delegation/pep.py` | `acps_cluster/guard/covert_event_guard.py` |

数据流：

```text
ACPs / 梧桐任务事件
        ↓
aip_tasks / aip_task_events  ←── PEP 事前判定（allow/deny）
        ↓
acps_cluster/guard/covert_event_guard.py  ←── 只读旁路（mode=ro）
        ↓
acps_cluster/guard_adapter.py  ←── 定时扫描、汇总、发布
        ↓
风险报告 JSON + 结构化日志 + viz 告警
        ↓
Supervisor / 人工复核
        ↓
（必要时，人工决定）走既有 PEP 流程限速 / 撤销授权 / 冻结域
```

**适配层不改变 PEP 语义**：不签发、不撤销、不改令牌、不改域策略、不自动阻断。
报告里 `disposition: "advisory-only"`、`pep_semantics: "untouched"` 是显式契约。

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

# 4) 队友组件自带测试
python -m pytest acps_cluster/guard/tests -q          # 12 项
```

```bash
# 5) 前端 viz
cd cluster-viz
npm ci && npm run dev        # http://localhost:5178
```

队友组件：

```bash
# 问迹（subtree）：按上游 README 启动（Docker + FastAPI + Vue3）
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
- 二进制 wheel：`wit_framework`、队友交付包里的 `wheels/`（`acps_sdk` 例外，随仓库分发）；
- 嵌入 git 目录：`partners/wenji` 以 subtree 引入（无 `.git`），guard 的 `.git` 已剥离。

**许可证**：本仓库代码以 **MulanPSL-2.0** 发布，全文见 [LICENSE](LICENSE)；
上游组件、队友组件的来源与许可状态见 [NOTICE](NOTICE)。

**问迹 subtree 同步**（保持与上游联系）：

```bash
git remote add wenji-upstream https://github.com/Hakrin-dev/multimodal-explainable-qa-agent
git subtree pull --prefix=partners/wenji wenji-upstream main --squash
```

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
| [acps_cluster/guard/README.md](acps_cluster/guard/README.md) | 队友 A：三维事件通信风险检测器 |
| [partners/designer/SUBMISSION_README.md](partners/designer/SUBMISSION_README.md) | 队友 A：设计 agent 提交说明 |
| [partners/wenji/README.md](partners/wenji/README.md) | 队友 B：问迹 |

---

## 八、已知限制

- 干净克隆不含 dev CA 私钥、平台快照、评测私有数据与 `ws-*` 运行时状态，
  相关用例会 **skip**（不是 fail）；需要完整覆盖时按
  [docs/runbook-add-new-agent.md](docs/runbook-add-new-agent.md) 铸造 dev 身份。
- guard 目前是规则型检测，阈值来自首轮真实任务基线，尚未在大规模标注数据集上
  统计 Precision / Recall；其结果只作异常筛查，不作攻击定论。
- `wit_framework` 不随仓库分发，因此 CI 在缺少该 wheel 时会跳过测试并给出告警。
- 三方组件各自的运行依赖（PostgreSQL/pgvector、Docker、Kafka）需按各自 README 准备。
