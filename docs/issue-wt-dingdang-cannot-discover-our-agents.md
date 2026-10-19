# 问题诊断：wt.ioa.pub 上"发现不到我们自己的 agent"（leader 与 partner）

> 诊断日期：2026-10-08 夜（比赛初赛截止 2026-10-15）
> 诊断方式：**线上实测**（服务器 192.0.2.10 + 平台 wt.ioa.pub 真实 mTLS 调用），非推断
> 复现脚本：`my-project/acps_cluster/deploy/diag_*.py`（只读，只发发现查询）
> 结论一句话：**我们的 agent 在平台索引里是正常的，但在"首页推荐面板""平台在线追踪""自然语言一句话找智能体"三个入口全都看不到——这是可发现性（发现排名 + 在线状态）问题，不是注册/证书问题。**

---

## 1. 现象

在梧桐平台（`https://wt.ioa.pub/`）的叮当里，无论找 leader 还是 partner，都"发现不到自己的 agent"。

## 2. 实测：我们到底能不能被平台发现

用 scout 的真实证书走平台发现接口 `POST https://wt.ioa.pub/discovery/acps-adp-v2/discover`：

| 入口 | 查询 | 结果 |
|---|---|---|
| 技能词检索 | `arXiv 论文检索`（limit 20） | 共 9 员，**我们 5 个，Scout 排第 1** |
| 技能词检索 | `论文评审` | 共 10 员，**我们 4 个，Critic 排第 1** |
| 按名字 | `研枢` / `研枢-Scout-Partner` | **排第 1** |
| 技能词 | `contradiction.detect` | 共 3 员，Contradiction 排第 1 |
| 平台首页「最新上架」 | `type=recent, limit=6` | **我们 0 张**（前端真实参数） |
| 平台首页「热门智能体」 | `type=trending, limit=6` | **我们 0 张**（前端真实参数） |
| 叮当自然语言 | 「帮我找一个能做文献综述的智能体」 | 共 5 个结果，**我们 0 个** |
| 叮当自然语言 | 「帮我找一个能写论文的智能体」 | 共 8 个，我们 1 个（**研枢-Supervisor 第 2**） |
| 叮当自然语言 | 「帮我找一个能审稿的智能体」 | 共 11 个，我们 1 个（**研枢-Supervisor 第 1**） |
| 叮当自然语言 | 「帮我找一个能做科研数据分析的智能体」 | 共 9 个，我们 1 个（研枢-Supervisor 第 5） |

**所以注册、证书、ACS 提交、审核、索引全部是好的**：平台能查到我们的 AIC/技能，技能词一查我们常常排第一。
问题集中在两处：

1. **平台首页两块推荐面板（最新/热门）里没有我们**；
2. **一句话自然语言找智能体时，我们大多进不了结果集**（尤其"文献综述"这类最贴合我们能力的问法）。

补充实测（`/discovery/admin/alive-sync/status`）：平台侧 `aliveCount=139 / knownCount=255`，
即平台知道 255 个 agent、其中 139 个"存活"；**trending/recent 结果里每条都带 `aliveMap` 且 alive=true，
而我们的 agent 在任何响应里都拿不到 aliveMap 条目**——平台从来不知道我们还活着。

## 3. 根因

### 3.1 平台"在线/存活"来自 AMP 心跳，而心跳必须靠 AMP Forwarder 转发进平台总线

- 平台侧 alive 视图链路：`AMP Forwarder（Fluent Bit 尾随 amp_*.jsonl）→ 总线(Redpanda amp.* topics) → monitor-server 入库 → alive-sync → discovery-server`。
- 平台 AMP 规范明确写了：**AMP API 不接收 Agent 直接上报**（`acps-specs/09-ACPs-spec-AMP/ACPs-spec-AMP.md:1054`）；
  官方教程也写了这条链路里"中间「谁来采集、怎么入库」当作黑盒……**开发者通常不用改 Forwarder 配置**"
  （`acps-docs/tutorials/amp-agent-observability.md:10`、`:456-470`），并明确"**没有持续 heartbeat，
  Agent 在 alive 视图里会「消失」，即使业务还在跑**"（同文件 `:79`）。
