# 架构说明

本文说明整合后的研枢集群：设计目标、模块职责、三项互联创新、外部组件的接入方式，
以及本次新增的 **guard → supervisor 适配层**。协议细节见
[PEP_design.md](PEP_design.md) 与 [RFC_delegation_domain_draft.md](RFC_delegation_domain_draft.md)；
项目证据见 [../project.md](../project.md)。

---

## 1. 设计目标

研枢不是"把 agent 连起来"的演示，而是把科研资源做成**受治资源**：

1. **可发现**：资源 agent 以 ACS 声明能力，进入发现层；可见范围按域过滤。
2. **可授权**：任务级 PEP 准入（public / 白名单 / 令牌），策略缺失 fail-closed。
3. **可计量**：AMP 心跳与访问上报；域内使用量对账与配额。
4. **可撤销**：令牌 `max_uses` / TTL、域冻结、授权撤销。
5. **可审计**：工作区 `manifest.jsonl`、PEP 审计流、可视化事件流，以及
   **通信行为风险报告**（本次整合新增）。

一句话：**PEP 管"能不能用"，guard 管"用得对不对"**。

---

## 2. 系统拓扑

```text
机构 B（消费方 / 研枢集群）                     机构 A（资源提供方）
  supervisor（总控、自主调度）
  ├─ scout / synthesis / writer / critic
  ├─ experiment（域内 consumer）
  ├─ frontier / contradiction / citation
  └─ 外部成员：designer、wenji
          │
          │  双签域章程（合同对象）+ 域内 grant
          ▼
  ┌─────────────────────────────────────────────┐
  │ kb（知识库）  cpu（算力）  gpu（算力）        │
  │ workspace（存储/工作区）                      │
  └─────────────────────────────────────────────┘
```

- 编排器：`acps_cluster/agents/supervisor.py`（入口 `acps_cluster/agents/partner_supervisor.py`）。
- 名册：`acps_cluster/core/cluster_roster.py`（15 条：1 supervisor + 14 partner，
  含两个外部成员）。
- 发现层元数据：`acps_cluster/core/discovery_metadata.py`。

---

## 3. 五部分与代码映射

| 部分 | 关键实现 |
|---|---|
| agents | `acps_cluster/agents/partner_*.py`（工作流 9 + 资源 4）、`capabilities/*.py`、`partners/designer/`、`partners/wenji/` |
| 互联机制 | `acps_cluster/delegation/`（pep.py / token.py / client.py / bridge.py / domain_manifest.py / grant.py / visibility.py / reconciliation.py / quota.py） |
| 衍生机制 | 三级能力边界（各 partner 的 `capability_visibility`）、`acps_cluster/core/discovery_metadata.py`、`acps_cluster/core/arg_shapes.py` |
| 框架与运行时 | `acps_cluster/core/bootstrap.py`（.env / 模型路由 / LLM 硬闸）、`acps_cluster/deploy/amp_*.py`、`partners/designer/src/amp_kafka_forwarder.py`、`requirements.txt`（WIT 只声明依赖） |
| 资源路由 | `acps_cluster/static_router.py`（结构化 args → ToolCall，默认不跑 LLM）、`supervisor.resource_call`、CPU/GPU/KB/workspace 的 `static_prefix` 接线 |
| 部署 | `deploy.sh`（preflight/init/build/install-units/start/stop/status/logs）、`acps_cluster/deploy/*.service` 模板、`docs/deployment.md` |
| 前端 viz | `cluster-viz/`、`acps_cluster/core/viz_live.py`、`acps_cluster/delegation/viz_record.py` |

---

## 4. 三项互联机制

### 4.1 域（授权关系的组织方式）

- 跨机构以**双签章程**建立协作域（`delegation/domain_manifest.py`）。
- 域内成员领取 **grant**（`grant.py`），域策略以 revision 演进，可冻结/解冻。
- 发现层按 `visibility` / `domains` 过滤并脱敏（`visibility.py` +
  上游 discovery 的 monkeypatch，见 `cluster_common.py` 注释与 NOTICE）。
- 商业域按同样模型表达"资源方—消费方—审计方"三方关系
  （`policy-projections/`、[third-party-compute.md](third-party-compute.md)）。

### 4.2 PEP：任务级 + 周期级

