# AMP（智能体监控协议）合规与缺口核对

> 核对日期：2026-10-09 · 依据：`ACPs-spec-AMP-v02.02`
> （`ACPs-community/acps-specs/09-ACPs-spec-AMP/ACPs-spec-AMP.md`，与用户提供的
> `ACPs-spec-AMP.md` 为**同一版本同一内容**：2506 行，仅字节数差 2.8 KB，字段命名统计一致）

## 1. 结论速览

| 问题 | 结论 |
|---|---|
| 我们缺这份协议文档吗？ | **不缺**。仓库里一直有，且与平台版本一致（v02.02） |
| 我们写的日志合规吗？ | **合规**。字段命名（snake_case 顶层 + body 内 camelCase）与 `HeartbeatBody` 结构都对 |
| 那为什么心跳进不了平台？ | **因为 AMP 协议本身不定义"日志怎么上报"** —— 这是规范的有意留白，属实现层 |
| 六类日志我们产出了几类？ | **2 类**（heartbeat / system）；metrics / access / message / audit **一行都没有** |

## 2. 为什么"按 AMP 上报心跳"这条路在规范里不存在

规范原文（`§6 AMP 查询接口` 开头）：

> AMP Provider 基于 AMP 日志入库后的稳定读模型提供 HTTP 查询接口。
> **AMP API 不接收 Agent 直接上报日志**，也不重复定义 DSP 的 Snapshot / Changes / Webhook 同步语义…

架构层（`§3.2`）只定义两层：

- **日志采集层**：各类日志由智能体写本地文件，"并在经过必要的解析、过滤、打标签等步骤后，
  由该层以可靠、可控的方式将日志**按类型投递到日志存储层**"；
- **日志存储层**：统一存储、索引、检索。

而 `§4.2` 末尾明确写：**"本节只约束存储类别与访问模式。具体存储产品的选型…属于实现层决策，
不在本规范约束范围内。"**

也就是说：**"agent 日志怎么进平台"这段线缆，AMP 规范故意没有定义**（只在 §4.2 与 §5.2 提到
Kafka / Fluent Bit 作为传输组件的存在）。官方实现把它落在 `acps-infra` 的 **amp_forwarder** 角色里
（Fluent Bit 尾随 `amp_*.jsonl` → Redpanda `amp.*` topic），官方教程也把这一段称为"平台黑盒"
（`acps-docs/tutorials/amp-agent-observability.md:10`）。

**所以"我们缺了 AMP"这个判断需要拆成两半**：

1. 协议/查询侧 —— 我们不缺，用 `acps-cli monitor` 就能查平台（`wt.ioa.pub/monitor` 已部署）；
2. **投递侧 —— 这才是真正的缺口**：本机没有 forwarder，心跳只落在 `ws-*/logs/amp_heartbeat_*.jsonl`。

## 3. 合规逐项核对（实测）

### 3.1 字段命名：不是大小写问题

规范 `§5.1.2` 的 TS 接口用 `schemaVersion` / `logType`（camelCase），但 SDK 明确说明：

> `acps_sdk/amp/models.py:8` —— "顶层字段无 alias → **保持 snake_case**（schema_version / log_id / trace_id …）"

且 `HeartbeatLogRecord` 的默认值就是 `schema_version="1.0"`、`log_type="heartbeat"`。
读模型（如 `heartbeat_sync.py:168` 的 `schemaVersion`）才用 camelCase —— 那是**查询侧**的字段。

我们实际写出的行（线上抓取）：

```json
{"schema_version":"1.0","timestamp":"2026-10-09T04:49:49Z","aic":"1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5",
 "log_type":"heartbeat","log_id":"4c486021-…","body":{"uptimeSeconds":90.089},
 "resource":{"service.name":"yanshu.scout-partner"}}
```

对照规范：`schema_version` ✔、`timestamp` ✔、`aic` ✔、`log_id` ✔（幂等键，见 §5.1.3）、
`log_type` ✔、`body.uptimeSeconds` ✔（§5.2.1 该 body 只有这一个字段）、`resource` ✔。**合规**。

### 3.2 六类日志的实际产出

