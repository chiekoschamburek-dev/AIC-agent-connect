# 事故诊断：梧桐调用排行榜上我们集群计数为 0（2026-10-19）

> 结论先行：**我们的 agent 从未产出过一条 AMP `access` 记录**，平台的排行榜是按
> `access`（+`message`）两个日志族聚合的，所以无论我们内部调了多少次，榜单上都是 0。
> 这不是"平台没收录我们"，也不是"调用次数不够"，而是**计量口径没被点亮**。

---

## 1. 榜单到底在数什么

平台首页的排行榜接口是 `GET https://wt.ioa.pub/internal/matched-stats/top`（免登录），
返回体里每条记录是：

```json
{"aic": "...", "count": 479, "accessCount": 479, "messageCount": 0}
```

也就是说 `count = accessCount + messageCount`。我们直接查了这个接口（`unit=task`）：

| 查询窗口 | 返回行数 | 我们的条目 |
|---|---|---|
| 2026-10-18 全天 | caller 21 / callee 57 | **0** |
| 最近 24 小时 | caller 21 / callee 57 | **0** |
| 最近 7 天 | caller 36 / callee 101 | **0** |
| 2026-09-24 起（榜单口径起点） | 200 条上限已满 | **0** |

逐日扫 10-09 → 10-19 共 11 个自然日，**我们 8 个 AIC 一次都没出现**。
（这 8 个 AIC 前缀是 `1.2.156.3088.1.BUPT.VTF97R.*`。）

## 2. 排除项：不是注册问题，也不是在线问题

先排掉三个"看起来像"的原因：

| 检查 | 方法 | 结果 |
|---|---|---|
| 平台认不认我们的身份 | `GET /registry-server/acps-atr-v2/acs/<AIC>` | 8/8 全部 `200`，`active=true`，技能齐全 |
| 发现接口能不能找到我们 | `POST /discovery/acps-adp-v2/discover` 按 aic 过滤 | 命中，`active=true`，端点 `https://192.0.2.10:<port>/rpc` |
| 服务是不是活着 | 从本机 TCP 探 8443–8470 | 8445/8446/8447/8448/8449/8450/8461/8468 全部 OPEN |

所以"注册、发现、监听"三件事都是通的。**榜单空白发生在注册之后、调用计量那一层。**

## 3. 根因：`amp_hooks` 没挂，SDK 因此从未发出 access

### 3.1 机制（读 SDK 字节码确认）

wit 2.1.0 的 `WitRuntime` 里有这样一个私有方法 `_amp_direct_access_observer`，
反汇编后逻辑是：

```python
def _amp_direct_access_observer(self):
    from wit.hooks.acps.amp import AmpAccessLoggingHooks as A
    if not any(isinstance(hook, A) for hook in self.compiled.amp_hooks):
        return None                    # ← 没挂钩子就直接返回 None
    return AmpDirectAccessRecorder(lambda: self.amp)
```

这个 observer 只在**挂载了 `AmpAccessLoggingHooks`** 时才会被创建，而它被注入到三处
Direct 传输边界：

| 注入点 | 覆盖方向 |
|---|---|
| `_ensure_aip_coordinator` | leader 出站（caller span） |
| `_handle_direct_partner_http` | partner 入站（callee span） |
| `_ensure_aip_group_coordinator` | Group 传输 |

记录落盘路径由 `_AmpJournal.path_for()` 决定：
`<workspace>/logs/amp_access_<AIC 的非法字符替换成 _>.jsonl`。

### 3.2 我们的代码：一次都没挂

全仓库搜索 `AmpAccessLoggingHooks` / `AmpMetricsHooks` / `AmpAudit*`：

```
my-project/docs/amp-compliance-and-gaps.md   ← 只有这份 2026-10-09 的文档提到它
（acps_cluster/ 下 0 处引用）
```

所有 `WitAgent(...)` 构造点的钩子清单是（抽样 `partner_writer.py`）：

```python
hooks=[*pep_run_hooks],
aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
# 没有 amp_hooks=...
```

`supervisor.py`、8 个 partner、`cluster_common.serve_partner()` 全都一样。
**所以 `amp_hooks` 恒为空列表 → observer 恒为 `None` → 一条 access 记录都不会产生。**

### 3.3 本地日志实证

扫描 `my-project/acps_cluster/ws-*/logs/` 下所有 AMP 文件，按 `log_type` 统计：

| log_type | 文件数 | 说明 |
|---|---|---|
| `heartbeat` | 7 | 有，且一直在写 |
| `system` | 7 | 每个进程只有 1 行（启动事件） |
| **`access`** | **0** | **`.jsonl` 文件根本不存在** |
| `metrics` / `message` / `audit` | 0 | 同样为空 |

这与 `docs/amp-compliance-and-gaps.md`（2026-10-09）里的自述一致：

> access | §5.4 每次交互 | ❌ 0 | wit 有 `AmpAccessLoggingHooks`，我们没挂

**那份文档十一天前就写明了这个缺口，但一直没补。**

## 4. 第二段断点：即便产出了，现有转发器也会把它丢掉

