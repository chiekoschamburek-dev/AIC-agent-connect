# 本地 ↔ 服务器 同步审计（只读）

> 审计日期：2026-10-09 · 方式：对两边代码树做 sha256 清单比对（排除身份/数据/缓存），
> 再对"两边都有但内容不同"的关键文件做行级 diff。
> 复跑：`deploy/discovery_diagnostics/` 下 `make_sync_manifest.py` + `compare_sync_manifests.py`
> （服务器侧脚本已就位；服务器清单生成 → 取回本地 → 比对）。

## 0. 总量

| 项 | 数量 |
|---|---|
| 本地 `my-project` 受审文件 | **361** |
| 服务器 `my-project` 受审文件 | **229** |
| 仅本地有 | **234** |
| 仅服务器有 | **102** |
| 两边都有、内容不同 | **52** |
| 完全一致 | **75** |

**关键结论（先说重要的）**：服务器上没有**任何**本地不存在的代码内容 ——
所有"两边都有但不同"的文件都是**服务器更旧、本地更新**（`Compare-Object` 只有"仅本地"侧的行，
"仅服务器"侧全为 0 行）。也就是说：**不存在服务器上的手工改动会被覆盖的风险**，
问题纯粹是"本地的新东西没部署上去"。

---

## 1. P0：影响功能与安全的未部署差异

### 1.1 ⚠️ 发现门面：服务器上是"半应用"状态（本次审计最重要的发现）

| 事实 | 证据 |
|---|---|
| 服务器**没有** `discovery_metadata.py` | `ls` 不存在 |
| 7 个在跑的 partner + supervisor **都没有门面接线** | 逐个 grep `discovery_metadata` → 全部 **0 处** |
| 但工作区 `acs.final.json` 里**有**新门面文本 | scout 269 字/15 例、writer 327/3、synthesis 185/3、frontier 509/5、contradiction 361/3、experiment 484/3、supervisor 820/2 |
| 来源 | `enrollment.json` 里 `finalAcsSource=registry` —— 是**平台权威 ACS 被搬进工作区**（换身份时做的），不是代码派生的 |

**含义**：`acs.final.json` 目前**不参与服务运行**，所以现在跑得好好的；但它意味着

- 如果这些服务**重新注册**（或有任何路径用代码重新派生 ACS 草案），门面文本会**回退成旧文本**；
- 本地那份"发现门面"工作（描述/tags/examples 登记表）**在服务器上并不存在**，
  下次部署 `partner_*.py` 时必须**一并部署 `discovery_metadata.py`**，否则导入直接失败（ImportError）。

### 1.2 本地已修的安全/正确性改动，服务器上仍是旧版

| 文件 | 服务器缺的内容 |
|---|---|
| `aip_guard.py` | **壳标记中和**（`_neutralize`）：外部内容里自带 `[外部伙伴内容结束]` 就能提前关壳、让后续文本脱离"只作数据"约束 —— 本地已修（标注 2026-10-07 审查修复），**服务器仍是易受影响的旧版** |
| `cluster_common.py`（+40 行） | `PeerIdentityHooks` + `_PEER_AIC`（对端身份只从 mTLS 派生、不接受自报）；`RunConfig` 超时补丁改为从 dataclass 默认值推导而非写死（防上游改默认值时静默失效） |
| `supervisor.py`（+88 −12） | `research.orchestrate` 能力、`DelegationLeaderHooks`、深层/自动研究剧本等（服务器 940 行 vs 本地 1016 行） |
| `cluster_roster.py`（+25） | 名册新逻辑（登记即用、公开能力从 ACS 派生等） |
| 7 个 partner | 本地多 6 行（门面接线）、服务器多 2 行（旧的说明性代码） |

### 1.3 整块未部署的模块

| 模块 | 本地 | 服务器 | 影响 |
|---|---|---|---|
| `acps_cluster/delegation/` | 37 个文件 | **无** | PEP 准入、委托令牌、协作域、商业域、配额、对账全部未上线（这解释了为什么 `ws-*/logs/` 里只有 PEP 的 `pep-audit.jsonl` 曾在本地出现） |
| gpu / cpu / eval / model / kb / hpc / citation | 7 个 `partner_*.py` | **无** | 这 7 个 agent 不在服务器上（`ws-gpu` 里是本机开发身份，未在平台注册） |
| `capabilities/` 顶层目录 | 有 | 未见 | 需要确认服务器侧这些能力实现来自哪里（可能在 `acps_cluster/capabilities/` 或随 partner 一起） |
| `citation_verify.py` / `eval_runner.py` / `hpc_runner.py` / `kb_runner.py` | 有 | **无** | 依赖它们的 partner 即便部署也跑不起来 |

---

## 2. P0 续：发现一个**真的、正在生效的**问题 —— critic 的门面从未提交到平台

审计顺带查出：`ws-critic` 的 `acs.final.json`（= 平台权威版）仍是

```
描述 42 字 / examples 0 条
```

而本地代码里 critic 的门面是 **317 字描述 + 7 个 tag + 4 条 examples**（`paper.review` 112 字/7/4，
`citation.verify` 44 字/5/3）。

对照今天的提交记录：**scout 与 supervisor 的增强版已提交到平台**（新记录 + 换身份），
**critic（以及 writer/synthesis/frontier/contradiction/experiment）没有**。
所以"叮当问法能命中我们"目前主要靠 scout + supervisor 两条，
**"审稿/评审"类问法虽然实测 supervisor 排 #1，但 critic 自己仍是弱文本**。

> 这不是部署问题，是**提交问题**。要让 critic 也吃到门面，需要为它生成新版 ACS 并提交
> （平台硬约束：已 APPROVED 记录不能原地改 → 抬版本新建记录 + 按新 AIC 换身份，
> 与 scout/supervisor 今天走的流程完全一致）。

