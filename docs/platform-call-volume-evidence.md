# 平台调用量佐证材料（2026-10-19 起）

> 用途：比赛"作品材料"中需提交**平台后台智能体访问量**与**互联交互记录**。
> 本文给出可复现的取证方法与当时的真实数字。

## 1. 榜单接口与口径

平台首页排行榜的取数接口（免登录）：

```
GET https://wt.ioa.pub/internal/matched-stats/top
    ?startAt=<YYYY-MM-DDTHH:00:00+08:00>
    &endAt=<YYYY-MM-DDTHH:00:00+08:00>
    &n=200
```

约束（实测）：

- `startAt` / `endAt` **必须按小时对齐**并带 `+08:00`，否则返回
  `400 amp-invalid-time-range`；
- 返回 `count = accessCount + messageCount`；
- **按小时聚合**，当前小时标记 `currentHourOpen=true`，结算后转 `false`；
- **只统计 caller 侧 span**（`record.aic == body.caller.aic`）——
  被调方自己记的 inbound 记录不计入（2026-10-19 实测确认）。

## 2. 一键取证命令

```bash
# 当前小时榜单里我们的条目
curl -s "https://wt.ioa.pub/internal/matched-stats/top\
?startAt=$(date +%Y-%m-%dT%H:00:00+08:00)\
&endAt=$(date -d '+1 hour' +%Y-%m-%dT%H:00:00+08:00)&n=200" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
for role in ('caller','callee'):
    for x in d.get(role) or []:
        if 'VTF97R' in x['aic']:
            print(f\"{role:7s} {x['aic']}  count={x['count']} access={x['accessCount']}\")
"
```

截图建议：同时截「平台首页排行榜区域」+「上面这条命令的输出」，
两者时间一致，可互相印证。

## 3. 当时的实测数字

采集时间：**2026-10-19 11:18:31（UTC+8）**，窗口 `11:00–12:00`：

| 角色 | AIC | 调用次数 | accessCount |
|---|---|---|---|
| 调用方 | `1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6`（supervisor） | 223 | 223 |
| 被调方 | `1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5`（scout） | 161 | 161 |
| 被调方 | `1.2.156.3088.1.BUPT.VTF97R.1OGDWX.U9K14M.0CDI`（writer） | 62 | 62 |

> 数字随派发持续增长。取证时以当时查询结果为准，不要硬编码本文的数字。

## 4. 本地侧的对账证据（与平台数字互相印证）

```bash
# 各 agent 实际产生的 access 记录数
wc -l /opt/wit/my-project/acps_cluster/ws-*/logs/amp_access_*.jsonl

# 转发器投递量
journalctl -u wit-amp-access-sender -n 20 | grep '已投'

# 派发账本（每条都有 topic / 耗时 / 产物字节数）
cat /opt/wit/my-project/acps_cluster/.feeder/ledger.jsonl | python3 -m json.tool
```

2026-10-19 11:12 时的本地侧数字：

| agent | access 记录 | 说明 |
|---|---|---|
| supervisor | 536 条 | caller 侧（被平台计入） |
| writer | 333 条 | callee 侧 |
| scout | 207 条 | callee 侧 |
| critic | 32 条 | callee 侧 |
| frontier | 16 条 | callee 侧 |

转发器累计投递 **730 条**，跳过 0。

## 5. 一条真实交互的完整记录（可选附在材料里）

```bash
# 取 supervisor 最近一条 caller 侧记录
tail -1 /opt/wit/my-project/acps_cluster/ws-supervisor/logs/amp_access_*.jsonl \
  | python3 -m json.tool
```

关键字段：

- `log_type: "access"`
- `aic` == `body.caller.aic`（caller 侧，被计入）
- `body.caller.aic` / `body.callee.aic`（谁调了谁）
- `body.durationMs`、`body.response.statusCode`
- `correlation_id`（与 task 库里的任务可对上）

## 6. 数据链路（答辩可讲）

```
supervisor 发起真实委派（mTLS /rpc）
  → SDK 在传输边界写 amp_access_<AIC>.jsonl（AmpAccessLoggingHooks）
    → wit-amp-access-sender（本地 → Kafka amp.access，位点持久化、失败重试）
      → 平台 AccessWriter（消费组 monitor-server.access.writer.v1）
        → ClickHouse / 聚合
          → /internal/matched-stats/top → 排行榜
```

每一跳都有可查询的中间证据：本地文件行数、转发器日志位点、Kafka 偏移、
消费组 lag、DLQ 是否拒收。这些构成了完整的可审计链。

## 7. 注意事项

1. **只有 supervisor 发起的委派才计数**。脚本直连伙伴只产生被调方记录，
   平台不认（本文 §1 最后一条）。
2. **数字按小时结算**，刚跑完的调用要等当前小时关闭才会出现在正式榜单里
   （但 `currentHourOpen=true` 时也能看到实时部分数据）。
3. 平台上"访问量"是**全平台横向排名**，榜首量级在数千次。
   持续、真实、多样的协作任务才是可持续的做法。