假设我们立刻挂上钩子、本地开始写 `amp_access_*.jsonl`，**榜单仍然是 0**，
因为 `deploy/amp_heartbeat_sender.py` 只转发一种日志：

```python
# amp_heartbeat_sender.py:180
if record.get("log_type") != "heartbeat" or not record.get("aic"):
    offsets[file.name] = new_offset
    continue          # ← 非 heartbeat 一律跳过（且推进位点，等于永久丢弃）
```

`deploy/amp_heartbeat_mirror.py:132` 有同样的过滤（连文件都不镜像）。
也就是说整条投递链路的名字就叫 "heartbeat sender"，**它天然不是 access sender**。

好消息：平台侧通道是现成的，Kafka 元数据探测（只读）显示

```
amp.access          16 分区
amp.access.dlq       1 分区
amp.message         16 分区
amp.audit            3 分区
amp.metrics          1 分区
```

—— `amp.access` 主题存在且分区数充足，**平台缺的不是通道，是我们这边没往里投。**

## 5. 完整断点图

```
真实调用（MCP/叮当/自建 leader 发起）
   │
   ├─(A)─ wit 传输层 ──► [断点①] 没有 AmpAccessLoggingHooks → observer=None → 不写本地 access
   │                                                        （本地 ws-*/logs/ 下 0 个 amp_access_*.jsonl）
   │
   ├─(B)─ 假设写出来了 ──► [断点②] amp_heartbeat_sender 只转 heartbeat，access 被 continue 丢弃
   │
   └─(C)─ 平台侧 ──► Kafka topic `amp.access`（存在，16 分区）→ AccessWriter → ClickHouse
                       → /monitor/acps-amp-v1/access/* → /internal/matched-stats/top → 排行榜
```

**两个断点都在我们这侧，平台侧是通的。**

## 6. 还有一层要留意：谁在调用决定了记在谁头上

`accessCount` 统计的是**经过 AIP 传输层的交互**，而我们的调用有两种来源：

| 调用来源 | 会不会被计数 | 说明 |
|---|---|---|
| 外部经 mTLS 打我们的 `/rpc`（叮当、其他参赛队、平台） | 会（callee 侧） | 这是我们真正想要的曝光 |
| supervisor 内部委派给 partner | 会（若挂上钩子） | caller=supervisor AIC，callee=partner AIC，两边都加分 |
| 我们在本机直接 `python xxx.py` 跑演示脚本 | **不会** | 不走传输层，没有 access 事件 |

`docs/issue-dingdang-task-payload.md` 记录过：叮当确实派过 66 次 `start` 命令给我们
（2026-10-11 前后）。**那些是真实的跨端调用，本该在榜单上留下数字——因为断点①全丢了。**
这反过来是一个很有说服力的证据：不是没人调我们，是我们没记账。

## 7. 修复方案（按性价比排序）

### 方案 A（必做，1 行/agent）：把 access 观察器点亮

给每个 agent 加 `amp_hooks=[AmpAccessLoggingHooks()]`。由于这是 SDK 的官方扩展点，
**不修改框架代码**，与"零侵入"原则一致。

```python
from wit import AmpAccessLoggingHooks

return WitAgent(
    ...,
    hooks=[*pep_run_hooks],
    aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
    amp_hooks=[AmpAccessLoggingHooks()],     # ← 新增
    protocols=[AcpsDirect()],
)
```

覆盖点（一处都不能漏，漏一个那个 agent 就永远 0）：

- `supervisor.py::build_supervisor_agent`（leader 出站，caller 侧）
- `partner_{scout,synthesis,writer,critic,contradiction,frontier,experiment}.py`
- `partner_{gpu,cpu,eval,kb,model,citation}.py`（本地 dev 身份那批，只要对外服务也算）
- `cluster_common.py` 里的公共装配（若有统一入口，优先改这里）

> ⚠️ **上线顺序有坑（两处，都会吃掉证据）**：
>
> 1. `deploy/run_supervisor.sh:22` —— `rm -f ws-supervisor/logs/*.jsonl`
>    （注释说 AMP 心跳 journal 校验会失败）；
> 2. `deploy/wit-yanshu@.service:18` —— **每个 partner 每次启动都执行**
>    `ExecStartPre=/bin/sh -c 'rm -f .../ws-%i/logs/*.jsonl; sleep <0-300s>'`。
>
> 也就是说：挂上 access 之后，**任何一次 systemd 重启都会把该 agent 的全部 access 记录删掉**。
> 心跳丢了下一轮 30 秒就能补一条，**交互记录丢了是永久丢失**。
> 必须在同一个修复里把这两处的通配符改成只匹配 `amp_heartbeat_*.jsonl`（或改成归档而非删除）。

### 方案 B（必做）：让转发器转 access

两条路，任选：

1. **扩展现有 sender**：把 `--topic` 从"单主题"改成按 `log_type` 路由
   （`heartbeat → amp.heartbeat`、`access → amp.access`），过滤条件从
   `!= "heartbeat"` 改成白名单 `in {"heartbeat","access"}`，位点文件按 `log_type` 分键。