---

## 3. P2：脚本与文档（不影响运行，但会造成"两边跑不同版本"的误判）

| 项 | 情况 |
|---|---|
| 诊断脚本位置 | 本地已收进 `deploy/discovery_diagnostics/`；服务器 `deploy/` 下还留着**旧的 8 个 `diag_*.py` 副本**（本地已移走）→ 跑脚本时可能跑到旧版 |
| 脚本缺失 | `make_sync_manifest.py` / `compare_sync_manifests.py` 已上传；其余 `discovery_diagnostics/` 里约 47 个脚本服务器还没有 |
| `tests/` | 本地 19 个文件，服务器只有 1~2 个（`test_cluster_logic.py` 且内容不同）→ **服务器上没有任何回归测试** |
| `docs/` | 本地 5 份文档（本报告、AMP 合规、交接单、runbook、issue）服务器全无 |
| `deploy/.env.production` | 内容不同（服务器那份含真实凭据，**属预期**，不要用本地覆盖） |
| `acs.*.filled.json` | 多份内容不同或缺失（声明稿，见前一轮评审第 ⑥ 条） |

---

## 4. 仅在服务器上的文件（102 个，分类）

| 类别 | 例子 | 判断 |
|---|---|---|
| 换身份/历史备份 | `ws-*/acps/*.pre-idswitch-20261009-*`、`ws-supervisor/acps.old-20260927_114100/`、`cluster_roster.py.bak-*`、`.env.production.bak-*` | **保留**（回滚材料），可归档 |
| 旧诊断产物 | `deploy/diag_*.py`（8 个）、`_diag_agents.json` | 可清理（本地已移到 `discovery_diagnostics/`） |
| 归档脚本 | `partner_arxiv.py`、`leader_cli.py`、`register_local_accounts.py`、`delegate_smoke.py`（本地都在 `archive/`） | 服务器保留无害；本地归档是对的 |
| 运行数据 | `ws-*/acps/{acs.draft,acs.final}.json`、`discovered/*.json`、`run_task_hw.sh` | **保留**（工作区状态） |

**没有发现"只在服务器上、且承载独占功能"的代码。**

---

## 5. 结论与建议（按优先级）

1. **下次部署必须成组**：`discovery_metadata.py` + 7 个 `partner_*.py` + `supervisor.py` + `cluster_common.py`
   + `cluster_roster.py` + `aip_guard.py` —— 缺任一都会 ImportError 或行为不一致（`aip_guard` 那条还是安全修复）。
2. **部署前先跑部署红线**：`compare_sync_manifests.py --deploy-guard` 只列 P0 差异；
   再跑一次 `check_new_agent.py --all`，确保部署后 8 个 agent 仍全绿（身份/心跳/在线）。
3. **单独决策：要不要把 critic 等 6 个 agent 的门面也提交到平台**（§2）——
   方向与 scout/supervisor 相同，代价是每个都要"抬版本新建记录 + 换身份"。
4. **纯清理（可选）**：删掉服务器 `deploy/` 下 8 个旧 `diag_*.py` 与 `_diag_agents.json`，
   把本报告与其余 4 份文档同步到服务器 `docs/`（便于队友在服务器上直接查）。

---

## 6. 补测：弱文本 agent 在"本职问法"下到底进不进结果（决定要不要重新注册）

只读实测（每条问法 `limit=15`，判定"本尊"是否出现，而不是看 scout/supervisor 是否出现）：

| agent（现网描述字数） | 问法 | 本尊排名 |
|---|---|---|
| **Critic（42 字）** | 帮我找一个能审稿的智能体 | ✘ 未进前 15 |
| | 帮我找一个能给论文提修改意见的智能体 | ✘ 未进前 15 |
| | 帮我检查这篇论文的引用是否真实 | ✔ #2 |
| **Writer（327 字）** | 帮我找一个能写论文的智能体 | ✘ 未进前 15 |
| | 帮我起草一份文献综述 | ✔ #3 |
| | 帮我写论文大纲 | ✔ #1 |
| **Synthesis（185 字）** | 帮我精读这几篇论文并提炼要点 | ✘ 未进前 15 |
| | 帮我把文献整理成结构化表格 | ✔ #2 |
| **Frontier（509 字）** | 帮我找新兴研究方向 | ✔ #3 |
| | **帮我做选题调研**（本职） | ✘ 未进前 15（仅 2 条结果） |
| | 哪个方向正在爆发 | ✔ #1 |
| **Contradiction（361 字）** | 帮我找文献之间的矛盾 | ✔ #1 |
| | 根据文献冲突帮我提出研究假设 | ✔ #1 |
| **Experiment（484 字）** | 帮我跑个实验验证这个想法 | ✘ 未进前 15 |
| | 帮我在 GPU 上跑一段训练代码 | ✔ #1 |

**读法（重要）**：

1. **描述字数不是决定因素**：Contradiction（361 字）两条本职问法全 #1，而 Experiment（484 字）
   与 Frontier（509 字）仍有本职问法进不去 → 命中与否取决于**文本是否含用户说法**，不只是长度。
2. **有真实缺口，但缺口很窄**：最该补的是 Critic（"能审稿""给修改意见"完全不进）、
   Synthesis（"精读提炼"不进）、Frontier（"选题调研"不进——这是它的本职）。
3. **"进不去"不等于用户办不成事**：这些问法里 **supervisor 常常排 #1**，
   而 supervisor 能调度到对应 partner。用户从叮当点到 supervisor 一样能完成任务。
   所以缺口影响的是"**这个专职 agent 自己是否被看见**"，不是"任务能不能办"。

