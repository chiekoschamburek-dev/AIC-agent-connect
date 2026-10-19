# 服务器上线执行记录（2026-10-19）

> 执行人：AI agent ／ 目标机：`192.0.2.10`（root）／ 时间：2026-10-19 09:41–11:10 CST
>
> 本文记录**实际做了什么、验证到什么程度、以及唯一未闭环的点**。不含推测。

## 1. 结论速览

| 项 | 状态 | 证据 |
|---|---|---|
| 模型切到北邮网关 deepseek | ✅ 完成 | 10/10 agent 启动日志均打印 `[model] 路由: 北邮网关/deepseek-v4-flash` |
| MiniMax 保底 | ✅ 完成并实测 | 主用指向必挂地址 → 自动切保底 → 仍返回正文 |
| 计量钩子 `amp_hooks` | ✅ 完成 | 0 → **14 个模块**（`supervisor.py` + 13 个 `partner_*.py`） |
| 重启删日志隐患 | ✅ 修复 | `ExecStartPre` 已收窄为 `amp_heartbeat_*.jsonl`**并重装到 `/etc/systemd/system/`** |
| access 记录生成 | ✅ 完成 | writer 266 条 / scout 46 / critic 32 / frontier 16 |
| access 转发 Kafka | ✅ 完成 | 累计投递 317 条，位点持续推进，`跳过 0` |
| 消息真的进了平台 Kafka | ✅ 验证 | 在 `amp.access` 中扫到我们 **193 条**记录（caller/callee/时间戳均正确） |
| 平台消费了这些消息 | ✅ 验证 | 消费组 `monitor-server.access.writer.v1` 各分区**积压 0** |
| 平台是否拒收 | ✅ 排除 | `amp.access.dlq` 抽样 50 条，**无我们的记录** |
| 任务派发闭环 | ✅ 完成 | 6 次派发：5 次 `ok`（产物 958–10537 字节），1 次早期 TLS 失败（已修） |
| **平台排行榜显示** | ✅ **已打通** | 派给 supervisor 后，榜单出现 supervisor **97**、scout **97**（caller/callee 各 1 条） |

**一句话**：四段链路（产生 → 记录 → 投递 → 平台消费 → 榜单）全部打通。
**关键前提**：必须让 **supervisor 真实出站委派**，脚本直连伙伴的调用平台不计入。

## 1.1 最后一块拼图：caller 侧记录

修完计量后榜单仍是 0，进一步定位到**归属口径**：

> 平台按 **caller 侧 span**（`record.aic == body.caller.aic`）计数。

证据链：

| 观察 | 说明 |
|---|---|
| 我们的 266 条记录 `aic` = **callee**（被调方） | 因为伙伴自己记的是 inbound span |
| 榜单样例记录 `aic` = **caller**（发起方） | 例如 `LDR001`（leader）→ `BNN2UD`（partner） |
| 平台消费组积压 0、DLQ 无我们 | 说明被消费了，但**未被归属** |

**验证**：给 supervisor 派一个 orchestrate 任务，它就会真实出站委派给 scout/writer。
结果 `ws-supervisor/logs/amp_access_*.jsonl` 立刻出现 **60 条 caller 侧记录**
（`aic == caller`，其中 21 条 `supervisor → scout`），榜单随即出现：

```
caller: 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6   count=97  access=97
callee: 1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5   count=97  access=97
```

**因此派发器的默认目标已改为 supervisor**（`--targets supervisor`），
supervisor 一次运行会扇出多个子委派，一次派发等于多条有效调用。

## 1.2 job worker（作业化交路）的 access 归属

`submit_job` 拉起的后台作业也是一个 supervisor 身份（同 AIC）的进程。
它能不能计入榜单、以及会不会和常驻 supervisor 打架，取决于 **journal 隔离**。

**问题**：两者共用 `ws-supervisor/logs/amp_access_<AIC>.jsonl`。该 journal 有
历史链校验（`_validate_history`），两个进程并发 append 必炸：

