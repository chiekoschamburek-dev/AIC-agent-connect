# 跨智能体双向调用：现状、修法与取证

> 面向赛题「AI 技术应用（20 分）」里的 **跨智能体双向通信**，以及「提交平台后台访问量
> 截图与互联交互记录」。本文只写能复核的事实与操作，不把"我们调用过别人"写成"双向"。

## 0. 一句话结论

**原实现确实是单向的**：出站方向（研枢作为 Leader 调伙伴/问迹）在跑；入站方向（对端作为
Leader 调研枢）**只对白名单里的平台编排方（叮当）开过**，对问迹这类"互相调用"的对端没有
通道；报告里的"反方向实测"早于 2026-10-21 的 PEP 收口，也没有归档 taskId。现在补三件事：

1. 入站**技能快速通道**（`ACPS_SUPERVISOR_SKILL_FASTPATH=1`）：对端指名
   `research.orchestrate` 时直接执行、不跑 LLM，秒级返回；
2. **验证与取证工具链** `acps_cluster/bidirectional/`：`doctor` / `probe` / `ledger` / `pack`；
3. **判定口径**写进代码：两条方向相反的平台任务、caller/callee 互换、taskId 不同、
   都 `completed`、都有产物，才算 PASS。

## 1. 为什么原来是单向（代码事实）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 出站链路完整：Leader 侧有 `acps_delegate` / `acps_delegate_public` / 资源令牌注入，任务落库 `direction='outbound'` | `agents/supervisor.py`、`delegation/client.py` |
| 2 | 入站入口收窄：supervisor `allow_unscoped_public_entry=False`，只保留 `research.orchestrate` 一个公开技能 | `build_supervisor_agent`、`delegation/pep.py` |
| 3 | AIP v2 标准客户端**不带技能 id**：`AipRpcClient.start_task()` 只填 `sessionId/userInput`，`TaskCommand.commandParams` 由调用方自己写；平台/叮当/问迹的常规调用因此 `skill is None` | `acps_sdk/aip/aip_rpc_client.py` |
| 4 | `skill is None` 且无令牌时，`pep.evaluate` 依次过 L0（要求全部能力公开，不成立）、L1（调用方不在 `leader_aics`）、L1'（调用方不在 `ACPS_SUPERVISOR_INBOUND_ALLOW_AICS`）、L0'（`unscoped_public_entry_open()` 对 supervisor 恒 False）→ **`deny / no-credential`** | `delegation/pep.py` §裁决 |
| 5 | 生产默认开 PEP（`deploy/env.cluster.append` 写 `ACPS_PEP_ENABLED=1`），仓内模板只说"叮当要列进 `ACPS_SUPERVISOR_INBOUND_ALLOW_AICS`"，**未包含问迹**（实际生产值在服务器的 `.env.production`，仓内不可见） | `deploy/env.cluster.append`、`.env.example` |
| 6 | 报告里"问迹 → 编排器"只有终端截图级记录，**没有归档 taskId**（表 5-1-1 自己写明"未离线归档"） | `output/report_review/v2_body.txt` |

结论：反方向不是"没写代码"，而是**准入没开 + 没有工具产出可复核证据**。这两点都不该靠
"再跑一次手工命令、截个图"解决。

## 2. 双向成立的判定口径（`pack` 的 PASS 条件）

| 检查 | 要求 |
|---|---|
| 两个方向都有任务 | 出站、入站各一条（不是同一条任务换个说法） |
| 终态 | 两条都 `completed`；`awaiting-completion` 只算"已交付未关单"，判 `incomplete` |
| 身份对称 | 出站 `caller=本机, callee=对端`；入站 `caller=对端, callee=本机` |
| taskId | 两条任务 id 不同且非空 |
| 产物 | 两侧都有产物（数量与字符数记录在案，正文留在平台任务里） |
| 本地错误 | `local_error` 为空 |
| 双侧对账（可选） | 给了 `--peer-workspace` 时，同一 taskId 必须在对端任务库也能查到，且 caller/callee 一致；查不到只告警（对端可能不是本仓运行时），不改变 PASS |

任一不满足 → `verdict=incomplete`，逐条写原因。**不凑数**：宁可写"单向 + 缺口"，也不
把两条同向调用说成双向。

## 3. 新增的代码与配置

