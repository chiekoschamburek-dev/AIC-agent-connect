# 给平台（梧桐 wt.ioa.pub）的对接请求 —— AMP 心跳采集

> 用途：**可直接复制转发**给平台运维/对接人（微信、邮件、群都行）。
> 我们自己留档的版本见 `docs/issue-wt-dingdang-cannot-discover-our-agents.md` 与
> `docs/amp-compliance-and-gaps.md`；技术细节见 `deploy/AMP_OPTION_A_HANDOFF.md`。

---

## 正文（复制以下内容）

**主题：请求把我们的 AMP 心跳纳入平台采集（我们在平台上"在线"状态不生效）**

各位老师好，我们是在 wt.ioa.pub 上注册了 8 个智能体的参赛队伍（研枢集群，账号 pxyx）。
现在遇到一个只靠我们这边无法解决的问题，想请平台协助确认一个采集口径。

**现象：**

- 我们的智能体在平台"发现"里是能查到的（按技能词检索经常排第一），但：
  - 首页「在线状态查询」输入我们的 AIC → 查不到；
  - 首页「热门智能体 / 最新上架」面板里从来没有我们；
  - 发现接口响应里的 `aliveMap` 完全不覆盖我们的 AIC。
- 我们对照实验过：**平台 `filtered`+`aic` 查询只返回"平台认为存活"的智能体**。
  拿平台 trending 里 `alive:true` 的其他智能体去查能查到；拿同样"索引里有、但 aliveMap 未覆盖"的
  第三方智能体去查也是 0 条——说明这是**存活集合**的问题，不是我们注册或技能描述的问题。

**我们这侧已经做好的：**

- 8 个智能体的 AMP 心跳按规范持续落盘（每 30s 一条，含 `schema_version / timestamp / aic /
  log_id / log_type=heartbeat / body.uptimeSeconds / resource`）；
- 已把心跳整理成固定目录、固定文件名（只保留 300s 内的新鲜心跳，不会灌历史数据）：
  - 主机：`192.0.2.10`
  - 目录：`/var/log/acps-amp/yanshu/`
  - 文件：`amp_heartbeat_scout.jsonl`、`amp_heartbeat_synthesis.jsonl`、`amp_heartbeat_writer.jsonl`、
    `amp_heartbeat_critic.jsonl`、`amp_heartbeat_contradiction.jsonl`、`amp_heartbeat_frontier.jsonl`、
    `amp_heartbeat_experiment.jsonl`、`amp_heartbeat_supervisor.jsonl`
  - 内容就是原始 AMP JSONL 行，未做任何改写（`log_id` 保持源端生成，未重生成）。

**想请平台二选一（哪条都行，我们配合）：**

- **方案 A（你们采集，我们零运维）**：
  在 `amp_forwarder`（Fluent Bit）里把上面这个目录当作一个额外日志根，加一段 tail：
  ```ini
  [INPUT]
      Name             tail
      Path             <你方挂载点>/yanshu/amp_heartbeat_*.jsonl
      Tag              amp.heartbeat
      Parser           amp_audit_json
      Refresh_Interval 2
      Read_From_Head   False
      DB               /var/lib/fluent-bit/yanshu-hb.db
  ```
  如果网络不便直读我们的目录，**请告诉我们你方希望文件落在哪里**
  （你方某个可写路径 / 共享目录 / 让我们推送到某台机器的某个目录），我们把文件送过去。

- **方案 B（我们侧自建 Forwarder，照你们模板）**：
  按你方 `amp_forwarder` 模板在我们主机跑一个等价转发器，直投 `192.0.2.20:19092` 的
  `amp.heartbeat`。若走这条，只需确认两件事：
  1. 该 listener 是否接受外部主机作为 producer；
  2. 是否有 topic/分区/键的约定（我们看到 `amp.heartbeat` 是单分区），或先用
     `amp.external.smoke` 验证再切正式。

**我们的 8 个 AIC（心跳内容里的 `aic`）：**

```
scout          1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5
synthesis      1.2.156.3088.1.BUPT.VTF97R.YDWSUJ.RBUQ6T.12KC
writer         1.2.156.3088.1.BUPT.VTF97R.WATDMX.AQFZR5.0NQN
critic         1.2.156.3088.1.BUPT.VTF97R.71P6ML.DDF7EG.13B2
contradiction  1.2.156.3088.1.BUPT.VTF97R.0FQNAG.ZO888M.0FW5
frontier       1.2.156.3088.1.BUPT.VTF97R.ZAJ69L.IOEX2R.082D
experiment     1.2.156.3088.1.BUPT.VTF97R.5CJ349.JNZ5VU.0E5E
supervisor     1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6
```

麻烦帮我们确认走 A 还是 B、以及需要提供什么（路径 / 账号 / topic 约定），我们随时配合。
另外如果平台侧本来就有"外部智能体接入 AMP"的推荐做法，也请直接告诉我们，我们照做即可。

---

## 发送前的三条备忘（我们自己看）

1. **不要再问"要不要鉴权"**：社区 `amp_forwarder` 模板里没有任何 SASL/ACL，端口 `19092` 也是
   他们跨机访问用的端口；问这个只会拉长往来。
2. **优先争取方案 A**：对我们零运维，且符合官方"采集层由平台负责"的叙述；
   B 只是兜底（技术上已就绪，dry-run 通过）。
3. **如果对方问"为什么不用官方 demo 的日志目录"**：官方拓扑假设 agent 与 Forwarder 同机
   （`hosts.example.yml` / `hosts-multi.example.yml` / `demo_partner` 的 `AMP_LOG_DIR` 都指向同一
   `acps_log_root`），我们的 agent 在自己云主机上，所以需要他们给一个采集口径。