```
AmpPersistenceError: AMP access append refused a journal changed during validation
```

2026-10-19 **11:17 / 11:19 两个 job 就是崩在这里**（`state=failed`）。当时的
应急做法是给 job 设 `ACPS_SUPERVISOR_NO_AMP_ACCESS=1`——job 不写 access，
代价是 **job 内部的委派不计入榜单**。

**修复**（`deploy/patch_job_workspace.py`）：给 job worker 独立的
`ws-supervisor-job/` workspace，让它写自己的 journal。四处置信点：

| # | 问题 | 修法 |
|---|---|---|
| 1 | 整目录 symlink `acps/` 会把 208MB 的 `aip-tasks.sqlite3` 也共享，两进程并发写同一任务库 | 只复制小体积身份材料（证书/enrollment/acs/trust-bundle），**任务库不复制**，job 用自己的空库；`discovered/` 用 symlink 共享 |
| 2 | `flock(LOCK_EX)` 无超时，一个卡死的 job 会让后续所有 job 永久等锁（实测已有 job 卡 12 分钟） | 改成 `LOCK_EX\|LOCK_NB` + 超时（`ACPS_JOB_LOCK_TIMEOUT`） |
| 3 | `DELIVER_DIR` 是模块常量，`WORKSPACE` 被换掉后仍指向旧目录 | 改成 `_deliver_dir()`，运行时取值 |
| 4 | `check_job` 的 `jobs_root = WORKSPACE / "jobs"` 会变成 `ws-supervisor-job/jobs`，找不到真实任务 | 改成 `_jobs_root()`，固定到 `ws-supervisor/jobs` |
| 5 | 锁超时设 15s 时，并发的第二个 job **直接失败**（并发=2 实际退化成 1） | `ACPS_JOB_LOCK_TIMEOUT=900`：让它**等**前一个跑完再执行 |
| 6 | 撞锁退出时 `status.json` **永远停在 `running`**，一直占着并发额度 | 捕获 `SystemExit` 后先把状态置 `failed`（`run_status=lock-timeout`）再退出 |

> 第 5、6 条是验证过程中才暴露的：第一个固定 job 跑着时，第二个 job
> 15 秒后放弃，但状态卡在 `running`——两个问题都修了。

**实测结果**（2026-10-19 11:57 起的真实 job，`state=done / exit_code=0 / 8 步`）：

```
[job] 独立 workspace 就绪: .../ws-supervisor-job（复制 10 项，任务库独立）
PEP trustBundle → .../ws-supervisor-job/acps/trust-bundle.pem   ← WORKSPACE 补丁生效
ws-supervisor-job/acps/aip-tasks.sqlite3  = 4KB（独立空库，非 208MB 共享库）
ws-supervisor-job/acps/discovered          = symlink → 共享
job journal: 54 条 access，全部 caller 侧（supervisor → scout）
AmpPersistenceError: 0（job 日志 0；常驻 supervisor 无新增）
sender 位点已同时包含主 journal 与 job journal 两个键（35002 = 文件全量）
job 结果: done / completed / exit_code=0 / iterations=8
```

平台侧同步可见：当前小时 supervisor 27 / scout 27（就是这次 job 产生的）；
已结算的 11:00–12:00 为 supervisor **787**、scout **527**、writer **260**。

**结论**：job worker 作为 caller 的 access **现在会计入榜单**（与常驻
supervisor 同 AIC，平台按 AIC 归属，数字合并到 supervisor 那一行），
且不再与常驻进程抢 journal。

> ⚠️ 代价：并发 job 现在会在锁上**串行**（最多等 900s）。要真正并发，
> 需要让每个 job 用各自的 journal 目录（`ws-supervisor-job/<job_id>/logs/`），
> 并同步扩展 sender 的 glob —— 属于更大的改动，当前没必要。

## 2. 实际执行的命令与结果

### 2.1 文件同步（11 个）