| 文件 | 作用 |
|---|---|
| `acps_cluster/bidirectional/models.py` | 回执数据模型（`TaskReceipt` / `ProductSummary`），只留可对账字段 |
| `acps_cluster/bidirectional/ledger.py` | 只读任务库（`aip_tasks` / `aip_task_events`），缺列降级、双向配对 |
| `acps_cluster/bidirectional/identity.py` | 从 `ws-*/acps/` 读 AIC / 端点 / mTLS 证书 / trust-bundle，按 AIC 找对端端点 |
| `acps_cluster/bidirectional/probe.py` | 真实 AIP 客户端探针（`acps_sdk` + mTLS），支持 `commandParams` 技能指名；状态机含 `awaiting-input→continue`、`awaiting-completion→complete`；客户端可注入（离线单测） |
| `acps_cluster/bidirectional/evidence.py` | 对称性自检 + Markdown 渲染 + `SHA256SUMS` |
| `acps_cluster/bidirectional/skill_entry.py` | 入站技能快速通道 provider（supervisor 用；未命中回落 LLM） |
| `acps_cluster/bidirectional/cli.py` | `doctor` / `probe` / `ledger` / `pack` |
| `acps_cluster/core/skill_call.py` | 从 AIP 入参解析 `(skillId, args)`；`static_router` 与快速通道共用 |
| `agents/supervisor.py` | `_orchestrate_payload` 抽出 + `ACPS_SUPERVISOR_SKILL_FASTPATH=1` 接线 |

配置（`.env.production`）：

```bash
# 入站技能快速通道：对端以 commandParams 指名 research.orchestrate 时不跑 LLM
ACPS_SUPERVISOR_SKILL_FASTPATH=1

# 仅"路径 B（免 skill 全链编排）"需要：把对端 AIC 列进白名单（逗号分隔）
# 问迹：1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN
# 叮当（平台调用方，历史身份末两段会轮换）：见 .env.example 的注释
ACPS_SUPERVISOR_INBOUND_ALLOW_AICS=<对端 AIC>

# 自证场景里让 kb 的公开能力走确定性路由（可选）
ACPS_KB_STATIC_ROUTER=1
```

改完重启：`systemctl restart wit-yanshu@supervisor`（kb 也改了就一并重启）。

## 4. 两条入站路径

| | 路径 A：技能指名（推荐） | 路径 B：免 skill 全链编排 |
|---|---|---|
| 调用方怎么发 | `commandParams={"skillId":"research.orchestrate","args":{"task":"…"}}`（或正文 `{"skill":…,"args":…}`） | 标准 `start_task`，只带 `userInput` |
| 准入依据 | 公开技能 L0（`public-skill`），不需要白名单 | 调用方 AIC 必须在 `ACPS_SUPERVISOR_INBOUND_ALLOW_AICS`（`inbound-allowlist`） |
| 本机执行 | 快速通道直接执行 `research.orchestrate`，秒级、零 LLM | 完整 leader run（几十步、分钟级、有模型成本） |
| 适合 | 双向验证、连通性回归、平台调用量 | 真的把科研任务交给研枢编排 |
| 限制 | 调用方要能写 `commandParams`（`AipRpcClient.start_task()` 不暴露） | 慢且贵；不建议为"证明双向"反复跑 |

## 5. 操作手册

### 5.1 开跑前自检

```bash
cd /opt/wit/yanshu-AIC
python -m acps_cluster.bidirectional doctor \
  --workspace acps_cluster/ws-supervisor \
  --peer-aic <对端 AIC> --discover
```

`READY` 的含义：身份文件齐全（AIC/端点/证书/trust-bundle）、`acps_sdk` 与 `httpx` 可导入、
任务库可读；对端命中发现、且能解析出端点。`doctor` 还会提示"对端是否在入站白名单""快速
通道是否打开"——这两条是**当前"单向"的直接原因**。

### 5.2 自证：supervisor ⇄ kb（不依赖任何外部团队）

两个都注册在平台、能力都公开、都能确定性执行，适合反复跑。

```bash
# 方向 A：supervisor → kb（kb.schema，只读结构卡）
python -m acps_cluster.bidirectional probe \
  --workspace acps_cluster/ws-supervisor \
  --peer-aic <KB_AIC> --skill kb.schema --args '{}' --discover \
  --out artifacts/bidirectional/$(date +%Y%m%d)/probe-supervisor-to-kb.json

# 方向 B：kb → supervisor（research.orchestrate，走快速通道）
python -m acps_cluster.bidirectional probe \
  --workspace acps_cluster/ws-kb \
  --peer-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
  --skill research.orchestrate --args '{"task":"为 RAG 综述排一个调度计划"}' \
  --out artifacts/bidirectional/$(date +%Y%m%d)/probe-kb-to-supervisor.json
```