- 我们这边的"头"是好的：**8 个 agent 的心跳 journal 全部在按 30s 持续增长**
  （`ws-*/logs/amp_heartbeat_<AIC>.jsonl`，2026-10-09 00:04-00:05 仍在写）。
- 但"中间"这一环我们完全没有：服务器上 **没有 fluent-bit / vector 等任何转发器**，
  也没有任何东西把 `logs/amp_heartbeat_*.jsonl` 推给平台。
- 结果：平台 monitor 从没收到我们的心跳 → alive 表里没有我们的 AIC → 不在 trending/recent（且不带 aliveMap），
  凡是按"在线/活跃"排序或过滤的入口都看不到我们。

### 3.2 自然语言找智能体时，我们的语义匹配面太窄

平台发现服务用 embedding 语义匹配（pgvector）。我们 agent 的 ACS 里
`description` 很短（一句话）、每个 skill 只有一个名字式的 `description` 和空 `examples`、`tags` 只有几个词；
而常年出现在结果里的对手（"基于多智能体协作的一站式科研助理平台""科研智能体协作工作台"）用的是
**覆盖面很宽的长描述 + 多能力 + 大量关键词**。于是问法一变（"文献综述""科研数据分析"），
我们就被挤出结果集——这不是平台 bug，是**我们在平台上的"门面文本"没有覆盖用户真实问法**。

### 3.3 另外发现：arXiv 检索 Partner 在平台上已被停用

`GET /registry-server/acps-atr-v2/acs/1.2.156.3088.1.BUPT.VTF97R.Q1EZOG.SSJTOF.0T4Q`
→ `403 / 40310 Agent status is not active (active:false)`。
该 agent 已于 2026-09-27 退役，但平台记录仍是"停用"状态；同时它的工作区里
`ws-partner/logs/amp_heartbeat_*.jsonl` 停在 09-27（本地也没跑了）。

---

## 4. 修复方案（按性价比排序）

### A. 把我们的 agent 送进平台"在线"视图（**这是 trending/recent 看不到的直接解**）

三条可选路径，A1 最省事：

1. **A1 走平台运维**：联系平台方（`/flux/docs` 文档站 + 接入培训渠道），请他们确认
   **外部 agent 的 AMP 心跳 ingestion 方式**，并把我们 8 个 AIC 的 heartbeat 纳入采集。
   需要向他们提供：机器、日志目录 `/opt/wit/my-project/acps_cluster/ws-*/logs/amp_heartbeat_*.jsonl`、
   AIC 清单。实测他们总线的外网端口 `192.0.2.20:19092` 从我们服务器**可达**，`5671`(AMQP) 也通，
   所以"我们侧自建 forwarder"在链路层是可行的，只差官方口径（topic 名/鉴权/是否允许）。
2. **A2 我们自建 Forwarder**：装 fluent-bit，按平台 `amp.*` topic 约定把 `amp_heartbeat_*.jsonl`
   投到 `192.0.2.20:19092`。**风险：未经平台确认就写他们的总线，属于越界操作，不建议先做。**
3. **A3 请平台手工标活/加白**：比赛场景下最快，但不解决长期问题。

> 说明：A 只影响"按在线/活跃"的入口与 UI 上的在线标记；不影响技能词检索（那条链路我们本来就靠前）。

### B. 把"门面文本"改成用户真实问法（**已在代码里落地，见第 7 节**）

对每个 leader/partner 的检索元数据做一轮增强：