| 口径 | 触发 | 凭据 | 代码 |
|---|---|---|---|
| 任务级（点对点委托） | 每次委派 | Ed25519 JWS 委托令牌（iss/sub/aud/act/jti，`max_uses`、TTL、skill 绑定） | `pep.py`、`token.py`、`client.py` |
| 周期级（域级授权与对账） | 域章程周期 | 域 grant + 使用量对账、配额 | `reconciliation.py`、`quota.py`、`grant.py` |

工作区令牌把两者接起来：supervisor 在委派时随任务签发 `act=partner` 的
`ws.*` 令牌 bundle（`token.extract_token_for_audience()` 只取 `aud==本机`），
伙伴持令牌调 workspace 的 `ws.read / ws.write`，令牌不进入模型上下文。

### 4.3 workspace：存储资源 + 可审计组织

工作区同时是"一种资源"和"一种组织方法"：

```text
ws-<task>/
├── repo/        # 输入仓库（可克隆公开仓库）
├── products/    # 最终产物（manifest 登记）
├── scratch/     # 中间草稿
├── shared/      # 跨 agent 共享
└── manifest.jsonl   # 每次写入的 actor / sha256 / 时间
```

- 资源实例：`acps_cluster/agents/partner_workspace.py` 以平台注册的资源 agent 身份运行，四 skill
  （`ws.create/list/read/write`）全部非公开，准入与 task_id 作用域都依赖 PEP。
- 审计与保留：写入必须持令牌，`actor` 由 token 的 `act.aic` 派生；`ws-*` 目录结构
  本身即"有规律组织"的规范，**仓库只保留规范与代码，不保留运行时数据**。
- 详见 [workspace-partner.md](workspace-partner.md)、
  [design-task-workspace.md](design-task-workspace.md)、
  [handoff-workspace-partner-access.md](handoff-workspace-partner-access.md)。

---

## 5. 能力边界与可见范围

| 层级 | 含义 | 判定点 |
|---|---|---|
| L0 public | 公开可发现、可直调 | discovery 元数据 + PEP `public-skill` |
| L1 白名单 | 仅名单内 AIC 可调 | PEP `leader-direct` / `whitelist` |
| L2 令牌 | 必须持有效委托令牌 | PEP `delegation-token` |

- 能力声明自带 `visibility`，`acps_cluster/core/discovery_metadata.py` 据此生成发现层投影。
- 资源伙伴默认 `ACPS_PEP_LEADER_DIRECT=0`：即使是 leader，走 L2 也要令牌。

---

## 6. guard → supervisor 适配层（本次核心新代码）

### 6.1 定位

| | PEP | guard |
|---|---|---|
| 时间 | 事前 | 事中 / 事后 |
| 问题 | 是否准入 | 通信过程是否异常 |
| 依据 | 身份、令牌、域策略、配额 | Storage / Timing / Behavioral 三维事件特征 |
| 动作 | allow / deny | 只告警（advisory-only） |

适配层**不改 PEP 语义**：不签发、不撤销、不改令牌、不改域策略、不自动阻断。

### 6.2 组件

```text
acps_cluster/guard/covert_event_guard.py   外部检测器（纯拷贝，零改动）
        ▲ load_sqlite / analyze_events
        ▲ load_peer_tasks_sqlite / analyze_peer_timing
        │
        ├── guard-api/                     本仓库新增：HTTP 接口层（FastAPI）
        │   ├── guard_api/app.py           /health /scan /tasks /peer-timing /events
        │   ├── guard_api/report.py        scan() → GuardReport（不落盘）
        │   └── guard_api/mount.py         start/stop_guard_api（supervisor 进程内）
        │
        └── acps_cluster/guard_adapter.py  本仓库新增：CLI / 留档层
            ├── scan()            → GuardReport（单任务 + 跨任务 peer timing）
            ├── write_report()    → guard-report-<ts>.json + latest.json
            ├── publish_viz_alerts() → cluster-viz/public/scenarios/guard-alerts.json
            ├── attach_recorder() → viz_live.Recorder.guard_alert()（run 内事件流）
            └── main()            → 一次性 / --interval 周期模式
```

### 6.3 报告契约（节选）