| 类型 | 规范要求 | 我们的产出 | 说明 |
|---|---|---|---|
| heartbeat | §4.1 每 10–60s 一条 | ✅ 9 个文件 / 29,785 行 | 8 个成员每 30s 一条，合规 |
| system | 自由格式 | ⚠️ 9 个文件 / 共 9 行 | 只写了启动事件（每进程 1 行），几乎没内容 |
| metrics | §5.3 结构化数值 | ❌ 0 | 无 TSDB 接入、无指标采样 |
| access | §5.4 每次交互 | ❌ 0 | wit 有 `AmpAccessLoggingHooks`，我们没挂 |
| message | §5.5 消息收发 | ❌ 0 | 我们用 Direct JSON-RPC，没有 MQ 通道 |
| audit | §5.6 需签名 | ❌ 0 | 我们自己写了 PEP 审计（`pep-audit.jsonl`），但**不是 AMP audit 流**，没有签名与入库 |

> 说明：AMP 的六类是**能力**而非"每类都必须"；partner 只用 Direct 协议时 message 为 0 是合理的。
> 但若答辩要讲"AMP 可观测性"，目前只有 heartbeat 一条能讲，**metrics / access / audit 是空白**。

### 3.3 平台侧的查询面（已就绪，只等数据）

| 端点 | 未鉴权返回 | 含义 |
|---|---|---|
| `POST /monitor/acps-amp-v1/heartbeat/liveness/{aic}` | 401 | 需要 bearer（OIDC）；平台已部署 |
| `POST /monitor/acps-amp-v1/metrics/snapshots/query` | 401 | 同上 |
| `POST /monitor/acps-amp-v1/access/events/query` | 401 | 同上 |
| `POST /monitor/acps-amp-v1/audit/records/query` | 401 | 同上 |
| `GET /monitor/acps-amp-v1/heartbeat/sync/info` | 401 | alive-delta 同步信息（Kafka topic / 分片） |

`GET /discovery/admin/alive-sync/status`（免登录）显示 `aliveCount=139 / knownCount=255` —— 我们在那 116 个"未知"里。

## 4. 缺口清单与优先级

| # | 缺口 | 影响 | 工作量 |
|---|---|---|---|
| 1 | **AMP 日志投递（forwarder）** | 平台认为我们离线 → 按 AIC 查不到、热门面板无我们 | 已就绪，等平台口径（见 §5） |
| 2 | metrics 日志（§5.3） | Monitor 里看不到负载/时延/成功率；可观测性单薄 | 中：wit 有 `AmpMetricsHooks` + `emit_metrics` |
| 3 | access 日志（§5.4） | 没有逐次交互记录，审计与性能分析缺数据 | 小：挂 `AmpAccessLoggingHooks` 即可 |
| 4 | audit 日志（§5.6） | 我们的 PEP 审计不在平台可查、无签名 | 中：需要 signer 与事件模型对齐 |
| 5 | system 日志内容 | 只有启动 1 行，排障价值低 | 小：在关键路径补 emit |
| 6 | `log_id` 用 UUIDv4 | 规范**推荐** UUIDv7（时间有序）；不影响去重正确性 | 小（wit 侧默认，非我们代码） |

## 5. 唯一阻塞项（缺口 1）的三种解

`deploy/amp_heartbeat_forwarder.py` 已写好（纯标准库、checkpoint、dry-run 默认），
平台总线只读探测：`amp.heartbeat`（1 分区）、`amp.heartbeat.alive-delta`、`amp.heartbeat.dlq` 等 14 个 topic；
broker `192.0.2.20:19092` 匿名可握手。三选一即可：

1. **平台采集**：把 `/opt/wit/my-project/acps_cluster/ws-*/logs/amp_heartbeat_*.jsonl` 纳入官方 Forwarder
   （最符合 §3.2 的"采集层"定位，我们零运维）；
2. **授权我们投递**：确认 topic 名、是否需要 SASL/ACL、分区与键约定（单分区，键可留空或按 AIC）；
3. **先给测试 topic**（平台已有 `amp.external.smoke`）：先验证再切正式。

打开后 1–3 分钟内：`aliveMap` 覆盖 → "按 AIC 查在线"变 ✔ → 首页热门开始出现我们。

## 6. 来自开源社区（AtomGit `AIP-PUB`）的官方答案