- agent 级 `description` 改成"能做什么 + 适合什么场景 + 用户典型问法"三段式；
- 每个平台可见 skill 补 `tags`（中英文近义词）与 `examples`（**用户会怎么问**的完整句子）；
- skill `description` 改成"输入什么 → 输出什么"；
- **只动描述类字段**：能力 `id`、参数 schema、`public`/`acps_skill` 可见性声明一律不动，
  因此不改变任何调用契约与准入边界。

### C. 顺手处理

- arXiv 检索 Partner：确认它在平台上是"退役/停用"的预期状态（实测平台返回 `403 40310 not active`，
  本地心跳也停在 09-27），避免答辩时被问"为什么有个停用的 agent"。
- `研枢-Supervisor` 的 ACS `skills` 是**空数组**：平台只把它当"无技能实体"，
  自然语言按技能找时它靠描述硬撑。第 7 节的增强已给它补了 `research.orchestrate` 的门面文本与关键词，
  重新提交 ACS 后它才会带上这些检索信号。

---

## 5. 复现命令

服务器上（只读，脚本本体在仓库 `my-project/acps_cluster/deploy/discovery_diagnostics/`）：

```bash
cd /opt/wit/my-project
# 先把仓库里的诊断脚本同步到服务器（或直接在服务器仓库目录下执行）
/opt/wit/venv/bin/python acps_cluster/deploy/discovery_diagnostics/diag_discovery_rank2.py    # 技能词/名字检索排名
/opt/wit/venv/bin/python acps_cluster/deploy/discovery_diagnostics/diag_discovery_recent.py   # recent/trending 面板
/opt/wit/venv/bin/python acps_cluster/deploy/discovery_diagnostics/diag_final_check.py        # 首页面板 + 叮当式问法
/opt/wit/venv/bin/python acps_cluster/deploy/discovery_diagnostics/diag_alive_coverage.py     # aliveMap 覆盖
/opt/wit/venv/bin/python acps_cluster/deploy/discovery_diagnostics/diag_compare_acs.py        # 与"在线"agent 的 ACS 字段对比
```

> 说明：脚本默认按服务器绝对路径 `/opt/wit/my-project/acps_cluster` 定位集群（可用环境变量
> `YANSHU_CLUSTER_DIR` 覆盖），并且需要服务器上已装 `httpx`（用 `/opt/wit/venv/bin/python` 跑）。

平台 alive 概览（免登录）：`GET https://wt.ioa.pub/discovery/admin/alive-sync/status`

---

## 6. 一页结论

- **不是**注册/证书/审核问题：平台索引里我们都在，技能词一查常常第一。
- **是**两个可发现性问题：
  1. **平台不知道我们"活着"**（缺 AMP Forwarder → 无 heartbeats → 不在 alive 视图 → 首页推荐面板 0 张）；
  2. **我们的门面文本覆盖不了用户的自然语言问法**（"文献综述/科研数据分析"类问法进不了结果）。
- 第 2 条已在本仓库落地（第 7 节）；第 1 条需要平台口径（A1）。

---

## 7. 本轮落地：发现门面（discovery facade）代码改造

> 落地日期：2026-10-08/09 · 位置：`my-project/acps_cluster/`

### 7.1 改了什么

| 文件 | 作用 |
|---|---|
| `discovery_metadata.py`（新） | **集中登记**所有 agent / skill 的检索门面文本（15 个 agent 门面 + 36 个能力门面），提供 `agent_discovery_description()` / `apply_to_capabilities()` / `unregistered_ids()` |
| `partner_*.py`（14 个）+ `supervisor.py` | 构建 agent 时套用门面：`description=` 走门面，`capabilities=` 过 `apply_to_capabilities()` |
| `tests/test_discovery_metadata.py`（新） | 10 项单测：登记完整性、描述质量下限、**只改描述类字段**（id/可见性/schema 不变）、可调用对象、幂等 |
| `deploy/verify_discovery_facade.py`（新） | 离线验证：构建真实 agent 并调用 wit 的 `Capability.to_acps_skill()`，打印平台将看到的内容 |
| `deploy/discovery_diagnostics/`（新） | 本轮所有平台实测脚本归档（只读诊断，可复跑） |

