# 多入口编排 P1：受理、查询与幂等根任务

> 日期：2026-11-17｜状态：**P1 代码完成，本机离线验收通过；P2（问迹侧分流）未开始。**
> 上游计划：`multi-entry-orchestration-plan.md`；前置验证：`p0-verification-report.md`。
> 本文只写已实现的东西和怎么用；没实现的一律列在 §8。

## 1. 这一期做了什么

把"入口 Partner 提交科研需求 → Supervisor 受理并后台执行 → 原入口查询结果"的**最小闭环**
落成代码，且**不动 ACS、不换 AIC、不改问迹**：

| 组件 | 位置 | 职责 |
|---|---|---|
| 根任务台账 | `runtime/root_store.py` | SQLite；幂等键 `(entry_aic, user_scope, request_id)`；状态机；绑定作业；记录平台 taskId |
| 受理/查询服务 | `runtime/root_service.py` | 建台账 → 拉起作业 → 绑定；状态映射；终稿取件；访问控制（纯标准库，可注入 fake） |
| 入口能力 | `agents/root_entry.py` | `research.submit` / `research.status`（**非公开**）；入站身份钩子；快速通道 handler |
| 准入扩展 | `delegation/pep.py` | 新增 L1''：**按调用方 × 按技能**的入站白名单（只对非域治理技能生效） |
| 接线 | `agents/supervisor.py` | 能力注册 + PEP 能力面 + 快速通道始终注册 `submit/status` + 入口身份钩子 |

复用而非新写：后台执行仍是 `submit_job` + `jobs_runner` + `jobs_store` 的既有交路
（受理即返回、并发上限、watchdog、自动补交回执）。**P1 没有新增 runner。**

## 2. 接口契约

### 2.1 `research.submit`（非公开，确定性执行）

| 入参 | 必填 | 说明 |
|---|---|---|
| `question` | ✅ | 完整任务文本（8–20000 字符） |
| `request_id` | ✅ | 入口生成的幂等号（1–128 位，字母/数字/`.` `_` `:` `-`），网络重试保持不变 |
| `user_scope` | | 用户/租户作用域，默认 `default`（1–128 位） |
| `mode` | | `custom`（默认）/ `deep-research` / `auto-research`（≤32 字符） |

调用方式（AIP `commandParams`）：

```json
{"skillId": "research.submit",
 "args": {"question": "分析这组实验数据并结合相关论文解释结果",
          "request_id": "req-20261117-001", "user_scope": "demo"}}
```

返回（作为本次 AIP 任务的产物）：

```json
{"ok": true, "root_id": "rt-20261117-193000-ab12", "request_id": "req-20261117-001",
 "state": "accepted", "job_id": "job-20261117-193000-1a2b", "mode": "custom",
 "replayed": false, "recovered": false,
 "note": "受理回执：任务已受理，不代表已完成；用 research.status 查询进度与结果。"}
```

### 2.2 `research.status`（非公开，确定性执行）

入参：`{"root_id": "rt-…"}`；只有**原入口 AIC** 能查（`entry_aic` 来自 mTLS 验证身份）。

```json
{"ok": true, "root_id": "rt-…", "state": "running", "job_id": "job-…",
 "job_state": "running", "report_available": false, "result": "",
 "result_chars": 0, "truncated": false, "error": "",
 "next": "执行中；稍后用 research.status 查询（不要重复提交）"}
```

完成后 `result` 是终稿（按 `jobs_store.report_page_chars()` 截一页，`truncated` 标明是否还有）。

### 2.3 错误码

| `error` | 含义 | 处置 |
|---|---|---|
| `unverified-caller` | 调用方身份未通过 mTLS/peer_verified | 检查对端证书与 AIC 绑定 |
| `invalid-request` | 参数非法（request_id/question/scope） | 按 `detail` 修正；**不要换 request_id 掩盖问题** |
| `request-conflict` | 同一幂等键提交了不同内容 | 要提交新需求请换新 `request_id` |
| `submit-failed` | 作业未被受理（并发上限/启动失败/凭据） | 按 `detail` 处理；失败会落台账 `failed` |
| `unknown-root` / `forbidden` | 任务不存在 / 非原入口查询 | 用原入口身份查；不要跨入口读 |

