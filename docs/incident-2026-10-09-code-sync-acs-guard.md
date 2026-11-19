# 事故与恢复记录：代码同步触发 ACS 指纹保护（2026-10-09）

> 起因：用户要求"把本地同步到服务器"。同步本身按只读审计→安全清单→暂存冒烟的顺序执行，
> 仍然触发了一个**此前没被识别的红线**。本文记录根因、处置与对当前状态的影响。

## 1. 红线（本次踩到，应当写进纪律）

**wit 有 ACS 指纹保护：agent 一旦注册完成，代码里"声明出来的 ACS"必须与注册版逐字一致。**

机制：

1. wit 每次启动把 `agent.capabilities` / `agent.description` 投影成一份 **ACS draft**，算指纹；
2. 与 `enrollment.json` 里注册时记下的 `acsFingerprint` 比较；
3. 不一致 → 拒绝启动，报：
   `failed delete or finish the remote registration, or use a new Agent version/workspace before changing the ACS`
4. 远程记录是 `APPROVED`（不可编辑）时**无法就地更新**，于是状态被写成
   `status=failed`、`failure=acs_fingerprint_changed_after_submission`，
   并带上 `newAcsFingerprint / previousAcsFingerprint / candidateDraftPath`。

**推论（关键）**：`discovery_metadata.py` 的"发现门面"会把**门面文本**注入 ACS 声明 →
**不能在"绑定已注册 ACS 的工作区"上部署带门面的 partner 代码**。
在服务器上，平台侧的注册文本本来就是权威版，门面本就不该生效。

## 2. 事件时间线

| 时间 | 事件 |
|---|---|
| 15:54 | 备份服务器代码 → 上传 186 个代码文件 → 暂存区冒烟 **PASS** |
| 15:55 | 落盘（20 个被覆盖文件已备份）→ 开始滚动重启 |
| 15:56 | `contradiction` 重启失败：**ACS 指纹保护命中**（新代码带了门面，注册版没带） |
| 16:03 | 中止滚动重启；把 8 个"碰 ACS 声明"的文件**回滚**到同步前 |
| 16:03–16:20 | 仍失败，错误变成 `failed None` → 取证发现 `enrollment.json` 已是 `failed` 状态，wit 卡在该状态 |
| 16:20 | 取证发现更严重的事实：**用户在平台上已把 6 个旧记录停用**（403），本地工作区却还绑着旧 AIC |
| 16:22–16:35 | 按"平台新 AIC + 换身份流程"逐个恢复：critic / experiment / writer / synthesis 全部切到新 AIC 并上线 |

## 3. 当前状态（16:50 全量验收，8/8 通过）

| agent | 服务 | 新 AIC（`…VTF97R.` 之后） | 平台记录 | 描述 / examples |
|---|---|---|---|---|
| 研枢-Scout-Partner | ✅ active | `GMT5S6.4SZM68.0LC5` | active v1.11.0 | 269 字 / 15 |
| 研枢-Supervisor | ✅ active | `JXN8O8.98DOO8.0EB6` | active v1.2.0 | 820 字 / 2 |
| 研枢-Critic-Partner | ✅ active | `M2NJ2G.A1BIB2.195J` | active v1.11.0 | **360 字 / 7** |
| 研枢-Writer-Partner | ✅ active | `1OGDWX.U9K14M.0CDI` | active v1.1.0 | **545 字 / 4** |
| 研枢-Synthesis-Partner | ✅ active | `9HADYH.RLVWEP.07Y5` | active v1.1.0 | **396 字 / 4** |
| 研枢-Experiment-Partner | ✅ active | `WN4C3S.0C0YA7.0J4R` | active v1.1.0 | **710 字 / 4** |
| 研枢-Frontier-Partner | ✅ active | `V1E6AY.3YME2N.01K2` | active v1.1.0 | **753 字 / 7** |
| 研枢-Contradiction-Partner | ✅ active | `Z7TJ57.ORGGRY.0ZFO` | active v1.1.0 | **594 字 / 4** |

验收口径（`full_acceptance.py`）：服务 active ✓ / 证书 CN = enrollment AIC ✓ / 心跳 ≤300s 且 AIC 一致 ✓ /
镜像目录有文件 ✓ / 平台按 AIC 查得到 ✓ —— **问题工作区 0 个**；
心跳链路 `wit-amp-mirror` / `wit-amp-sender` 均 active；平台 `aliveCount=146 / knownCount=273`。

> 意外收获：这次 6 个 agent 换到新 AIC 后，**门面文本真正进了平台索引**
> （描述 360/545/396/710/753/594 字、examples 4–7 条都是合并稿的内容），
> 比原计划（只提交、不换身份）更彻底——因为新记录本身就是用合并稿注册的。

## 4. 遗留：代码同步只完成了一部分（唯一未完成项）

### 4.2 代码同步只完成了一部分

| 已成功部署 | 已回滚（需再处理） |
|---|---|
| `delegation/` 整棵树（PEP/委托令牌/协作域/配额/对账） | `partner_scout/writer/synthesis/critic/frontier/contradiction/experiment.py` |
| `discovery_metadata.py`（纯新增，未被引用，无副作用） | `supervisor.py`（含 `research.orchestrate`） |
| `check_new_agent.py` / `sync_*` / `emergency_*` / `check_all_active_records.py` 等运维脚本 | `cluster_common.py`（含 **PeerIdentityHooks** 安全改进） |
| 文档（`docs/` 下 5 份） | `aip_guard.py`（含 **壳标记中和** 安全修复） |
| `merge_acs_for_resubmit.py` / `build_merged_resubmits.py` | `cluster_roster.py` / `agents.py` / `requirements.txt` |

**两处安全修复（`aip_guard` 壳标记中和、`cluster_common` 对端身份派生）目前仍未上线**，
因为它们与"被回滚的那批"混在同一批文件里。要上线它们，需要先解决 4.2 与 §1 的冲突：
要么让代码投影与注册版逐字一致（写一层"注册文本覆盖表"），要么接受"改声明 = 重新注册"。

### 4.3 备份与回滚材料

| 材料 | 位置 |
|---|---|
| 服务器代码（同步前） | `/opt/wit/sync-backup-20261009-155452/acps_cluster-code.tar.gz` |
| 被覆盖的 20 个文件 | `/opt/wit/sync-backup-20261009-155452/overwritten/` |
| scout / supervisor 工作区（昨日） | `/opt/wit/id-switch-backup-20261009-124140/` |
| 各工作区换身份前的 enrollment/acs | `ws-*/acps/*.pre-idswitch-<时间戳>`（本次 4 个新增） |

## 5. 纪律更新（写给下一次）

1. **部署前先问一句**：这批改动会不会改变代码投影出来的 ACS？（改 `@capability` 声明、
   agent `description`、门面文本、capabilities 列表 → 会）
2. **会改的，必须与"平台记录"同批处理**：先提交新 ACS → 换身份到新 AIC → 再部署带该声明的代码。
   顺序错了就会命中指纹保护。
3. **不会改的（delegation / 运维脚本 / 文档 / 纯新增模块）可以随时部署**，本次已验证安全。
4. **平台侧停用旧记录后，本地必须立刻换身份**：否则服务挂着"已作废的身份"对外服务，
   对方调用会身份失败（本次 4 个 agent 在这段时间里就是这样）。
5. 换身份后**必查三件事**：证书 CN/SAN = 新 AIC、`enrollment.json` 三处 AIC 一致、
   平台按 AIC 查得到。