```json
{
  "generated_at": "2026-10-26T18:01:33+08:00",
  "db": "/path/to/aip-tasks.sqlite3",
  "window_limit": 1000,
  "task_count": 2,
  "peer_count": 1,
  "highest_level": "HIGH",
  "alerts": [
    {
      "channel": "task",
      "level": "HIGH",
      "score": 73,
      "subject": "task-suspicious-demo",
      "rules": ["encoded_high_entropy_blob", "repeated_payload",
                "alternating_interval_pattern", "invalid_state_transition"],
      "suggested_action": "人工复核并评估是否经既有 PEP 流程限速/撤销授权；适配层不自动处置"
    }
  ],
  "disposition": "advisory-only",
  "pep_semantics": "untouched"
}
```

- **零写入**：guard 以 `sqlite://...?mode=ro` 打开任务库；适配层只写报告/告警文件。
- **降级**：任务库没有 `aip_tasks` 表时，peer timing 记 warning 并跳过，单任务检测照常。
- **不判攻击**：输出只用于异常筛查，处置必须人工复核后走既有 PEP 流程。

### 6.4 运行方式

```bash
# 一次性
python -m acps_cluster.guard_adapter --db /path/to/acps/aip-tasks.sqlite3

# 周期（建议 systemd timer 或 --interval）
python -m acps_cluster.guard_adapter --db /path/to/acps/aip-tasks.sqlite3 --interval 300
```

默认输出目录 `acps_cluster/ws-supervisor/guard/`（属 `ws-*` 运行时目录，不入库）。

**HTTP 接口层**（`guard-api/`，可选依赖 `pip install -r guard-api/requirements.txt`）：

```bash
# 独立进程
python -m guard_api --db /path/to/acps/aip-tasks.sqlite3 --port 8778

# 挂载进 supervisor：设了才启用，与宿主同一事件循环、同生共死
export ACPS_GUARD_API_PORT=8778
export ACPS_GUARD_DB=acps_cluster/ws-supervisor/acps/aip-tasks.sqlite3
```

`guard-api` 的检测器与 `acps_cluster/guard/` 同源；`report.py` 沿用 `guard_adapter` 的
`GuardReport` 契约但**不落盘**。仓库侧修复了一处 Bearer 鉴权缺陷（`Annotated[..., Header()]`
在 `from __future__ import annotations` 下被 FastAPI 误判为 query，导致 token 模式全部 401），
修复后 20 项 API 测试全过；该改动在 NOTICE 中记录。

### 6.5 运行中守卫（RunGuardHooks）

`guard-api` / `guard_adapter` 都只能看到**已经落库的历史事件**，只有建议权；
`RunGuardHooks`（`acps_cluster/agents/supervisor.py`，priority 20）在 run 进程内按
代码侧观测实时判定，`ACPS_RUN_GUARD=enforce` 时中止并如实上报：

```text
命中规则 → _RUN_GUARD_ABORT 置位（仅 enforce）
        → wrap_tool_call 只放行 aip_fail，其余返回 error=run-guard-abort
        → SupervisorRunHooks.on_run_end：fail_handler → AIP failed
                                          _viz_finish(status="failed")
        → 审计 <workspace>/logs/run-guard-audit.jsonl + run-trace run_guard_abort
```

规则：`wall-clock`（13800s）、`no-activity`（1500s）、`progress-stall`（5400s）、
`error-streak`（8）、`repeat-loop`（16）、`step-budget`（195）、
`partner-not-finishing`（30）。`progress-stall` 只认实际推进（提交/写工作区/新委派/
闸门通过/等待状态变化），反复 `working` 不续命。默认 `observe`，建议先灰度校准阈值；
详见 [run-guard.md](run-guard.md)。

三层的分工是：PEP 管事前准入，RunGuard 管事中止损，guard-api/adapter 管事中事后取证。

---

## 7. 组件接入的接缝

原则：**外部组件代码零改动，接缝全部做在本仓库一侧**。