> `KB_AIC` 取 `acps_cluster/ws-kb/acps/acs.final.json` 的 `aic`（见
> [agent-aic-roster.md](agent-aic-roster.md)）。方向 B 的回执会同时落在
> `ws-supervisor/acps/aip-tasks.sqlite3` 的 `inbound` 行（运行时收到任务）。

取证（本机 = supervisor）：

```bash
python -m acps_cluster.bidirectional pack \
  --workspace acps_cluster/ws-supervisor --peer-aic <KB_AIC> \
  --peer-workspace acps_cluster/ws-kb \
  --out-receipt artifacts/bidirectional/<日期>/probe-supervisor-to-kb.json \
  --self-label 研枢-Supervisor --peer-label 研枢-KB-Partner \
  --out-dir artifacts/bidirectional/<日期>/supervisor-kb
```

> 加了 `--peer-workspace` 会把 kb 的任务库也读一遍：**同一条 taskId 在两侧都存在、
> caller/callee 一致**才算"可对账"。这条检查是非必填的——探针直连不写对端任务库时
> 会显示"查不到"，但不影响 PASS 判定（证据包会写清原因）。

### 5.3 对外：supervisor ⇄ 问迹

方向 A（研枢 → 问迹）：

```bash
python -m acps_cluster.bidirectional probe \
  --workspace acps_cluster/ws-supervisor \
  --peer-aic 1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN \
  --skill wenji.data_query \
  --task "查询 2023 年各国销售额前五" --discover
```

> 对问迹这类"按自然语言干活"的伙伴，用 `--task` 给正文、`--skill` 只做技能点名：
> `commandParams` 里带 `skillId`，正文仍是人话，避免把 JSON 直接丢给对端模型。

方向 B（问迹 → 研枢），二选一：

- **B1（推荐，快）**：在问迹服务器上用同一份工具，工作区指向问迹自己的
  `.wit/wenji-aip-agent`（目录结构与 `ws-*/acps/` 一致）：

  ```bash
  python -m acps_cluster.bidirectional probe \
    --workspace .wit/wenji-aip-agent \
    --peer-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
    --skill research.orchestrate --args '{"task":"给一个科研任务调度建议"}'
  ```

  把生成的 probe 回执文件带回研枢侧，`pack` 时用 `--in-receipt` 传入（工具会自动把
  "对端视角的出站"翻转成"本机视角的入站"，并写进备注）。

- **B2（不改问迹脚本）**：把问迹 AIC 写进 `ACPS_SUPERVISOR_INBOUND_ALLOW_AICS`，用问迹
  现有的 `partners/wenji/backend/scripts/acps/aip_selftest.py --partner-aic <SUPERVISOR_AIC>`
  发一条——这会触发**完整 leader run**（慢、有模型成本），产物是能力矩阵/编排建议。

取证：

```bash
python -m acps_cluster.bidirectional pack \
  --workspace acps_cluster/ws-supervisor \
  --peer-aic 1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN \
  --out-receipt <方向 A 的 probe 回执> \
  --in-receipt   <方向 B 的回执文件> \
  --self-label 研枢-Supervisor --peer-label 问迹 \
  --out-dir artifacts/bidirectional/<日期>/supervisor-wenji
```

> 若方向 B 走 B2（平台侧任务），`pack` 不传 `--in-receipt` 时会自动从
> `ws-supervisor` 任务库挑最近一条与问迹的 `inbound` 任务；用 `--in-task <taskId>`
> 可以锁定某一次演示。

### 5.4 产物与归档

```
artifacts/bidirectional/<日期>/<对端>/
├── bidirectional-evidence.json   # 机器可读：两条回执 + 逐条自检 + verdict
├── bidirectional-evidence.md     # 直接贴进佐证材料/答辩附录
└── SHA256SUMS                    # sha256sum -c 可校验
```

`bidirectional-evidence.md` 里已包含：双方 AIC、两条 taskId、方向、终态、时间、产物
数量/字符数/sha256、状态时间线、自检表（含"对端任务库对账"）、只读复核命令。**平台后台
截图仍需人工截**，证据包不伪造平台数据。

