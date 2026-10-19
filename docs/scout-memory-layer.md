# scout 论文记忆层（memory_partner 的第一阶段）

日期：2026-10-11　状态：**已上线并实测通过**

## 为什么要做

对集群历史任务实测统计：

```
检索类指令 281 条 → 其中重试/重派/追问 126 条（45%）
真正独立检索主题 105 条 → 唯一主题仅 58 个
跨任务主题重复率 ≈ 45%（高频：diffusion×21、models×19、kgc×6、mrr×6、子图生成×6）
```

接近一半的检索是同一批主题反复查。把结果存下来，能直接省掉这些网络往返。

## 实现（按约定的四步）

| 步骤 | 落地 |
|---|---|
| ① `ws-scout/memory/papers.sqlite3`：canonical 去重 + provenance + 关键词索引 | `capabilities/papers_memory.py`（`PaperMemory`） |
| ② 三个检索能力**记忆优先**，命中标注 `from_memory` | `partner_scout.py` 的 `_search_with_memory()` 接入 `arxiv.search`/`openalex.search`/`semanticscholar.search` |
| ③ 暴露只读能力 → "记忆伙伴"成立 | `memory.search`（按主题查已存论文）、`memory.stats`（库统计 + 最近检索出处） |
| ④ 单测 | `tests/test_papers_memory.py`（8 组）、`tests/test_scout_memory_wiring.py`（A/B/C/D/E 五段） |

### canonical 身份（关键设计）

同一篇论文在三个源里 ID 不同，按源各存会"越滚越胖但知识不涨"。键优先级：

```
arXiv ID  →  (source, native_id)  →  标题+首作者指纹
```

并且**额外用标题指纹做跨源合并**（归一化后标题完全相同且年份不冲突才合并，防误合）。
> 注：`ScholarRecord` 目前没有 DOI 字段（只有 `source`/`native_id`），DOI 位先留空，
> 未来加字段时补进 `_canonical_of` 即可。

### 命中口径与时效

- 按**归一化主题/关键词**命中（去停用词 + FTS5，退化为 LIKE），不按查询原文——
  历史数据证明措辞每次略不同（"diffusion models survey" vs "diffusion model survey"），按原文几乎全 miss。
- `fetched_at`/`refreshed_at` + TTL（`ACPS_SCOUT_MEMORY_TTL_DAYS`，默认 **7 天**）；过期不再命中。
- 开关 `ACPS_SCOUT_MEMORY=0` 可完全旁路（回到改造前行为）。
- **隔离**：记忆层任何异常都退化为纯联网检索，绝不影响检索本身。

## 实测结果（服务器真实检索）

```
[1] 第 1 次（联网）  : 3 条   1779 ms   from_memory=False
[2] 第 2 次（同措辞，联网被禁用）: 3 条      0 ms   from_memory=True   ← 证明没联网
[3] 换措辞（联网仍被禁用）    : 3 条      0 ms   from_memory=True
记忆库: papers=3 fresh=3 stale=0 sources={'arxiv': 3} ttlDays=7.0 fts=True
```

单测（服务器实跑）：

```
test_papers_memory       ✔ canonical 去重、跨源合并、命中口径、TTL、provenance、边界
test_scout_memory_wiring ✔ 记忆优先生效、跨源合并、故障退化、开关旁路、ACS 影响已识别
test_discovery_metadata  ✔（新增能力已登记检索门面）
test_cluster_logic       ✔
```

## ★ 关于"要不要重新注册"——实测结论（重要）

**进 ACS 投影的规则**（实测 8/8 吻合，不是推测）：

```
public=True  或  声明了 acps_skill 的能力  →  才会进 ACS
  · arxiv.fetch         public=False + acps_skill=域  → 在已注册 ACS 里
  · dataset.fetch       public=False + 无 acps_skill   → 不在（注册后才加，未重新注册）
  · scholar.deep_fetch  同上                          → 不在
```

因此：

| 记忆能力的 public | 进 ACS？ | 需要重新注册？ | 平台可发现？ |
|---|---|---|---|
| **False**（当前线上） | 否 | **不需要** | 否（仅内部/委托可用） |
| **True** | 是 | **需要** | 是 |

### 我实际踩到的后果（已修复，但必须记住）

我一度把两个记忆能力设为 `public=True` 以便"记忆伙伴"可被叮当发现，结果：

```
wit 拒绝启动：注册未就绪: failed
  delete or finish the remote registration, or use a new Agent version/workspace
  before changing the ACS
failure = acs_fingerprint_changed_after_submission
  acsFingerprint 26ab5fb9… → newAcsFingerprint 68c61ab9…
平台侧：scout 收录=0  alive=False        ← scout 直接离线
```

**即：把非公开能力改成公开 = 改变 ACS 指纹 = 该 agent 起不来，直到重新注册。**
已回退为 `public=False` 并清掉 enrollment 的 failed 状态，scout 已恢复（`alive=True`）。

> 若要"记忆能力可被平台发现"，正确做法是**改 agent version 后重新注册**
> （scout 现为 `1.10.0`，注册经验是改 `1.11.0`），重新注册需平台方 STAFF 审批。

## 复现与运维

```sh
# 单测
python tests/test_papers_memory.py
python tests/test_scout_memory_wiring.py

# 真实端到端（会真的联网一次，随后走记忆）
sh deploy/discovery_diagnostics/e2e_scout_memory_live.sh

# 观察记忆层是否在省检索
#   memory.stats → papers / fresh / stale / sources / recentAccess（hit/miss）
```

备份：`partner_scout.py.public-memory-20261011-203554`（若日后要切回 public 版重注册）