| 接缝 | 做法 | 影响面 |
|---|---|---|
| 问迹 | 团队自研模块，直接置于 `partners/wenji/` | 与集群共用本仓库；AIP 接入层与能力声明见 `partners/wenji/docs/aic/` |
| guard | 纯拷贝到 `acps_cluster/guard/`，剥离其 `.git` | 无改动 |
| guard 模块路径 | 新增 `acps_cluster/covert_event_guard.py` 一行转发 shim | 原测试/CLI 命令原样可用 |
| guard → supervisor | 新增 `acps_cluster/guard_adapter.py` | 新增旁路，不触碰 PEP |
| designer | 纯拷贝到 `partners/designer/`（去 `wheels/`、无真实 `.env`） | 无改动 |
| `cluster-viz` 入仓 | 仅修正两处"仓库外路径"默认值：`viz_live.DEFAULT_SCENARIO_DIR`、`viz_record.VIZ_PUBLIC` | 属本仓库自有代码，非外部组件 |
| 忽略规则 | `my-project/.gitignore` 为基线，改为路径化规则 | 见下 |

**`core/viz_live.py` / `delegation/viz_record.py` 的两处改动**：`cluster-viz` 原先在仓库外
（`../cluster-viz`），整合后位于仓库根（`./cluster-viz`）。标准包化后稳定为
`core/viz_live.py` 的 `parents[2]` 与 `delegation/viz_record.py` 的 `PROJECT / "cluster-viz"`，
其余逻辑不动；两处均可用 `ACPS_VIZ_SCENARIO_DIR` 覆盖，
单元测试用临时目录，故不影响既有行为。

**`.gitignore` 为什么不沿用全局 `*.json`**：原基线用 `*.json` + 白名单挡住运行时状态，
但新仓库同时收纳两个前端的源码 JSON（`cluster-viz/package.json`、
`partners/wenji/**`），全局忽略会让它们无法入库。整合版改为**按目录/后缀精确忽略**
（`ws-*/`、`*.sqlite3`、`*.token`、`*.pem`、`tokens/`、`secrets/` 等），
覆盖面不放宽，且 `vendor/` 只放行 `acps_sdk`。

---

## 8. 验证

干净克隆（不含 dev 私钥、平台快照、评测私有数据、`ws-*` 运行时状态）实测：

| 项 | 结果 |
|---|---|
| 集群测试 `acps_cluster/tests` | 见交付报告与 CI 输出（0 failed） |
| 外部 guard 自带测试 | 12 passed |
| guard 适配层测试 | 12 passed（只读、告警、报告、viz、CLI） |
| 最小链路 | `2 任务 / 1 peer → HIGH 73/100`，报告与 viz 告警均落盘 |
| 嵌入 git 目录 | `partners/` 与 `acps_cluster/guard/` 均无 `.git` |

> 数字以 `python run_all_tests.py` 的实时输出为准，本文不复制易漂移的计数。

---

## 9. 目录取舍

| 保留 | 理由 |
|---|---|
| `acps_cluster/`（supervisor、partner_*、delegation、tests、deploy 运行组件） | 集群本体与互联机制 |
| `capabilities/`、`acps_cluster/core/bootstrap.py` | 能力实现、启动引导与官方扩展点 |
| `registered_acs_overlay.json`、`policy-projections/` | 发现层覆盖表与域策略投影（无密钥） |
| `docs/` 精选 | 架构、协议、工作区、AMP、名册、新增 agent 手册 |

| 剔除 | 理由 |
|---|---|
| `ws-*`、`tokens/`、`certs-backup-*`、`*.pem`、`.env`、`_dev-ca-chain.pem` | 运行时数据与凭据 |
| `acs.*.filled.json` / `acs.*.resubmit.json` | 各 agent 的 ACS 声明稿快照；按需用 `deploy/make_acs.py` 生成，仓库只留发现层覆盖表与代码投影 |
| `delegation/demo_*.py`（除 `demo_pep.py`） | 演示脚本精简；保留的 `demo_pep.py` 被复现脚本引用 |
| `datasets/`、`eval-private/`、`demo_run/`、`output/` | 大数据与评测私有标签 |
| `_archive/`、`_audit/`、`__pycache__`、`.pytest_cache` | 中间产物 |
| `vendor/wit_framework-*.whl`、`partners/designer/wheels/` | 二进制 wheel（WIT 只声明依赖） |
| 外层 `competition/`、`plans/`、`docx/`、`examples/`、`_sync-backup-*`、`ACPs-community/` 上游克隆 | 非交付内容，避免误把上游整仓带入 |