### 7.2 验证结果（本地，未提交平台）

```
python -m unittest acps_cluster.tests.test_discovery_metadata   →  Ran 10 tests … OK
python acps_cluster/deploy/verify_discovery_facade.py            →  6 个已上线 agent 全部构建成功
```

改造前后对比（取 scout 为例，`Capability.to_acps_skill()` 的真实输出）：

| 项 | 改造前 | 改造后 |
|---|---|---|
| agent 描述 | 43 字（"科研信息收集员：跨 arXiv / OpenAlex / Semantic Scholar 三源检索文献…"） | **269 字**（含"适合：…"与"典型问法：…"） |
| `arxiv.search` skill 描述 | 40 字 | **78 字**（说明输入输出与适用场景） |
| skill tags | 3 个（`research/arxiv/scout`，偏内部标签） | **7 个**（含"文献检索/论文检索/学术搜索/literature-search"等用户词） |
| skill examples | **0 条** | **4 条**（"帮我检索近三年图神经网络在知识图谱补全方向的论文"…） |

其中 supervisor（leader）采用**追加检索面**而非替换：原文里"plan_gate / review_gate / 200 步 / AIC 溯源"
等对内与对评委有价值的说明**逐字保留**，检索问法语以"【找智能体时看这里】"段落追加在后。

### 7.3 尚未做的一步（需要决策）

**平台索引读的是注册表里那份 ACS，源码改动本身不会自动生效**——要让增强后的门面文本进入平台检索索引，
必须把新的 ACS 提交到平台。

⚠️ **平台侧硬约束（已核对 registry-server 源码 + 线上接口形态）**：

```python
# ACPs-community/registry-server/app/agent/service.py:544
if agent.approval_status in [ApprovalStatus.APPROVED, ApprovalStatus.PENDING]:
    raise AgentError(400, INVALID_STATUS_TRANSITION,
                     f"Agents in {agent.approval_status} status cannot be updated")
```

即：**已经 APPROVED 的记录不能原地改**（我们的 scout 等在平台上都是 `APPROVED + active`，
`GET /acps-atr-v2/acs/{aic}` 返回 `"active": true`）。因此"重新提交"在平台上只有两条可行路径：

| 路径 | 做法 | 代价 |
|---|---|---|
| **b1 新建一条记录** | 抬版本号（如 scout `1.10.0 → 1.11.0`）重新注册，拿到**新 AIC** | 同一能力在平台上出现两条记录（老条目仍在，且老 AIC 会被其他队引用）；若要真正切换还需把服务器工作区按新 AIC 重新登记。**注意**：registry 的 `(name, version)` 在活跃记录间必须唯一（`_ensure_agent_identity_available`，否则报 `AGENT_NAME_VERSION_EXISTS`），所以版本号必须抬 |
| **b2 请平台改** | 请平台把这批记录**退回可编辑状态**（或允许已批准记录更新描述类字段） | 依赖平台运维/评审；但**保留 AIC**，服务器与社区引用都不用动。比赛场景下最省事，**建议先走这条** |

> 相比之下，**尚未注册的 agent（如 leader `研枢-Supervisor`）没有这个约束**——它可以直接用新 ACS 注册，
> 一次到位。所以本轮同时准备了 scout 与 supervisor 两份稿。

已生成的两份提交稿（都由代码派生 + 三重校验）：

| 文件 | agent | version | 变化 |
|---|---|---|---|
| [acs.scout.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.scout.resubmit.json) | 研枢-Scout-Partner | `1.10.0 → 1.11.0` | 描述 65→269 字；4 个 skill 全部补描述/tags/examples（examples 0→3~4 条） |
| [acs.supervisor.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.supervisor.resubmit.json) | 研枢-Supervisor | `1.1.0 → 1.2.0` | 描述 398→820 字（原文逐字保留 + 追加检索面）；**新增 `research.orchestrate` skill**（原 ACS 的 `skills` 是空数组，这正是它在"文献综述"类问法里找不到的结构性原因） |