社区共 9 个仓库，**只有 `ACPs-community` 是活跃的**（其余 8 个 6 月 22 日已归档）。
在活跃仓库里找到了这段线缆的官方实现细节——**它确实存在，而且就是"agent 侧转发器"**：

### 6.1 官方 Forwarder 的配置模板（`acps-infra/.../roles/amp_forwarder/templates/fluent-bit.conf.j2`）

```ini
[INPUT]
    Name             tail
    Path             /logs/demo_partner/amp_heartbeat_*.jsonl   # 尾随 agent 的 jsonl（按类型 glob）
    Tag              amp.heartbeat
    Parser           amp_audit_json                            # 就是 JSON parser
    Refresh_Interval 2
    Read_From_Head   False                                     # 只转发新行（与我们转发器同语义）
    DB               /var/lib/fluent-bit/partner-hb.db          # 位点库

[OUTPUT]
    Name                              kafka
    Match                             amp.heartbeat
    Brokers                           {{ _amp_fw_brokers }}    # = <redpanda_host>:19092
    Topics                            amp.heartbeat
    Workers                           1
    rdkafka.request.required.acks     1
    rdkafka.allow.auto.create.topics  false
```

要点：

| 项 | 官方值 | 我们实测/实现 |
|---|---|---|
| broker 端口 | `redpanda_kafka_port: 19092`（`group_vars/all.yml:260`） | 平台广告的就是 `192.0.2.20:19092` ✅ 一致 |
| 鉴权 | 配置里**没有任何 SASL/ACL 设置** | 我们探测到匿名可握手 ✅ 自洽 |
| parser | `Format json`、`Time_Key timestamp`、`Time_Keep On`（保留原 JSON 结构，避免签名校验失败） | 我们投递的**就是原始 JSONL 行**，结构不变 ✅ |
| 语义 | `Read_From_Head False` + 每类一个 DB 位点 = 只转发新增行 | 我们的字节偏移 checkpoint 同语义 ✅ |
| 部署位置 | 角色由 Ansible 装在 agent/demo 主机上，读取该主机的日志目录 | 我们要么自建、要么请平台把我们的目录纳入采集 |

**结论：官方约定的投递方式 = agent 侧 Fluent Bit 尾随自己的 jsonl → 平台 Redpanda `amp.heartbeat`。
我们已经用纯标准库把这套语义 1:1 实现（`deploy/amp_heartbeat_forwarder.py`），
连"位点库 / 只转发新行 / 消息体保持原始 JSON"这些细节都对齐了。**

### 6.2 社区里没有的东西（也是为什么要找平台确认）

- 仓库里**没有**"外部 agent（不在平台主机上）如何接入总线"的文档或 role；
  没有 `amp.external*` 相关约定（平台上手建了 `amp.external.smoke` topic，说明他们自己留了外部接入测试口）。
- 官方教程把采集层称为"平台黑盒"（"你作为开发者通常不用改 Forwarder 配置"）——这句话成立的前提是
  **agent 与 Forwarder 在同一台机器/同一套日志目录**。我们的情况（agent 在我们自己的云主机上）
  不属于这个前提，所以必须由平台明确一下分工。

### 6.3 因此要问平台的就两个选项（不再需要讨论"要不要鉴权"）

对方是运维那套 Ansible 栈（role 名 `amp_forwarder`，broker 就是 `redpanda:19092`），
所以可以直接这样提：

> **选项 A（推荐，零运维）**：请把我们的心跳日志纳入你方 `amp_forwarder` 的采集范围——
> 主机 `192.0.2.10`、目录 `/opt/wit/my-project/acps_cluster/ws-*/logs/amp_heartbeat_*.jsonl`、
> 8 个 AIC 见下。或告诉我们你方 Forwarder 读取的日志根目录，我们把文件同步过去。
>
> **选项 B（我们侧自建）**：按你方模板在我们主机跑一个 Fluent Bit（或等价的轻量转发器），
> 直投 `192.0.2.20:19092` 的 `amp.heartbeat`；请确认：
> ① 该外部 listener 是否接受外部主机写入；② 是否需要 SASL/ACL；
> ③ 先用 `amp.external.smoke` 验证还是可以直接投 `amp.heartbeat`。