```
/opt/wit/my-project/agents.py                                  （模型路由接入）
/opt/wit/my-project/acps_cluster/model_router.py               （新增：主用+保底）
/opt/wit/my-project/acps_cluster/llm_tools.py                  （两条路径统一）
/opt/wit/my-project/acps_cluster/feed_tasks.py                 （派发器）
.../deploy/patch_amp_access.py, amp_access_sender.py,
         preflight_check.py, go_live.sh,
         wit-amp-access-sender.service, wit-task-feeder.service,
         platform_identities.json
```

同步后修正：CRLF、属主 `wit:wit`、权限。**37 个 py 文件语法 0 失败**。

### 2.2 模型配置（写入 `.env.production`，已备份）

```ini
ACPS_MODEL_PRIMARY=bupt
BUPT_BASE_URL=https://myai.bupt.edu.cn/llm-gw/v1
BUPT_MODEL=deepseek-v4-flash
BUPT_HTTP_PROXY=http://127.0.0.1:18888   # 校园网出口反向隧道
ACPS_MODEL_PRIMARY_COOLDOWN=60
BUPT_API_KEY=<已写入，文件权限 644 wit:wit>
```

**关键前提**：服务器 DNS 解析不了 `myai.bupt.edu.cn`（校园内网域名），
但 `127.0.0.1:18888` 已有 sshd 反向隧道在监听。经该代理实测：

```
/models              HTTP=200  0.36s
chat/completions     HTTP=200  4.2s   （deepseek-v4-flash）
tool_calls           正常返回
```

### 2.3 服务器侧模型验证（真实调用，非 mock）

```
[1] 配置解析       ✓ 北邮网关/deepseek-v4-flash → 保底/MiniMax
[2] llm_tools.chat ✓ 拿到正文 '收到'
[3] provider 包装  ✓ complete 返回正文；stats={'primary_ok':1,'fallback_used':0}
[4] agent 循环     ✓ status=completed，工具真被调用 ['gw-ok']
```

### 2.4 计量补丁

```
14 个模块全部 +import +amp_hooks
重启清理：wit-yanshu@.service / run_supervisor.sh → amp_heartbeat_*.jsonl
```

> **执行中发现并修掉的真实问题**：补丁改的是
> `/opt/wit/.../deploy/wit-yanshu@.service`，但 systemd 实际加载的是
> `/etc/systemd/system/wit-yanshu@.service`。只改源文件 + `daemon-reload`
> **不会生效**。已 `cp` + `daemon-reload` 并核对 `ExecStartPre` 已变。

### 2.5 派发闭环（6 次真实调用）

| agent | 技能 | 结果 | 耗时 | 产物 |
|---|---|---|---|---|
| writer | paper.write | ok | 269.3s | 10537 B |
| scout | semanticscholar.search | ok | 177.5s | 2958 B |
| frontier | frontier.detect | ok | 56.3s | 958 B |
| critic | paper.review | ok | 121.4s | 2077 B |
| writer | paper.write | exception | 0.0s | 早期 TLS 信任锚问题（已修） |
| writer | paper.write | working→failed | 422.5s | 进程卡死，已被 reconcile |

**过程中修掉的两个真实缺陷**：

1. **信任锚选错**：原逻辑"自动从端点抓中间 CA"，在服务器上反而不如工作区自带的
   完整链（含 `Agent CA Root`）。已改为**优先用工作区 trust-bundle**，实测
   `openssl s_client -verify_return_code: 0`。
2. **只认 `completed` 会漏掉真产物**：AIP 的产物态是 `awaiting-completion`
   （等 leader 关单）。已改为把 `awaiting-completion`/`awaiting-input` 也算终态，
   并补上 **`complete` 关单**调用（协议规定只能在 `awaiting-completion` 时发）。
   实测关单后任务状态由 `awaiting-completion` → `completed`。

## 3. 唯一未闭环项：排行榜

### 3.1 已排除的原因

