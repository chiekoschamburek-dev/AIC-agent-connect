# 发现：registry 上存在**未被使用的活跃 Agent 条目**（2026-10-11）

## 现象

`GET {ACPS_REGISTRY_URL}/api/v1/agent/client` 列出本账号 **10 个 Agent 条目**，
其中 4 个的 AIC **没有任何进程在使用**（心跳里找不到）：

| registry 名称 | AIC | 是否有进程在用 |
|---|---|---|
| 研枢-Scout-Partner | `…VTF97R.I8C7ZM.CNE8J2.0PWV` | ✗ 无 |
| 研枢-Critic-Partner | `…VTF97R.B0A8L0.1R65NK.0BYB` | ✗ 无 |
| 研枢-Frontier-Partner | `…VTF97R.BS9QTH.KPV1W1.0Y4B` | ✗ 无 |
| 研枢-Contradiction-Partner | `…VTF97R.X5NA66.ZKFLRV.1BT2` | ✗ 无 |
| 研枢-Writer-Partner（**重名**） | `…VTF97R.1OGDWX.U9K14M.0CDI` | ✗ 无 |
| 研枢-Synthesis-Partner（**重名**） | `…VTF97R.9HADYH.RLVWEP.07Y5` | ✗ 无 |

## 归属：全部来自 `ws-*-idswitch` 残留工作区

各工作区 `acs.final.json` 里的 AIC 对照（**在跑的服务用 `ws-*`**）：

| 正在使用（权威） | AIC | 残留工作区 | 其 AIC |
|---|---|---|---|
| ws-scout | `GMT5S6.4SZM68.0LC5` | ws-scout-idswitch | `I8C7ZM.CNE8J2.0PWV` |
| ws-critic | `M2NJ2G.A1BIB2.195J` | ws-critic-idswitch | `B0A8L0.1R65NK.0BYB` |
| ws-frontier | `V1E6AY.3YME2N.01K2` | ws-frontier-idswitch | `BS9QTH.KPV1W1.0Y4B` |
| ws-contradiction | `Z7TJ57.ORGGRY.0ZFO` | ws-contradiction-idswitch | `X5NA66.ZKFLRV.1BT2` |
| ws-writer | `PK47TC.WSY8VG.089N` | ws-writer-idswitch | `WATDMX.AQFZR5.0NQN` |
| ws-synthesis | `0J3LA2.XB2BZ3.07U1` | ws-synthesis-idswitch | `YDWSUJ.RBUQ6T.12KC` |
| ws-experiment | `JKYLU6.I4DOBA.097G` | ws-experiment-idswitch | `5CJ349.JNZ5VU.0E5E` |

**结论**：这些是早前"身份切换尝试"留下的注册残留——
新 AIC 被注册（甚至审批通过），但进程后来仍用旧 `ws-*` 工作区，
于是那些新 AIC **在平台上是 active 的、可以被人发现，却没有任何进程应答**。

## 影响评估（不夸大）

- **当前 8 个在跑 agent 的身份与 registry 完全对得上**（`ws-*` 的 AIC 全部命中心跳），
  所以**日常调用不受影响**。
- 真实风险有两个：
  1. **发现歧义**：registry 里同名条目（Writer、Synthesis 各两条），
     调用方按名字/hash 命中到无人应答的那条时会失败；
  2. **同类风险**：此前诊断脚本里写死的过期 AIC（`diag_alive_per_agent.py`）正是这类残留，
     已造成过一次"6 个伙伴离线"的误判。

## 建议（需你决定）

| 方案 | 做法 | 代价 |
|---|---|---|
| A. 停用残留条目 | 用 `POST /api/v1/agent/staff/{agent_id}/disable` 停用那 4–6 个无人应答的条目 | **需 STAFF/ADMIN 权限**（我们账号只有 CLIENT，实测 403），得请平台方操作 |
| B. 归档残留工作区 | 把 `ws-*-idswitch` 移到 `archive/`，避免后续诊断脚本再被它们误导 | 我们可自行完成、无副作用 |
| C. 暂不处理 | 记录在案，注册新资源 agent 时留意别复用这些 AIC | 风险保留 |

**推荐先做 B**（我们可自主、零风险），A 需要平台方配合。

> 与本次任务的关系：注册 CPU/GPU/KB 时要**避免复用**上表任何一个"无人应答"的 AIC，
> 否则新 agent 一上线就与残留条目同名冲突。

---

## 2026-10-15 复查更新（本条目的两条后续）

**① 上表 idswitch 期条目已全部处理（本议题闭环）**：账号 API 逐条复查——scout `I8C7ZM`、
critic `B0A8L0`、frontier `BS9QTH`、contradiction `X5NA66`、writer `PK47TC`、
synthesis `0J3LA2`、experiment `JKYLU6`（及更早代 kb `GIKJNY`）**均已于 2026-10-14
15:12:10–15:17:28 由用户从控制台删除**（`deleted_reason = User deletion`，全部生效、现均 403）。
无需 STAFF，也无需再做。

**② `NZUA9M`：换身份事故残留的活跃孤儿（处置路径修正：控制台所有者删除即可）**

- 记录：研枢-KB-Partner v1.0.0，`…NZUA9M.7BGY7W.0GDH`，is_active=true、无进程使用；
- 来历（账号 API 实证）：记录 id `01a13963-ae7a-7590-a485-5eb6f6595486` 与 10-14
  硬切事故中 wit 自动发起的重复注册（registrationId `01a13963…`）**UUID 逐字一致**；
  **15:49:56 提交 → 15:50:33 平台自动过审（37 秒，规则 registry-admission-2.0）**→ 铸证；
  后续修复认领了正确的 `D86STV`(v1.0.1)，该自动提交自此无人认领；
- 关键澄清：用户当天的"注销"（15:12–15:17 删掉 8 条旧记录）发生在它**出生之前 32 分钟**，
  平台侧对此记录从未有过任何停用/删除动作（disabled_at / deleted_at 均为空）——
  不是"注销失效"，而是"注销时它还不存在"；
- 影响：语义视图可见（filtered 视图不可见）→ 叮当可能选中无人应答的那条；
- **处置：所有者从控制台删除即可**（与 10-14 批同一路径，`User deletion` 属 owner 权限，
  **不需要 STAFF**；此前"需 STAFF"的说法仅针对 disable API 的 CLIENT 403）。
- **✅ 已闭环（2026-10-15 00:18）**：用户从控制台删除成功——`deleted_at=2026-10-15T00:18:14`、
  `is_deleted=true`（在跑的 `D86STV` 未受影响）；ATR 即刻 403；按名 filtered 仅剩 D86STV；
  语义索引因定期重建存在滞后，下次自然刷新时移除（今晚实测该索引约一天一次更新）。