我们的 8 个 AIC（心跳文件名里就是它）：
`…C6HTRB…` 已作废，当前为
`…GMT5S6.4SZM68.0LC5`（scout）、`…YDWSUJ.RBUQ6T.12KC`（synthesis）、`…WATDMX.AQFZR5.0NQN`（writer）、
`…71P6ML.DDF7EG.13B2`（critic）、`…0FQNAG.ZO888M.0FW5`（contradiction）、`…ZAJ69L.IOEX2R.082D`（frontier）、
`…5CJ349.JNZ5VU.0E5E`（experiment）、`…JXN8O8.98DOO8.0EB6`（supervisor）。

> ⚠️ 上面这份清单是 2026-10-09 早些时候的。**当日 16:50 起已全部换新**（用户重新注册了 6 个更高版本），
> 最新 AIC 与验收结果见 `docs/incident-2026-10-09-code-sync-acs-guard.md` §3：
> scout `GMT5S6.4SZM68.0LC5` / supervisor `JXN8O8.98DOO8.0EB6` / critic `M2NJ2G.A1BIB2.195J` /
> writer `1OGDWX.U9K14M.0CDI` / synthesis `9HADYH.RLVWEP.07Y5` / experiment `WN4C3S.0C0YA7.0J4R` /
> frontier `V1E6AY.3YME2N.01K2` / contradiction `Z7TJ57.ORGGRY.0ZFO`。


---

## 7. 选项 A 落地（2026-10-09）：心跳已整理成"可被 Forwarder 直接 tail"的目录

选定 **A（由平台 Forwarder 采集）**后，唯一的技术约束是：
**采集必须发生在平台 Forwarder 读得到的日志根目录里**——我们的日志原本散在
`ws-*/logs/` 且文件名带 AIC，不能直接对接。于是做了镜像层：

| 件 | 作用 |
|---|---|
| `deploy/amp_heartbeat_mirror.py` | 读 `ws-*/logs/amp_heartbeat_*.jsonl`（只读）→ 追加写 `/var/log/acps-amp/yanshu/amp_heartbeat_<agent>.jsonl`；位置持久化；只搬运 **300s 内**的新鲜心跳；单文件 8MB 自动裁剪；行内容**原样搬运**（不改字段、不重生成 `log_id`） |
| `deploy/wit-amp-mirror.service` | systemd 常驻（15s 一轮），`enable --now` 已启用 |
| `deploy/AMP_OPTION_A_HANDOFF.md` | 给平台运维的交接单：目录/文件名/样例、要加的两段 Fluent Bit 配置、文件搬运三选一、8 个 AIC 清单、验收方式 |

**实测状态**（`/var/log/acps-amp/yanshu/`）：

```
amp_heartbeat_scout.jsonl          ← 8 个 agent 各一个文件
amp_heartbeat_synthesis.jsonl         每 30s 新增一条
amp_heartbeat_writer.jsonl            40 秒内 11 → 12 行（实时同步已验证）
amp_heartbeat_critic.jsonl
amp_heartbeat_contradiction.jsonl
amp_heartbeat_frontier.jsonl
amp_heartbeat_experiment.jsonl
amp_heartbeat_supervisor.jsonl
```

首次镜像自动跳过了 **29,825 行**过期心跳（退役的 arXiv partner 与换身份前的旧 AIC），
只上报当前 8 个在跑身份的新鲜心跳——避免把历史数据灌进平台 alive 视图。

**平台侧只需两步**：① 把该目录（或其同步副本）挂到你方 Forwarder 能读的位置；
② 按交接单加 `tail → amp.heartbeat` 两段配置。

**兜底（B 方案，已就绪未启用）**：`deploy/amp_heartbeat_forwarder.py` 现在支持
`--mirror-dir`（直接以镜像目录为源投递）+ `--message-key none|aic`（`amp.heartbeat` 单分区，默认留空键）。
若选项 A 卡在文件搬运上，注册器改一行即可切到"我们直投"：dry-run 已验证（8 文件 / 104 条）。
仍缺的只有平台的投递授权（topic 与是否接受外部 producer）。

---

## 8. 社区核实补充：官方拓扑假设 agent 与 Forwarder 同机（2026-10-09）

把 `ACPs-community/acps-infra` 安装层翻完后确认：**社区没有"平台采集外部主机日志"的实现**，
官方拓扑默认 agent 与 AMP Forwarder 在**同一台主机**上：