两份都是**从代码派生**的（`to_acps_skill()` 的真实投影），并依次通过：
registry `acsSchema.json`（jsonschema，不传 format_checker 复刻平台行为）→ 隐形字符扫描 →
端点/mTLS 方案引用 → 与构建后 agent 的**逐字段一致性**比对。

生成与自检命令（可复跑，需要 `--like` 载体保留端点/证书/provider 等字段）：

```bash
cd my-project
python acps_cluster/deploy/make_acs.py scout \
    --like acps_cluster/_platform_snapshot/acs.live.scout.json \
    --out acps_cluster/acs.scout.resubmit.json --version 1.11.0
python acps_cluster/deploy/check_resubmit_acs.py scout      # 与代码逐字段比对
python acps_cluster/deploy/validate_acs.py acps_cluster/acs.scout.resubmit.json
```

> 载体 `_platform_snapshot/acs.live.*.json` 是从服务器工作区取回的**现网 ACS 快照**（只读），
> 用来保留 `endPoints` / `certificate` / `provider` / `capabilities` / 安全方案等无法从代码推导的字段。

---

## 8. 注册通过后的线上复验（2026-10-09）

### 8.1 平台索引确实吃到了新文本（同一问法、同一 limit，前后对照）

| 问法（limit 15） | 改造前（10-08 实测） | 注册后（10-09 实测） |
|---|---|---|
| 帮我找一个能做**文献综述**的智能体 | 共 5 条，**我们 0 条** | 共 7 条，**我们 2 条**（Supervisor #2、Scout #3） |
| 帮我找一个能**写论文**的智能体 | 共 8 条，我们 1 条 | 共 9 条，**我们 2 条**（Supervisor #1、#3） |
| 帮我找一个能**审稿**的智能体 | 共 11 条，我们 1 条 | 共 12 条，**我们 2 条**（Supervisor #1、#2） |
| 帮我找一个能做**科研数据分析**的智能体 | 共 9 条，我们 1 条 | 共 11 条，**我们 2 条**（Supervisor #2、#6） |
| 帮我**检索某个主题的文献** | — | 共 10 条，**我们 4 条**（新旧 Scout #2/#5、Frontier #8、Supervisor #9） |
| 帮我做**选题调研和前沿分析** | — | 共 21 条，我们 3 条（新旧 Scout #3/#8、Synthesis #15） |

**首页面板**：「最新上架」从 **0 张 → 2 张**（研枢-Scout-Partner、研枢-Supervisor）；
「热门智能体」仍是 **0 张**（trending 与心跳/调用热度相关，属第 3.1 节的 AMP 问题，与描述无关）。

描述层证据：索引里同一条 Scout 的新版本（269 字描述、15 条 examples）排名**高于**旧版本
（65 字描述、0 条 examples）——同一身份、同一能力，差异只在门面文本。

### 8.2 ⚠️ 关键副作用：现在平台上有**两条记录**，而新记录没有本地身份

注册是"新建记录"路径（b1），因此每个成员多出一条 AIC：

| 成员 | 旧 AIC（**本地在跑**） | 新 AIC（索引里、**本地无人服务**） |
|---|---|---|
| 研枢-Scout-Partner | `…VTF97R.C6HTRB.5TNX0M.17SZ`（65 字 / 0 examples） | `…VTF97R.GMT5S6.4SZM68.0LC5`（269 字 / 15 examples） |
| 研枢-Supervisor | `…VTF97R.GGWW9N.KYWRB7.0IC8`（398 字 / 0 examples，**skills 为空**） | `…VTF97R.JXN8O8.98DOO8.0EB6`（820 字 / 2 examples，含 `research.orchestrate`） |