## 3. 幂等与恢复语义（写死在代码里）

- **同键同内容**：返回原根任务（`replayed=true`），**不再拉起作业**；
- **同键不同内容**：`request-conflict`，不覆盖；
- **崩溃窗口**（台账已建、作业未绑）：同键重放会**补拉起一次**（`replayed=true, recovered=true`）；
- **失败后重试**：受理失败会把根任务落 `failed`；重试需换新的 `request_id`（同键只会返回失败记录）；
- 写路径 `BEGIN IMMEDIATE` + 唯一索引，跨进程并发提交只会有一个成功。

状态映射（v1）：

```
jobs:  queued   → accepted
       running  → running
       done     → completed
       failed   → failed
       interrupted → failed
```

`needs_input / cancelled / expired` 在台账里已预留，P1 不产生（属 P4）。

## 4. 授权：为什么是"非公开 + 按技能白名单"

P0 验证的硬约束（`p0-verification-report.md` §1）：

- `public=True` → 进 ACS → 平台要求重新注册 → **换 AIC** → 材料全改；
- `visibility=domain` → 同样进 ACS（带 visibility/domains）→ 同样换 AIC；
- **`acps_skill` 才是 ACS 投影开关**：非公开能力只要不设它，就不进 ACS；
- `public=False` 只影响 PEP 可见性。

所以两个入口能力声明为：

```python
@capability(id="research.submit", public=False, model_callable=False, ...)  # 不设 acps_skill
```

P1 的准入用新增的 **L1''**（`delegation/pep.py`）：

```
ACPS_PEP_INBOUND_SKILL_ALLOW=AIC=skill1,skill2;AIC2=skill3
ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW=<同上，supervisor 专用别名，取并集>
```

判定规则：

- 只对**非域治理**技能生效（域技能仍必须走 grant/令牌，避免绕过冻结/成员制/配额）；
- 必须同时命中"调用方 AIC"和"技能 id"，没有通配；
- 未命中时继续走原有 L2/no-credential（fail-closed）；
- 放在域存储预检之后：域技能 + 章程缺失仍按 `domain-store-missing` 拒绝。

> 这是 P1 的**过渡通道**。P2 接问迹时应优先用域 grant（非公开技能 + 双边章程），
> 白名单可保留给"本地自证/联调"。

## 5. 服务器操作手册（自证闭环）

前提：P0 的 jobs 底座在服务器上可跑（`p0_checks jobs --static` READY）。

```bash
cd /opt/wit/yanshu-AIC
KB_AIC=$(python -c "import json;print(json.load(open('acps_cluster/ws-kb/acps/acs.final.json'))['aic'])")

# ① 授权：允许 kb 这个已验证调用方调用两个入口技能（按技能，不是整链）
grep -q '^ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW=' acps_cluster/deploy/.env.production \
  || echo "ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW=${KB_AIC}=research.submit,research.status" \
       >> acps_cluster/deploy/.env.production
systemctl restart wit-yanshu@supervisor

# ② 提交：kb 作为入口，向 supervisor 提交一条真实需求
python -m acps_cluster.bidirectional probe \
  --workspace acps_cluster/ws-kb \
  --peer-aic <SUPERVISOR_AIC> --skill research.submit \
  --args '{"question":"分析这组实验数据并结合相关论文解释结果","request_id":"req-demo-001","user_scope":"demo"}' \
  --out artifacts/bidirectional/$(date +%Y%m%d)/p1-submit.json

# ③ 查询：同一个 kb 身份查（终稿就绪后 result 非空）
python -m acps_cluster.bidirectional probe \
  --workspace acps_cluster/ws-kb \
  --peer-aic <SUPERVISOR_AIC> --skill research.status \
  --args '{"root_id":"rt-…"}' \
  --out artifacts/bidirectional/$(date +%Y%m%d)/p1-status.json
```

期望：

- ② 的产物是受理回执：`state=accepted`、`job_id` 非空、`note` 明确"不代表已完成"；
  回执 JSON 就在 `probe` 输出的 `product_previews[0].text` 里（终端也会打印前 400 字符），
  把其中的 `root_id` 拿出来给下一步；