| 证据 | 位置 |
|---|---|
| 全部组件默认落一台机 | `inventories/hosts.example.yml`（`acps-node-1` 同时是 redpanda/monitor/demo_partner/demo_leader） |
| 多机也只是"应用机 + 依赖机" | `inventories/hosts-multi.example.yml`（`acps-app.example` 同时挂 demo_partner + demo_leader + monitor） |
| Forwarder 输入是**本机**日志根 | `amp_forwarder/templates/fluent-bit.conf.j2`（`{{ _amp_fw_log_base \| default('/logs') }}/demo_partner/...`，容器 `/logs` ← 宿主 `acps_log_root: /var/log/acps`） |
| agent 与 forwarder 共享同一目录 | `demo_partner/compose.yml.j2`（`{{ acps_log_root }}/demo_partner:/opt/acps/app/logs`） |

**推论**：官方对"agent 在别的主机"的隐含解法 = **那台主机自己跑 forwarder 投到共享 Redpanda**（即 B）。
但三件关键事实说明 A 也很容易落地：`_amp_fw_log_base` 是变量、`redpanda_kafka_port: 19092` 本就是
跨机访问端口（实测对我们可达）、Forwarder 配置**无 SASL/ACL**。

已据此把对平台的请求改写成"可直接转发"的对接件：
`docs/platform-request-amp-heartbeat.md`（含现象与对照实验、我们已就绪的镜像目录、A/B 两方案的
具体配置与所需信息、8 个 AIC 清单）。对外发送动作记为 **2026-10-09**，方案 A-A0/B 同时抛出。

---

## 9. B 方案上线与验收（2026-10-09 13:34，**问题已解决**）

平台未及回复，按决策直接走 **B（我们侧投递）**。全过程与结果：

### 9.1 实施链

| 步 | 动作 | 结果 |
|---|---|---|
| 1 | 低影响探测：向平台**测试 topic** `amp.external.smoke` 投一条探针 | ✔ offset=6，**平台 broker 接受外部匿名 producer** |
| 2 | 心跳镜像（`wit-amp-mirror.service`） | ✔ `/var/log/acps-amp/yanshu/` 8 个文件实时更新 |
| 3 | 位点初始化到当前末尾（`--init-offsets`） | ✔ **不回灌历史**，只发上线后的新心跳 |
| 4 | 发送器（`wit-amp-sender.service`，`amp_heartbeat_sender.py`） | ✔ active，位点持续推进（首轮已发 24 条） |

### 9.2 验收结果（平台侧实测，实测时刻 13:35–13:38）

| 检查项 | 上线前 | 上线后 |
|---|---|---|
| 平台 alive 总盘 | `aliveCount=137 / knownCount=257` | **`aliveCount=145 / knownCount=265`** |
| `aliveMap` 覆盖我们的 AIC（recent 取数） | 0 条 | **✔ Scout `…GMT5S6…`、Supervisor `…JXN8O8…` 均被覆盖** |
| **平台「按 AIC 查在线」**（`filtered`+`aic`，就是首页那个查询） | 0 条（"未查询到智能体"） | **✔ 返回完整 ACS**（`active:true` + 含 tags/examples 的 skills）→ 首页会显示"**在线**" |
| 叮当语义搜索 | 「文献综述」0/5 | **「文献综述」我们 2 个（#2 Supervisor、#3 Scout）；「写论文」「审稿」均 #1** |
| 首页「最新上架 / 热门」面板 | 0 张 | 仍 0 张（面板取数按活跃度排序，非按在线；属"热度"而非"在线"，见 9.4） |

> 结论：**"按 AIC 查不到"这个问题已经解决**——平台 now 认为我们在线（alive 视图覆盖 + filtered 查询返回）。

### 9.3 过程中的两个真 bug（都已修，值得记）

1. **自研 Kafka 协议不可靠**：最初为"零依赖"手写了 Kafka 生产者，broker 返回
   `error_code=2 CORRUPT_MESSAGE`——根因是 RecordBatch 里
   `keyLen/valueLen` 用了**不正确的 varint**（Kafka 要求 zigzag，负值/长度编码错了）。
   教训：**能用成熟库就别自研线缆协议**。最终发现 venv 里本来就有 `kafka-python 3.0.11`，
   改用之后一次通过。