两条记录的 `endPoints` **完全相同**（`…:8445/rpc` / `…:8444/rpc`），但监听在那两个端口上的进程
用的是**旧 AIC 的证书**。而 AIC 是绑死在证书里的：

```
subject = CN = <AIC>
X509v3 Subject Alternative Name:
    URI:acps://<AIC>, IP Address:192.0.2.10
```

所以：**按新记录 AIC 去调用会打到一个"自称旧 AIC"的服务上**，身份对不上（AIP 侧会按证书 AIC 判定）。
即新记录目前是"**索引里在、实际不可服务**"的状态——检索能发现它，真正指名调用会失败。

### 8.3 收敛两条记录的两条路

| 路径 | 做法 | 结果 |
|---|---|---|
| **c1 请平台改旧记录**（推荐） | 让平台把**旧记录**（保留 AIC）的描述类字段更新为新版内容，新记录删除/停用 | 一身份一条记录，AIC 不变，服务器/社区引用都不用动；最干净 |
| **c2 迁移到新 AIC** | 为新记录走一次证书签发，把工作区身份指向新 AIC（`enrollment.json` + `client/server` 证书全套换），再重启服务；旧记录停用 | 需要一次身份迁移窗口；好处是版本号与内容一致 |

> 目前**不建议**就这样放着：检索结果里同一能力出现两条（旧条目还在参与排序，且调用会身份失败）。
> 若短期不想动服务，至少 c1 的第一步（让平台把旧记录内容更新掉）就足以消除"新记录不可服务"的坑。

### 8.4 仍然没解决的（与描述无关）

- 「热门智能体」0 张 + 全部记录 `aliveMap` 未覆盖 → 仍是 **AMP 心跳未进平台**（第 3.1 节 A1 路径）。
- `arXiv 检索 Partner`（已退役）在平台上仍是 `403 not active`；`研枢-GPU-Partner` 的 AIC
  `1.2.156.3088.1.1.GP5060…` 被平台判 `400 Invalid AIC format or checksum`（本地铸造身份，未在平台注册）。

---

## 9. 决定性实验：平台"按 AIC 查"到底卡在哪（2026-10-09）

用户反馈"在平台侧查 AIC 还是找不到"。实测平台首页的「在线状态查询」实现是：

```js
// https://wt.ioa.pub/ 首页内联 JS（showOnlineQueryState 附近）
body: JSON.stringify({
  type: 'filtered', limit: 5,
  filter: { conditions: [{ field: 'aic', op: 'eq', value: aic }] }
})
// 返回后：找到 acs → 看 aliveMap[aic].alive === true ? 在线 : 离线
```

### 9.1 三点实测（同一接口、同一时刻）

| 样本 | 语义检索（explicit）能查到？ | `filtered + aic` 能查到？ |
|---|---|---|
| 平台 trending 里 `alive=true` 的别人家 agent（协诊协调/验证/日志…） | 能 | **✔ 能（1 条）** |
| **在 recent 结果里、但 aliveMap 未覆盖**的第三方 agent（校园报修、校园物联网…） | 能（recent 里就有） | **✘ 0 条** |
| 我们自己的 4 条 AIC（Scout 旧/新、Supervisor 旧/新） | Scout 能（问法检索排 #3） | **✘ 0 条**（`extFilter.alive` 传 true/false 都一样） |

**结论：平台的 `filtered` 查询被"存活集合"约束**——只有被平台 alive 视图覆盖的 agent 才查得到。
"索引里有"（语义检索/首页最新上架能看到）与"按 AIC 查得到"是两套取数：
前者读注册表派生的技能索引，后者只读 alive 集合。

### 9.2 心跳：我们这边一直在写，平台一条都没收到

