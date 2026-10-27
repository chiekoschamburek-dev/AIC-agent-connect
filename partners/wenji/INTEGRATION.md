# 问迹 × 研枢集群：互联对接说明

> 本文件说明问迹（`partners/wenji`）如何作为**外部成员**接入研枢（Yanshu）多智能体集群，
> 以及双方互相调用、心跳可见性与验证记录。

## 1. 身份与能力（平台权威值）

| 项 | 值 |
|---|---|
| 智能体名称 | 问迹·可解释问数问答智能体 |
| AIC | `1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN` |
| 对外端点 | `https://8.160.176.215:8601/rpc`（mTLS，证书 CN/SAN = AIC） |
| 协议 | ACPs Direct（JSON-RPC），WIT Framework 2.1.0 + ACPs SDK 2.2.0 |
| 能力（ACS Skills） | `wenji.ask`（综合）· `wenji.data_query`（NL2SQL）· `wenji.doc_qa`（RAG 带引用）· `wenji.schema`（库表元数据） |
| 心跳 / AMP | `amp_heartbeat_*`、`amp_metrics_*`、`amp_system_*`、`amp_access_*`（含 taskId 配对记录） |

## 2. 在集群名册中的位置

`acps_cluster/core/cluster_roster.py` 已登记为外部成员：

```python
RosterMember(
    key="wenji",
    name="问迹·可解释问数问答智能体（队友节点）",
    module="server_acps",          # 外部成员：不由本集群拉起
    workspace="ws-wenji",
    port_env="ACPS_WENJI_PORT",
    default_port=8601,
    declared_capabilities=("wenji.ask", "wenji.data_query", "wenji.doc_qa", "wenji.schema"),
)
```

名册从 `ws-wenji/acps/acs.final.json` 解析 AIC / endpoint / skills；因此 Supervisor 的
Discovery 结果与名册条目始终一致（不做硬编码）。

## 3. 能力路由建议（Supervisor 侧）

| 用户意图 | 目标能力 | 说明 |
|---|---|---|
| 结构化问数（销量/客户/员工/发票…） | `wenji.data_query` | 返回 SQL + 数据行 + 图表建议 |
| 制度/手册/总结类问答 | `wenji.doc_qa` | 返回答案 + 文档名/页码引用 |
| 需要同时查库与查文档 | `wenji.ask` | 内核自动规划跨源 DAG |
| 需要了解可查询的库表 | `wenji.schema` | 表与字段摘要（供编排决策） |

## 4. 双向调用

### 4.1 集群 → 问迹（Supervisor 作为 Leader）

Supervisor 通过 Discovery 检索 `问数/数据库/文档问答` 即可命中问迹，随后
`acps_delegate` 下发任务；问迹返回 `awaiting-completion` 后由 Supervisor `complete`。
问迹侧任务状态机完整：`accepted → working → awaiting-input / awaiting-completion → completed`。

### 4.2 问迹 → 集群（问迹作为 Leader）

```bash
cd partners/wenji/backend
# 发现集群伙伴（问迹内置 Leader 能力）
.venv/bin/python scripts/acps/aip_selftest.py --query "科研任务调度规划" --no-delegate
# 向研枢-Supervisor 委派（把 <SUPERVISOR_AIC> 换成名册中的值）
.venv/bin/python scripts/acps/aip_selftest.py \
  --partner-aic <SUPERVISOR_AIC> --query "科研任务调度规划" \
  --question "请介绍你能提供的科研任务调度能力，并给出一个示例任务规划。"
```

2026-10-19 实测：问迹 → 研枢-Supervisor 委派 `completed`，返回其能力矩阵与示例计划；
同日的问迹自测（自委派）也完整走通全部状态机。

## 5. 心跳与在线可见性

- 问迹由 `wenji-serve`（systemd 用户单元）常驻，`wit` 在 `logs/amp_heartbeat_<AIC>.jsonl`
  周期写心跳；`tools/amp_forwarder` 增量投递到平台 Kafka `amp.heartbeat`；
- 平台 Monitor → Discovery aliveMap 据此判定在线；
- 集群侧心跳镜像/发送器按 `ws-*/logs/amp_heartbeat_*.jsonl` 通配，天然兼容外部成员。

## 6. 运维与边界

| 项 | 说明 |
|---|---|
| 部署 | VPS 原生部署：`db / backend / frontend / llm-proxy / serve / amp-forwarder` 六个 systemd 用户单元（Linger 已启用） |
| 端口 | 8601 由安全组放行，mTLS 直连，无反向代理 |
| ACS 版本 | 平台已批准 `v1.0.0`（4 技能 + rpc）。WIT 2.1.0 的注册状态机为一次性设计，**不要就地修改声明**（会触发 `acs_fingerprint_changed_after_submission` 并拒绝服务）；如需扩展技能，应使用新版本号 + 新工作区重新注册 |
| 依赖边界 | 问迹自带的 ACPs 适配层（`backend/app/acps/`）与集群的 `acps_cluster/` 相互独立；双方仅通过 AIP/mTLS 交互，不共享进程与存储 |