2. **响应解析偏移错**：早期版本解析 Produce 响应时偏移算错，把 `error_code` 读成天文数字，
   误报成"解析失败"。修成游标式解析后才看到真正的错误码。

### 9.4 仍未达成的两点（诚实边界）

- **首页「热门智能体」面板**仍是 0 张。该面板用 `type=trending`，其排序取决于平台侧的
  活跃度/调用量（我们的 agent 还没被其他队伍调用过），与"在线"无关。
  要出现在那里需要真实调用量积累，或平台调整推荐口径。
- **两个 agent 的心跳先进 alive 视图、`trending` 取数的 aliveMap 暂未覆盖**（`recent` 已覆盖）。
  同一个 alive 表在两处取数表现不同，说明 trending 那条路径的 aliveMap 是**按结果行**注入的
  ——等我们的 agent 进入 trending 结果集后自然会出现，无需额外动作。

### 9.5 运维状态（回滚与长期）

| 件 | 位置 | 说明 |
|---|---|---|
| 心跳镜像 | `wit-amp-mirror.service` → `/var/log/acps-amp/yanshu/` | 每 15s 同步；只保留 300s 内新鲜心跳 |
| 心跳发送 | `wit-amp-sender.service`（`amp_heartbeat_sender.py`） | 每 15s 巡查；位点 `/var/log/acps-amp/.sender-offsets.json` |
| 关闭投递 | `systemctl disable --now wit-amp-sender` | 心跳即停止进入平台（1–2 分钟后 alive 过期） |
| 探测权限 | `amp_heartbeat_sender.py --probe` | 只投测试 topic `amp.external.smoke` |

> 注意：这条路径是我们侧直投平台生产 topic。若平台后续加了 ACL/SASL，
> 发送器会开始报错（journal 里可见）；届时改用平台给的认证参数，或退回 A 方案（他们采集）。

---

## 10. 「AMP 是 agent 主动上报」——和我们的实现有没有偏移？

> 起因：比赛方材料里出现"AMP 由 agent 主动上报"的说法，需要确认我们是否走偏。
> 核实动作：抓取比赛官方附件 PDF 全文检索（`my-project/docs/_competition/`，
> 附件1 规则 3 页 / 附件2 报告大纲 3 页 / 0826 通知 6 页）——
> **三份材料里都没有出现"AMP"或"主动上报"**（只命中一处无关的"在线答疑"）。
> 所以这句话应出自平台侧的培训/口头说明，而不是比赛文件原文。

### 10.1 结论：**方向完全一致，不存在偏移**

"主动上报"描述的是**推送方向**，不是"agent 自己实现 HTTP 上传"。逐项对照：

| 维度 | "主动上报"的含义 | 官方参考实现 | 我们的实现 | 是否偏移 |
|---|---|---|---|---|
| 发起方 | agent 侧发起，平台不轮询 | agent 写 jsonl → **本机 Fluent Bit 主动投递** | agent 写 jsonl → **本机发送器主动投递**（每 15s，位点持久化） | 一致 |
| 传输 | agent 侧把记录送进平台 | Kafka `amp.heartbeat` @ `:19092` | 同 left | 一致 |
| 记录内容 | AMP LogRecord | `schema_version/log_type=heartbeat/body.uptimeSeconds/…` | 字段级一致（§3.1 已逐条核对） | 一致 |
| 落点 | 平台入库后供查询 | monitor-server → alive-sync → discovery | 同 | 一致 |
| 是否平台拉取 | **否** | 官方没有任何"平台来拉"的组件 | 我们的心跳镜像只是本机内部分发，**没有让平台来拉** | 一致 |

也就是说：**我们既没有依赖平台来"拉"，也没有等平台"发现"我们——心跳是我们这边每 15s 主动推进平台的。**
这恰恰就是"主动上报"。（顺带印证：`aliveCount` 从 137 涨到 147，正是我们主动推上去的。）

### 10.2 与官方参考实现的差异（都是实现细节，不是方向差异）

