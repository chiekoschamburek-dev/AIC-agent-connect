# ACS 指纹保护：完整根因与正确部署流程

> 2026-10-09 事故的最终定论。**下次改代码/部署前先读这一页。**

## 1. 一句话结论

**wit 在 agent 注册完成后，会把"代码投影出来的 ACS"与"注册时记下的指纹"逐字比对；
不一致就拒绝启动。** 而我们的"发现门面"会改投影 → 所以必须让**投影恒等于注册版**。

## 2. 机制（实测确认，非推测）

| 环节 | 事实 |
|---|---|
| 指纹算法 | `wit.acps.provisioner._fingerprint(acs_draft_dict)` —— 对**整份 draft** 做哈希 |
| 参与哈希的字段 | `skills`（含 `description`/`version`/`tags`/`examples`…）与 `description` 等**声明类**字段 |
| 不参与 | `aic`、`active`、`lastModifiedTime`（实测改动它们指纹不变） |
| 存储位置 | `ws-*/acps/enrollment.json` 的 `acsFingerprint` = **draft 指纹**（15 个工作区实测一致） |
| 失败态 | `status=failed`、`failure=acs_fingerprint_changed_after_submission`，并写入 `newAcsFingerprint` / `previousAcsFingerprint` / `candidateDraftPath` / `remoteRegistration` |
| 远程记录 | 已 APPROVED 的记录**不可就地修改** → 只能抬版本重新注册（→ 换 AIC） |

## 3. 本次事故的两个真因（都踩到了）

### 真因 A：部署顺序（先代码、后换身份）

带门面的 `partner_*.py` 一旦上服务器，代码投影就与"旧注册版"不一致 → 守护拦截。

### 真因 B（更隐蔽）：`skill.version` 让**同一份代码**在两个阶段算出不同指纹

wit 在 **enroll 阶段**与 **serve 阶段**对同一份声明做投影时，`skill.version` 的取值不同：

- 实测（contradiction，修复前）：enroll 阶段指纹 `b877b332…`（投影**无** `version`），
  serve 阶段指纹 `76cc474b…`（投影**带** `version="1.0.0"`）；
- 于是**即使代码一个字没改**，重启也会被判成"ACS 变了" —— 这是最难查的一类假故障。

**修法（已落地）**：`discovery_metadata.apply_to_capabilities()` 在应用覆盖表时**一并补上 `version`**，
让投影在两个阶段完全一致。修复后实测两阶段都是 `b877b332…`。

## 4. 解决方案：注册文本覆盖表（已上线）

```
my-project/acps_cluster/registered_acs_overlay.json     ← 由平台现网 ACS 生成
my-project/acps_cluster/discovery_metadata.py           ← 生成声明时优先读它
```

取值优先级：**覆盖表（注册文本） > 门面登记表 > 代码原声明**。

- 命中的 skill/agent → 返回注册版 `description` / `version` / `tags` / `examples` → **投影 ≡ 注册版**；
- 未命中的（本地开发、新 agent 首次注册）→ 照旧享受门面的"用户问法"增益。

工具链：

| 脚本 | 作用 |
|---|---|
| `deploy/discovery_diagnostics/build_registered_overlay.py` | 从 `_platform_snapshot/acs.live.*.json` 生成覆盖表 |
| `deploy/discovery_diagnostics/verify_overlay_matches_registered.py` | 校验投影与注册文本逐字一致（13/13 skill） |
| `deploy/discovery_diagnostics/verify_projection_stability.py` | 校验投影稳定性（enroll/serve 同指纹） |
| `deploy/discovery_diagnostics/verify_guard_on_server.py` | **部署前**在临时工作区真跑 `enroll()`，确认不触发守护 |
| `deploy/discovery_diagnostics/rebuild_enrollment.py` | 发现工作区身份字段被写坏时，按健康模板重建 |

## 5. 标准部署流程（改代码时照做）