| 假设 | 验证方法 | 结论 |
|---|---|---|
| 没生成 access | `ls ws-*/logs/amp_access_*.jsonl` | 4 个 agent 共 360 条 |
| 没投出去 | 转发器日志 + 位点推进 | 已投 317 条，跳过 0 |
| 没进 Kafka | 直接消费 `amp.access` 扫描 | 找到我们 **193 条** |
| 平台没消费 | 查消费组位点 | 16 分区**积压 0** |
| 被拒收 | 抽样 `amp.access.dlq` | **无我们的记录** |
| 小时没结算 | `currentHourOpen` → 11:03 已 False | 结算后仍无 |
| 缺 `trace_id` | 统计榜单 agent 记录 | 27/31 也**没有** trace_id → 不是门槛 |

### 3.2 仍存在的结构差异（未证实是原因）

我们的记录 vs 榜单上活跃 agent 的记录：

| 字段 | 我们 | 对方 | 榜单中普遍性 |
|---|---|---|---|
| `body.caller.serviceName` / `callee.serviceName` | 缺 | 有 | 29/31 有 |
| `body.response.bodySizeBytes` | 缺 | 有 | — |
| `request.url` | `acps.direct.rpc` | 真实 URL（`http://host:8031/rpc`） | — |
| `request.method` | `POST` | `start` | — |
| 发射侧 | **callee 侧**（aic=被调方） | **caller 侧**（aic=调用方） | — |

最后一条值得注意：榜单样例记录的 `aic` 是**调用方**。我们目前只有被调方
（partner 侧入站 span）的记录——因为派发器是独立脚本直连/被调，supervisor
本身并不发起出站委派。若平台按 `record.aic` 归属，我们只会被记到 callee 一侧，
而 caller 侧为空。

### 3.3 建议的下一步（按性价比）

1. **让 supervisor 做一次真实出站委派**（而非脚本直连），这样 `ws-supervisor/logs/`
   里会出现 caller 侧 access 记录，补齐"发射侧=调用方"这一形态；
2. **请平台方核对**：给出可复现证据——topic `amp.access`、我们的 193 条消息
   （时间 02:00–02:35 UTC）、消费组 `monitor-server.access.writer.v1` 积压 0、
   DLQ 无记录。问题在"消费后如何聚合进 matched-stats"这一跳；
3. 若平台确认"自集群内部调用不计入匹配榜单"，则需要**跨机构调用**才能上分。

## 4. 回滚

```bash
# 模型：立即回 MiniMax
echo 'ACPS_MODEL_PRIMARY=none' >> /opt/wit/my-project/acps_cluster/deploy/.env.production
systemctl restart wit-yanshu@{scout,synthesis,writer,critic,contradiction,frontier,experiment,citation,kb,supervisor}

# 计量：还原代码
cd /opt/wit/my-project/acps_cluster && /opt/wit/venv/bin/python deploy/patch_amp_access.py --revert
systemctl daemon-reload && systemctl restart wit-yanshu@writer

# 转发器
systemctl disable --now wit-amp-access-sender

# 环境文件
cp /opt/wit/my-project/acps_cluster/deploy/.env.production.bak-20261019-094359 \
   /opt/wit/my-project/acps_cluster/deploy/.env.production
```

## 5. 日常检查命令

```bash
# 所有 agent 状态
systemctl list-units --no-legend 'wit-yanshu@*' | awk '{print $1, $3}'

# 模型路由（每个 agent 启动时打印一次）
journalctl -u wit-yanshu@writer -n 200 | grep '\[model\]'

# access 生成量
wc -l /opt/wit/my-project/acps_cluster/ws-*/logs/amp_access_*.jsonl

# 转发器
journalctl -u wit-amp-access-sender -n 20 | grep -E '已投|本轮'

# 派发战果
cd /opt/wit/my-project/acps_cluster
sudo -u wit bash -c 'set -a; . deploy/.env.production; set +a; /opt/wit/venv/bin/python feed_tasks.py --stats'
```