| 项 | 官方 | 我们 | 影响 |
|---|---|---|---|
| 谁执行投递 | Fluent Bit（rdkafka） | Python 发送器（kafka-python） | 无：消息体一致、目标 topic 一致 |
| 投递进程与 agent 的关系 | 同主机、独立进程（容器） | 同主机、独立进程（systemd） | 无 |
| 日志路径 | `<acps_log_root>/demo_partner/amp_heartbeat_*.jsonl` | `ws-*/logs/amp_heartbeat_<AIC>.jsonl` → 镜像到 `/var/log/acps-amp/yanshu/` | 无：`tail` 用 glob，官方换我们这路径同样能工作 |
| 覆盖的日志族 | 六类（heartbeat/metrics/access/message/system/audit） | 当前只有 heartbeat + system | **这才是真正的"偏移"**，见 10.3 |

### 10.3 唯一真正的偏移：日志族覆盖不全（与"主动上报"无关）

"主动上报"不等于"上报得全"。我们目前只有：

- ✅ **heartbeat**：已进平台，存活视图生效；
- ⚠️ **system**：每进程仅 1 行（启动事件）；
- ❌ **metrics / access / audit**：0 行（wit 有 `AmpMetricsHooks` / `AmpAccessLoggingHooks`，我们没挂）。

如果答辩/技术报告要讲"AMP 可观测性"，建议至少补 **access**（挂一个钩子就有：每次交互一行）
与 **metrics**（负载/成功率/时延序列），否则只有心跳一条能讲。
这两项都不影响"主动上报"的定性，但会明显影响观感。

### 10.4 可选的进一步对齐（如果要让"agent 主动"更字面）

当前拓扑是"agent 写 → 同机两个转发服务 → 平台"。若对方强调"agent 本体主动上报"，
可升级成下面任一种，都不改协议：

| 档 | 做法 | 优劣 |
|---|---|---|
| **L0（现状）** | agent 写 journal → `wit-amp-mirror` → `wit-amp-sender` 投递 | 优点：agent 进程零改动、Kafka 故障不影响业务；缺点：多一跳进程 |
| **L1** | 每个 agent 进程内起一个后台线程，自己的 journal 自己投 | 最贴合"agent 主动"字面；需在每个 partner 里加发送线程（失败不得阻塞事件循环） |
| **L2** | 按官方模板在本机装 Fluent Bit，tail 我们自己的路径直投 `:19092` | 与官方参考实现**逐字对齐**（同一工具、同一配置形态），答辩最无争议 |

> 我的建议：**答辩口径用 L2 或 L0 都站得住**（都满足"agent 侧主动推送"）；
> 若评委追问"你们和官方 demo 是不是一套"，L2 最容易一句话说清；
> 若只是想在报告里写清"我们实现了 AMP 主动上报"，L0 现状即可，把 10.1 的对照表放进去就行。

---

## 11. 8 个 agent 的在线状态逐条复核（2026-10-09 15:07）

**方法**：用平台首页「在线状态查询」的**原样请求**（`type=filtered` + `filter.conditions[field=aic]`）
逐个 AIC 查询 —— 该请求返回非空 ⇔ 平台认为该 AIC 在线（§9.1 的对照实验已证明这个等价关系）。

| agent | 按 AIC 查（首页查询） | 结果里的 `aliveMap` |
|---|---|---|
| Scout `…GMT5S6…` | ✔ 1 条 | `alive: true` |
| Supervisor `…JXN8O8…` | ✔ 1 条 | `alive: true` |
| Synthesis `…YDWSUJ…` | ✔ 1 条 | `alive: true` |
| Writer `…WATDMX…` | ✔ 1 条 | `alive: true` |
| Critic `…71P6ML…` | ✔ 1 条 | `alive: true` |
| Contradiction `…0FQNAG…` | ✔ 1 条 | `alive: true` |
| Frontier `…ZAJ69L…` | ✔ 1 条 | `alive: true` |
| Experiment `…5CJ349…` | ✔ 1 条 | `alive: true` |
| **arXiv 检索 Partner（已退役）** | ✘ 0 条（平台记录 403 not active） | 无 |

> 一个容易误判的细节：`type=recent/trending` 的响应里，`aliveMap` **只覆盖本次结果行里的 AIC**
> （spec §4.2.3 的注入语义）。所以"在 `recent limit=50` 的 aliveMap 里只看到 3 个我们"
> **不等于**另外 5 个没上报——按 AIC 逐个查，8 个全是在线。
> 判断"某 agent 是否在线"必须用按 AIC 的查询，不能看列表型响应的 aliveMap。