| 项 | 实测 |
|---|---|
| 本地心跳文件 | `ws-*/logs/amp_heartbeat_<AIC>.jsonl` **每 30s 一条**，最后一次写入在 **2–29 秒前**（8 个成员全在写；`ws-partner` 是 9-27 退役的 arXiv，已停） |
| 平台 aliveMap 覆盖我们的条目 | `trending`/`recent`/`extFilter.alive` 三种取数下 **都是 0 条** |
| 平台 alive 总盘 | `aliveCount=139 / knownCount=255`（我们在那 116 个"不知道死活"里） |

即：**心跳在产生、但从未进入平台**（平台 AMP 规范：API 不接收 agent 直接上报，链路是
`AMP Forwarder（Fluent Bit 尾随 amp_*.jsonl）→ 总线 → monitor-server → alive-sync`）。

### 9.3 因此，"按 AIC 查不到"与描述/版本/技能无关

只要心跳不进平台，`filtered` 那条路就永远查不到我们——**这一点已由上面 9.1 的对照实验证明**
（同样未覆盖 aliveMap 的第三方 agent 也一样查不到）。心跳一旦被平台采集：

- 「按 AIC 查在线」会从"未查询到"变成"在线"；
- 「热门智能体」trending 会开始出现我们；
- 所有带 `aliveMap` 的响应里会出现我们的 AIC。

**唯一动作**：让平台把我们的 heartbeat 纳入采集（A1）。需要给平台的信息：
机器 `192.0.2.10`、目录 `/opt/wit/my-project/acps_cluster/ws-*/logs/amp_heartbeat_<AIC>.jsonl`、
AIC 清单（见 `diag_collect_our_records.py` 输出）；或由我们侧自建 forwarder，
但目标总线（`192.0.2.20:19092` 实测可达）需要平台确认 topic 与鉴权。

---

## 10. 执行记录：心跳上报件 + 两个 agent 换身份（2026-10-09）

### 10.1 心跳上报：转发器已就绪，默认不投递

`deploy/amp_heartbeat_forwarder.py`（纯标准库）+ `deploy/wit-amp-forwarder.service` + `deploy/AMP_FORWARDER.md`。

- 扫描 `ws-*/logs/amp_heartbeat_*.jsonl` → 按字节偏移只投新增整行 → Kafka `amp.heartbeat`；
- dry-run 已验证解析链路（1250 条/轮，8 个文件），**且修掉一个真 bug**：dry-run 曾经也推进 checkpoint，
  会导致正式投递时漏掉这些行（现已改为 dry-run 绝不写偏移）；
- 平台总线只读探测结果：`amp.heartbeat`（**1 分区**）、`amp.heartbeat.alive-delta`（alive 增量源）、
  `amp.heartbeat.dlq`，共 14 个 `amp.*` topic；broker 匿名可握手（`ApiVersions error_code=0`）。

**未自动打开投递**：`amp.heartbeat` 是平台生产 topic，未经平台确认写入属越界。
需要平台给一句口径：① 由平台 Forwarder 采集我们的日志目录（最符合官方架构）；② 授权我们投递
（确认 topic/鉴权/键约定）；③ 给一个外部接入测试 topic 先验证。
打开后 1–3 分钟内 `aliveMap` 即应覆盖我们的 AIC，验收脚本已备好（`AMP_FORWARDER.md` 末尾）。

### 10.2 换身份：scout 与 supervisor 已切到平台新记录

**做法（不重新注册、不产生第三条记录）**：wit 的 CA 客户端支持**按 AIC 直接签发**
（`registry.fetch_eab_credential(aic)` → `ca.issue_certificate_pair(aic=…)`），
所以可以把已在平台注册并过审的新 AIC 的证书签回本地：

1. 登录平台（账号口令）→ 取该 AIC 的 EAB 凭据（字段 `aic/keyId/macKey/expiresAt`）；
2. 向平台 CA 走 ACME+EAB 签发 client/server 证书（私钥沿用现有，配对已校验 `True`）；
3. 更新工作区 `enrollment.json` 的三处 AIC（`identity.ref.aic` / `review.aic` /
   `review.authoritativeAcs.aic`）+ `certificatePaths`；用**平台权威 ACS** 覆盖 `acs.final.json`；
