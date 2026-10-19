# KB 门面文案加宽 + 重新注册（1.0.0 → 1.0.1）

日期：2026-10-11　状态：**注册已提交，待平台方 STAFF 审批**

## 起因：叮当里"找不到 KB"

用户反馈在叮当里找不到 KB。实测排查（全部有据）：

```
心跳      本地 73 条（最后一条 22 秒前）；镜像 19956 字节与本地一致；
          sender 位点 {'amp_heartbeat_kb.jsonl': 19956}，未投递 = 0 字节
平台      alive=True   aliveLastSeenAt = 2026-10-11T12:42:00Z
服务      wit-yanshu@kb active，监听 0.0.0.0:8599
端点一致  注册 publicEndpoint = ACS endPoints = https://192.0.2.10:8599/rpc
索引文案  平台侧 description 209 字，与本地门面**逐字一致**，3 个 skill 都有 tags/examples
```

**心跳与索引都没问题。** 真正的原因是**问法匹配**：

| 叮当式问法 | 结果 |
|---|---|
| `知识库` | ✔ KB 第 **1/9** 位 |
| `查文献` | ✘ **KB 不在**（返回 8 个，被 scout 及别家文献智能体占满） |
| `KB` / `研枢-KB-Partner` / `知识库查询` | ✘ **ReadTimeout**（连续两次一致，平台侧不稳定） |

即：KB 被描述成"**库**（查询/统计/脱敏）"，而用户会用"**查文献/找资料/查数据**"这类问法，
原文案里没有这些词 → 匹配不到。另有一个**平台侧 bug**：发现接口对纯 ASCII 查询稳定超时
（建议单独反馈）。

## 改了什么

**只改检索文案，不动能力声明**（能力面/公开性不变，行为不变）：

1. `discovery_metadata.py` → `AGENT_FACADES["yanshu.kb-partner"].description`
   **209 → 365 字**：原文一字未删，把用户真实问法并进同一段：
   `查文献 / 找文献 / 查论文 / 找论文 / 查资料 / 找资料 / 查数据 / 找数据 / 查一下库里有什么 /
   文献库里有啥 / 检索资料 / 按条件查 / 字段查询 / 条件筛选 / 批量查 / 数据统计 / 统计一下 /
   分组统计 / 求和 / 计数 / 分布统计 / 排名 / 对比不同机构 / 按角色看数据 / 权限分级 / 脱敏 /
   哪些字段被隐藏 / 受控写入 / 把资料入库 / 录入数据 / 登记数据`
   tags 也补上 `查文献/找论文/查资料/查数据`

2. `discovery_metadata.py` → `CAPABILITY_FACADES["kb.query"]`
   description 里加"查文献、找论文、查资料、查数据"与聚合统计同义词；
   **tags 6 → 14、examples 3 → 6**

3. `partner_kb.py` → `version` `1.0.0` → `1.0.1`（与提交的草案对齐）

### ⚠️ 两个必须记住的坑

**坑 1：`search_surface` 会丢弃门面 description。**
`agent_discovery_description()` 在 `search_surface` 非空时返回
`f"{default}\n{facade.search_surface}"`——那个 `default` 是 `partner_kb.py` 里传的**短句**，
**不是**门面 description。我一开始用 `search_surface` 追加，实测得到
"287 字全是短句+检索面、原 209 字描述被丢掉"，**反而更差**。最终改为并进 `description`。

**坑 2：已注册的 skill/agent 会走"注册文本覆盖表"，改门面不生效。**
`apply_to_capabilities()` 的优先级是
**`registered_acs_overlay.json` 覆盖表 > 门面登记表 > 代码原声明**。
KB 不在覆盖表里（overlay 仅含 8 agent / 13 skill），所以改门面生效；
**若 KB 将来进了覆盖表，必须同时改覆盖表**，否则平台文本与代码投影不一致。

## 注册怎么提交的（安全做法）

KB 服务正在 8599 上跑。若直接用新代码重启它，会因指纹变化被 wit 拒绝启动
（与 scout 记忆能力那次同一个保护）。所以：

```sh
sh deploy/discovery_diagnostics/reregister_kb.sh
```

- 在**临时工作区** `/opt/wit/tmp/kb-reregister` 提交，**不动 ws-kb 的证书/终稿** → 线上 KB 不受影响
- `publicEndpoint` 沿用 **8599**（避免 KB 首次注册时"探测用 8599、正式配 8464"导致的
  `acs_fingerprint_changed_after_submission`）
- agent version 提到 **1.0.1**

提交结果：

```
status      = pending_review
registrationId = 01a12b18-39ee-7637-964c-bce8d4c532f3
acsFingerprint = 2c2d1f6a9b24c8a4ea9f4b1a8675effb7c7e…
platform    : 研枢-KB-Partner version=1.0.1 PENDING active=False aic=待发证
              研枢-KB-Partner version=1.0.0 APPROVED active=True  aic=…GIKJNY.9AL6AY.10UD（仍在服务）
```

草案核对（`verify_kb_reregistration.sh`）：

```
version=1.0.1  endPoints=['https://192.0.2.10:8599/rpc']
description 365 字，含「查文献/找资料/统计一下/权限分级」全部为 True
kb.query  desc=119 字  tags=14  examples=6
```

## 批准后怎么切换

```sh
sh deploy/discovery_diagnostics/cutover_kb_101.sh
```

它会一次做完（顺序有依赖，不能拆）：

1. 确认已获批且证书已铸出（否则终止）
2. 备份现有 ws-kb 身份产物到 `/opt/wit/tmp/kb-identity-backup-<时间戳>`
3. 换入新身份（enrollment / 终稿 / client / server / secrets）
4. **重建 trust-bundle**（新工作区默认缺中间 CA → 令牌永远验签失败，KB 上次踩过）
5. 重启 KB 服务
6. **用新 AIC 重建域章程** `yanshu-kb.r1.json`——章程里记着 KB 的 provider AIC，
   换身份后不重建会导致域路径失效
7. 验收：平台在线 + 准入矩阵
8. 发现侧复核：`查文献` 这类问法现在能否命中 KB

## 回滚

```sh
# 代码
cp partner_kb.py.pre-version-bump-<时间戳> partner_kb.py
# 身份（若已切换）
cp /opt/wit/tmp/kb-identity-backup-<时间戳>/* ws-kb/acps/
```