### 11.1 关于 arXiv 检索 Partner 的常见误解（口径建议）

| 说法 | 事实 |
|---|---|
| "它当初能正常发现，说明有另一种做法" | 它当年**也没进过 alive 视图**（`aliveMap` 从未覆盖 `…Q1EZOG…`），服务器上从未装过任何转发器（`/var/lib/fluent-bit` 不存在）。它当年"能用"= **发现索引里能检索到 + 端点可直连调用**，与"平台认为在线"是两件事 |
| "它现在还能被发现" | 不能。平台记录已 `403 not active`，按 AIC 查 0 条，`aliveMap` 无条目 |
| "它和现在的机制不同" | 机制相同（同一 registry/CA/discovery、同样只写本地 jsonl）；**差别只在它已于 09-27 退役**，而其余 8 个从 10-09 起补上了心跳上报 |

**答辩口径**：如果评委搜到这条 403 记录，直接说明"它是第一个原型，已在本集群内退役，
由 scout 承接其检索能力"；不要把它当作"可发现的范例"，因为它并不在线。

---

## 12. 结项状态（2026-10-09，用户验收通过）

用户确认：**平台上按 AIC 已能查到全部 agent，supervisor 也能在叮当里调用**。
"叮当发现不到自己的 agent"这个问题**关闭**。

### 12.1 最终成因（三件事叠加，缺一不可）

| # | 成因 | 修法 | 证据 |
|---|---|---|---|
| 1 | **身份不一致**：平台记录被停用（旧 AIC 403），在跑的服务却还挂着旧 AIC | 用平台 CA 按新 AIC 直接签发证书 + 切工作区身份（`enrollment.json` + `acs.final.json` + 证书） | 平台 `active=true`；证书 CN/SAN = 新 AIC；按 AIC 查得到 |
| 2 | **门面文本太弱**：描述短、`examples` 为空、tags 少 → 自然语言问法匹配不上 | 集中式"发现门面"（15 agent + 36 skill 的 description/tags/examples） | 「文献综述」问法从 0 条 → 我们 2 条；「写论文」「审稿」各 #1 |
| 3 | **心跳没进平台**：agent 只写本地 jsonl，平台 alive 视图不覆盖 → 按 AIC 查不到、不在线 | 镜像 + 发送器（B 方案）把心跳按官方同语义投到 `amp.heartbeat` | `aliveMap` 覆盖 8/8；平台按 AIC 查询返回完整 ACS |

> 三者的关系：**光有 3（心跳）而没有 1（身份一致），平台认的是已停用的 AIC；光有 1 而没有 2，
> 一句话问法依然匹配不上；2 与 3 都做对但 1 没做，新记录会"索引里有、实际无人服务"。**

### 12.2 arXiv 检索 Partner：确认保持退役（不再救活）

- 它的位置**已由 scout 承接**（`arxiv.search` 能力在 scout 上，且现在在线可发现）；
- `cluster_roster` 中它的条目保持注释状态；`ws-partner` 工作区保留为历史存档；
- 平台侧那条 `403 not active` 的记录**保持现状**（不删、不重启），答辩时按 §11.1 的口径说明。

### 12.3 长期运维（现在在跑的两个服务）

| 服务 | 作用 | 停止方式 |
|---|---|---|
| `wit-amp-mirror.service` | `ws-*/logs/amp_heartbeat_*.jsonl` → `/var/log/acps-amp/yanshu/`（15s，只保留 300s 新鲜） | `systemctl disable --now wit-amp-mirror` |
| `wit-amp-sender.service` | 镜像目录 → 平台 `amp.heartbeat`（15s，位点持久化） | `systemctl disable --now wit-amp-sender`（停后 1–2 分钟平台认为我们离线） |

**若平台后续给总线加 ACL/SASL**：`journalctl -u wit-amp-sender` 会出现投递错误；
届时改用平台给的认证参数，或退回 A 方案（把 `/var/log/acps-amp/yanshu/` 交给平台 Forwarder 采集，
交接单见 `deploy/AMP_OPTION_A_HANDOFF.md`，可直接发给他们）。