2. **另起一个 access sender**：复制 sender 的位点/重试骨架，默认
   `--topic amp.access`、`--pattern "amp_access_*.jsonl"`。好处是心跳链路零风险，
   坏处是多一个 systemd 单元。

建议先做 2（不碰已经稳定的心跳链路），稳定后再合并。

### 方案 C（建议）：补 metrics，顺带把"调用量"做成能自证的数字

`AmpMetricsHooks(collect=...)` 可以按周期上报应用自有的数值指标。挂上之后
Monitor 里能看到成功率/时延，答辩时"访问量"就不只是一张榜单截图。

### 方案 D（必修）：别再让重启删掉日志

`run_supervisor.sh:22` 与 `wit-yanshu@.service:18` 都在启动时 `rm -f logs/*.jsonl`。
这是当初为了绕开"AMP 心跳 journal 校验失败"加的，代价是**每次重启清空全部证据**。
access 是**不可再生**的：心跳丢了 30 秒后补一条，交互丢了就是永久丢失。
修法：通配符收窄成 `amp_heartbeat_*.jsonl`，或改成 mv 到 `logs/archive/<时间戳>/`。

## 8. 验证方法（改完之后怎么证明它好了）

### 8.0 已经做过的本地实验（修复路径已证明可行）

不连平台、不发起调用，直接在 SDK 层验证了两件事：

```text
# 实验一：observer 的开关就是 amp_hooks
without hook -> observer = None
with hook    -> observer = AmpDirectAccessRecorder

# 实验二：写出来的记录长这样（AmpRuntime.emit_access 实测）
FILE: amp_access_1.2.156.3088.1.BUPT.VTF97R.UU5Z5X.6DL0JB.8XE6VL.1E38.jsonl
{"schema_version":"1.0","timestamp":"2026-10-18T16:25:49.798780Z",
 "aic":"1.2.156.3088.1.BUPT.VTF97R.UU5Z5X.6DL0JB.8XE6VL.1E38",
 "log_type":"access","log_id":"0ec91bd2-...",
 "body":{"durationMs":12.5,
         "request":{"method":"POST","route":"/rpc"},
         "response":{"statusCode":200},
         "caller":{"aic":"1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"},
         "callee":{"aic":"1.2.156.3088.1.BUPT.VTF97R.UU5Z5X.6DL0JB.8XE6VL.1E38"}},
 "resource":{"service.name":"yanshu.writer-partner"}}
```

这个结构与平台 `app/access/events.py::build_event_row` 的读取完全对得上
（`body.caller.aic` / `body.callee.aic` → `caller_aic` / `callee_aic`）。
**结论：缺的只是"挂一个钩子 + 转一个 topic"，格式本身没有问题。**

### 8.1 改完后的线上验收

```bash
# 1) 本地应当出现 access 日志（改前：0 个文件）
ls -l /opt/wit/my-project/acps_cluster/ws-*/logs/amp_access_*.jsonl
tail -1 /opt/wit/my-project/acps_cluster/ws-scout/logs/amp_access_*.jsonl | python3 -m json.tool
#    期望：log_type=access，含 trace_id / span_id / caller / callee

# 2) sender 应当开始投递（改前：只会打印 heartbeat 文件）
python3 deploy/amp_access_sender.py --mirror-dir /var/log/acps-amp --once --dry-run

# 3) 平台侧 30–60 分钟后应当在榜单出现（榜单按小时聚合，且有延迟）
curl -s "https://wt.ioa.pub/internal/matched-stats/top?startAt=$(date +%Y-%m-%dT00:00:00+08:00)&endAt=$(date +%Y-%m-%dT%H:00:00+08:00)&n=200" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
print([x for x in d['callee'] if 'VTF97R' in x['aic']])"
```

**端到端自测建议**：先只改 **writer** 一个 agent，用一次真实 mTLS 委派打它，
确认第 1、2 步出现数据，再推定第 3 步。不要一次全改——8 个 agent 同时改，
一旦哪里不对，你分不清是"钩子没生效"还是"投递没生效"。

## 9. 与"批量派任务"的关系（回答你最初的问题）

你原本想做的是"不断派任务把调用量刷上去"。诊断结果说明：

- **现在派再多任务，榜单仍然是 0**——因为计量断在传输层，不在任务数量；
- 断点修好之后，**批量派任务才有意义**，而且必须是**真实经过 AIP 传输层的调用**
  （本机直调演示脚本不计数）；
- 榜单是全平台横向排名（冠军已到 5510 次），所以修复之后仍然需要
  **持续、真实、多样**的调用量——这正是"不要一直干重复任务"的合理之处：
  重复调用同一能力既不体面也容易被判无效，而**覆盖不同 agent、不同能力、
  不同任务形态**的调用才是既真实又有效。

建议的顺序：**先修计量（本文 §7 A+B），再做批量派发，最后用 §8 的接口自证。**
在计量没通之前做派发，等于往一个没有水表的管子里灌水。