- 再发一次同样的 `request_id` → `replayed=true`、`root_id` 相同、不会新建作业；
- ③ 先 `running`，作业结束后 `completed` 且 `result` 是终稿；
- 用其它身份查 → `forbidden`。

> 会真实创建一个后台作业（一次完整 leader 编排）。演示/联调建议用短任务，或直接复用
> 已有作业做状态查询。

## 6. 本地验证记录（2026-11-17）

```bash
python -m unittest acps_cluster.tests.test_root_orchestration -v   # 29 项
```

覆盖：

| 组 | 内容 |
|---|---|
| `RootStoreTest` | 幂等重放、同键冲突、scope 隔离、参数校验、绑作业/拒改绑、状态机、平台 taskId、列表过滤 |
| `RootServiceTest` | 受理成功/失败落账、重放不重复拉起、崩溃窗口补拉起、running/completed 映射、终稿取件、`forbidden`/`unknown-root`/`job-not-found` |
| `PepInboundSkillAllowTest` | 按 AIC×技能放行、同调用方其它技能不放行、未列调用方不放行、域技能不被白名单绕过、解析器容错 |
| `EntryCapabilityTest` | 入参面不含 `store/submit_fn/caller_aic`（防注入）、未验证调用方拒绝、受理-查询闭环、快速通道端到端（`<payload>` → 回执 JSON） |
| `AcsInvariantTest` | 新增两个能力后 supervisor 的 skills 投影仍只有 `research.orchestrate` |

另跑了 PEP/域/安全回归：`test_entry_hardening`、`test_p0_security`、`test_delegation`、
`test_domain*`、`test_visibility` 共 190 项（跳过项为缺 dev 证书，非失败）。

## 7. 与既有材料的关系

- **P0 报告**：为什么入口技能必须非公开、后台作业底座可复用；
- **双向调用文档**：两条互调任务的取证；本期把"业务根任务"落到 `root_id`，供后续
  `pack --root-task` 关联（P5）；
- **比赛口径**：现在可以写"入口 Partner 可提交科研需求并由 Supervisor 统一编排"，
  但**在问迹侧接通、跑出真实两条业务调用之前**，不要把它写成"已完成双向业务链"。

## 8. 未完成（按计划顺序）

| 项 | 归属 | 说明 |
|---|---|---|
| 域 grant 授权（替代技能白名单） | P1 收尾/P2 | **工具已就绪**：`acps_cluster.tools.entry_grant`（prepare/cosign/install/issue/verify/mint），见 [multi-entry-p2-grant.md](multi-entry-p2-grant.md)；`entry_grant` 单测覆盖章程双签与 PEP `domain-grant` 端到端 |
| 问迹侧入口分流 | P2 | 问迹收到外部需求 → 调 `research.submit`；收到 Supervisor 子任务 → 直接执行 |
| 回派子任务（Supervisor → 问迹 数据分析） | P3 | 设计见 [multi-entry-p3-dispatch.md](multi-entry-p3-dispatch.md)：根上下文注入、委派记账、按 root_id 取证 |
| 根预算/缺口报告/取消/超时 | P4 | 台账状态已预留，未产生 |
| 证据包按 `root_id` 关联两条平台任务 | P5 | `root_store.platform_task_id` 已记录，`pack` 尚未消费 |
| 多入口推广 | P6 | 公共适配层未抽取 |
| 演示视频/报告/PPT 更新 | 材料 | 等 P2/P3 真实跑通再改，避免过度声明 |

## 9. 配置项速查

| 变量 | 默认 | 作用 |
|---|---|---|
| `ACPS_ROOT_STORE` | `acps_cluster/ws-supervisor/root-tasks.sqlite3` | 根任务台账位置 |
| `ACPS_PEP_INBOUND_SKILL_ALLOW` | 空 | 按 AIC×技能入站白名单（`;` 分隔条目） |
| `ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW` | 空 | 上者的 supervisor 别名（取并集） |
| `ACPS_SUPERVISOR_SKILL_FASTPATH` | `0` | 是否把只读 `research.orchestrate` 也纳入快速通道；`submit/status` **始终**走快速通道 |