4. 搬运证书材料到线上工作区，重启 systemd 服务。

执行结果：

| 项 | scout | supervisor |
|---|---|---|
| 旧 AIC（已停用） | `…C6HTRB.5TNX0M.17SZ` | `…GGWW9N.KYWRB7.0IC8` |
| **新 AIC（在跑）** | `…GMT5S6.4SZM68.0LC5` | `…JXN8O8.98DOO8.0EB6` |
| 证书 CN/SAN | 新 AIC ✔ | 新 AIC ✔ |
| `enrollment.json` / `acs.final.json` | 新 AIC / v1.11.0（269 字描述）✔ | 新 AIC / v1.2.0（820 字描述）✔ |
| 平台记录 | HTTP 200 `active=true` | HTTP 200 `active=true` |
| 旧记录 | HTTP 403 `not active`（平台已停用） | HTTP 403 `not active` |
| 新证书可用性 | 用新证书做真实 mTLS 发现调用 → HTTP 200 ✔ | 同（重启后验证） |
| 心跳文件 | 已改为 `amp_heartbeat_<新AIC>.jsonl`，内容自报新 AIC ✔ | 待服务完成启动抖动 |

**回滚**：换身份前的完整工作区备份在 `/opt/wit/id-switch-backup-<时间戳>/{ws-scout,ws-supervisor}.tar.gz`；
工作区内还留了 `enrollment.json.pre-idswitch-<时间戳>` 等原件。

**过程中的两个坑（已修）**：
1. 签发脚本把证书写到了 `-idswitch` 副本工作区，线上仍是旧证书（症状：服务自称新 AIC、证书却是旧的）
   → 已把 client/server 证书与 trust-bundle 搬到线上工作区并重启；
2. `make_acs`/诊断脚本里出现过 `dataclasses` 未导入、dry-run 推进偏移等实现问题，均已修复。

### 10.3 换身份后的预期收益与仍需平台配合的部分

- ✅ 现在**跑着的服务与平台记录是同一个身份**：指名调用不会再打到"自称旧 AIC"的服务上；
- ✅ 描述层增强（269 字 / 15 条 examples）已经绑在**在跑的身份**上，不再只是"索引里的孤儿记录"；
- ✅ **心跳已进平台**（2026-10-09 13:34 起，B 方案）：`aliveMap` 覆盖我们的 AIC，
  「按 AIC 查在线」返回完整 ACS（首页显示"在线"），叮当问法排名进前列；
- ⚠️ 首页「热门智能体」面板仍未出现（那是活跃度/调用量排序，不是在线状态）。

---

## 11. 收尾：B 方案上线后的最终验收（2026-10-09）

心跳上报按 **B（我们侧投递平台 `amp.heartbeat`）** 落地，链路与验收见
`docs/amp-compliance-and-gaps.md` §9。关键前后对照：

| 检查项 | 上线前 | 上线后 |
|---|---|---|
| 平台 alive 总盘 | 137 / 257 | **145 / 265** |
| `aliveMap` 覆盖我们 | 0 条 | **✔ Scout、Supervisor 均被覆盖** |
| 平台"按 AIC 查在线" | 0 条 | **✔ 返回完整 ACS → 显示"在线"** |
| 叮当「文献综述」问法 | 我们 0 条 | **我们 2 条（#2、#3）** |
| 叮当「写论文」「审稿」问法 | 各 1 条 | **各 #1** |

结论：**"叮当发现不到自己的 agent"这个问题已经解决**——底层是身份一致性（换 AIC）+
检索门面（描述/tags/examples）+ 存活上报（心跳进平台）三件事共同作用的结果。