## 6. 常见失败与处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 探针对端返回 `deny / no-credential`，任务 `rejected` | 调用方用标准客户端、没带 `commandParams`，又不在入站白名单 | 走路径 A（带 `skillId`）或把调用方 AIC 加进 `ACPS_SUPERVISOR_INBOUND_ALLOW_AICS` |
| `sender does not match expected` / `identity binding` | 探针 `senderId` 与证书 AIC 不一致（用错了工作区） | `--workspace` 必须指向发起方自己的工作区 |
| `TLS server AIC mismatch` | `--peer-aic` 与实际端点证书不一致（端点填错/对端换了 AIC） | 用 `doctor --discover` 重新解析端点；或 `--peer-endpoint` 指定正确端点 |
| 探针一直 `working` 到超时 | 对端在跑 LLM（路径 B/无静态路由） | 加大 `--timeout`，或改用确定性技能（`kb.schema` / `research.orchestrate`） |
| `awaiting-input` 停住 | 对端要求补充输入 | 加 `--answer "<补充>"` 重跑；或先人工补齐再续跑 |
| `pack` 判 `incomplete`（`both-completed`） | 任务停在 `awaiting-completion`（调用方没关单） | 用 `complete` 关单，或改用探针（会自动 `complete`） |
| `pack` 判 `incomplete`（`aic-symmetry`） | 两条任务不是同一对 agent，或对端回执没翻转视角 | 用 `--in-receipt` 让工具翻转；不要手工拼 AIC |

## 7. 与比赛材料的口径衔接

- 技术报告表 5-1-1 的"问迹 → 编排器"一行：**跑完 5.3 后填真实 taskId**，删掉"未离线归档"；
- 答辩 PPT 的"双向通信"一句话口径：*两个方向各一条平台 AIP 任务，caller/callee 互换，
  taskId 不同，终态均 completed*（不要写"双方可互相调用"这种没有 taskId 的表述）；
- 佐证材料：附 `bidirectional-evidence.md` + 平台后台截图；两者 taskId 必须能对上；
- 视频：录两个方向各一次任务（技能指名路径秒级完成，适合现场演示）。

> 从"两条互调任务"走到"一条业务链"（Partner 入口 → Supervisor 编排 → 回派子任务），
> 前置验证见 [p0-verification-report.md](p0-verification-report.md)：入口技能必须非公开
> （否则改 ACS 换 AIC）、后台作业底座可复用、问迹并发不是单工作者。

## 8. 边界（如实记录）

- `probe` 是**独立客户端**，不经过 supervisor 运行时，所以它发出的出站任务**不会**写进
  `ws-supervisor` 的 `aip_tasks`；取证时用 `--out-receipt` 把回执带进 `pack`（工具会注明
  `source.kind=probe`）。运行时发出的委派（模型编排）仍会正常落库 `direction='outbound'`。
- 快速通道只执行**代码侧登记**的技能（当前只有 `research.orchestrate`），没有扩大能力面；
  参数对不上签名时回落 LLM，不猜。
- `ACPS_SUPERVISOR_INBOUND_ALLOW_AICS` 只解决"能否入场"，不改变"能力面"：入场后仍是
  完整 leader run，模型能调什么由 PEP 与令牌决定。
- 证据包证明的是"两条 AIP 任务的双方身份、方向、终态与产物"；`--peer-workspace` 能把
  对端任务库的同 taskId 记录一起对账，但**平台后台数据**仍要另外截图，二者 taskId 对齐才
  算完整佐证。

## 9. 本地验证记录（2026-11-17）

离线可复跑（不需要平台证书）：

```bash
# 30 项：解析 / 任务库 / 证据包 / 探针状态机 / 快速通道 / CLI
python -m unittest acps_cluster.tests.test_bidirectional -v
```

另做了三类冒烟（脚本在 `_tmp/`，不入仓）：

| 冒烟 | 做法 | 结果 |
|---|---|---|
| 静态路由回归 | 装 wit 2.1.0 + acps_sdk 2.2.0 后跑 `test_static_router` | 6/6 通过（`_extract` 改为共用解析后行为不变） |
| 快速通道接线 | 真实 `wit` 类型 + `<payload>` 信封调 `SkillFastPathProvider` | 命中技能 → 确定性 JSON、底层 provider 0 次调用；未指名 → 回落 |
| 真实 SDK 客户端 | 本地假 AIP Partner（HTTP）跑 `probe` | `commandParams.skillId/args` 原样到达；accepted→working→awaiting-completion→completed；自动 `complete` |
| CLI 取证 | 伪造双向任务库 + 对端工作区跑 `pack` | 判定 PASS、三项检查全绿、`SHA256SUMS` 可校验、含"对端任务库对账" |

> 以上验证的是**代码路径**；平台侧真实任务需要按第 5 节在服务器上跑，taskId 由平台生成。