```bash
# ① 改代码（含门面/能力声明）
# ② 若平台记录已变（重新注册过），先重生成覆盖表
python deploy/discovery_diagnostics/build_registered_overlay.py
python deploy/discovery_diagnostics/verify_overlay_matches_registered.py   # 必须 0 不一致
python deploy/discovery_diagnostics/verify_projection_stability.py         # 必须 0 不一致

# ③ 传到服务器暂存区，做部署前验证（不碰生产）
#    ⚠ 暂存区是"一次一放"的草稿区：只放**本次**要部署的文件（同名覆盖），验证通过后
#      **逐文件**从暂存区落盘；**禁止**整目录拷回生产——2026-10-15 实测暂存区内残留着
#      2026-10-09 的旧拷贝（窄门面 discovery_metadata.py + 8-agent 旧 overlay），整目录
#      回搬会把它们带回现网。旧暂存已归档到
#      /root/audit-archive-20261014/sync-staging-stale-20261009/，勿再回搬。
scp <改动文件> 服务器:/opt/wit/sync-staging/acps_cluster/
ssh 服务器 "cd /opt/wit/my-project/acps_cluster && sudo -u wit /opt/wit/venv/bin/python \
    deploy/discovery_diagnostics/verify_guard_on_server.py"               # 每个 agent 都要 ✔

# ④ 落盘 + 重启（一次一个，观察 active 再下一个）
# ⑤ 全量验收
sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/full_acceptance.py
```

**红线**：
1. 改声明类字段（`@capability` 的 description/version/tags/examples、agent description）→ **必须**同批更新覆盖表；
2. **不要**在没有覆盖表的情况下把带门面的代码部署到"已注册"的工作区；
3. 换身份/重新注册之后 → **立刻**重生成覆盖表，否则下次重启就会踩真因 B。

## 6. 第二个红线：**换 AIC 会让 agent 在平台上"消失"一段时间**（本轮血的教训）

平台的可检索性 / 在线判定，依赖 **alive 索引里"已知的 AIC"**：

- alive 视图由一个**快照**（`snapshotGeneratedAt`）+ 之后的 Kafka 增量维护；
- **新 AIC 不在快照里**，要等平台把它纳入（实测 `knownCount` 从 273 → 279 是缓慢增长的）；
- 在这之前，**该 agent 的 AIC 查不到、`aliveMap` 里没有它** → 叮当搜不到、调用不了。

实测证据（2026-10-09 23:00–23:25）：

| agent | AIC 情况 | 按 AIC 查 | 恢复方式 |
|---|---|---|---|
| supervisor / writer / synthesis / experiment | **未换过 AIC**（一直是注册值） | ✔ 在线 | — |
| scout / critic / frontier / contradiction | 换成**新 AIC** | ✘ 查不到（**新老 AIC 都查不到**） | **切回原 AIC 后立刻恢复** ✔ |

**纪律**：
1. **不要在比赛/演示临近时换 AIC**。"换身份"只在"注册了新版本"这种必须的场景做；
2. 换完之后若发现平台查不到，**最可靠的恢复是切回原来那个"平台已知"的 AIC**（对应的远端记录
   通常仍 `active`），而不是等新 AIC 被纳入；
3. 工作区一旦出现"身份字段丢失 → wit 自动重新注册"，**会静默产生一个全新 AIC**，
   agent 就此从索引里消失。发现心跳里的 `aic` 与预期不符时，立刻按上一条处理。

## 7. 本次事故留下的真实损伤（已全部修复）


| 项 | 处置 |
|---|---|
| contradiction / frontier 起不来 | 两个真因叠加；重建 enrollment（身份字段被写坏）+ 修复 version 投影 → **已 active** |
| 6 个 agent 曾挂"已作废身份" | 用户重新注册后逐个换身份 → **已修** |
| 代码同步只完成一半 | **已完成**：`delegation/` + 门面 + security 修复（`aip_guard` 壳标记中和、`cluster_common` 对端身份）+ 覆盖表 均在服务器上 |
| 备份 | `/opt/wit/sync-backup-20261009-155452/`、各工作区 `*.pre-guardfix-*` / `*.pre-rebuild-*` / `*.pre-idswitch-*` |

> ⚠️ 注意：**另外 6 个服务（scout/supervisor/critic/writer/synthesis/experiment）当前进程里跑的还是旧代码**，
> 磁盘上已是新代码 —— 需要一次重启才会生效（下次部署/重启时自然完成）。
