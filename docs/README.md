# 文档索引

> 单一事实源：测试数字与运行状态以 `python run_all_tests.py` 的实时输出为准，
> 本文只说明"哪篇文档讲什么、是否仍然有效"，不复制会漂移的计数。

## 先读这三篇

| 文档 | 用途 |
|---|---|
| [../README.md](../README.md) | 项目定位、目录结构、五部分导览、快速开始、安全约定 |
| [architecture.md](architecture.md) | 架构、三项互联创新、模块职责、guard 适配层、整合接缝 |
| [../NOTICE](../NOTICE) | 上游组件、外部组件、修改范围与许可证说明 |

## 协议与互联机制

| 文档 | 用途 |
|---|---|
| [PEP_design.md](PEP_design.md) | PEP 准入设计：任务级点对点委托 + 周期级域授权与对账 |
| [RFC_delegation_domain_draft.md](RFC_delegation_domain_draft.md) | 委托令牌与协作域 RFC |
| [workspace-partner.md](workspace-partner.md) | 工作区资源实例：四分区、task_id 作用域、伙伴读写 |
| [handoff-workspace-partner-access.md](handoff-workspace-partner-access.md) | 不建域开放工作区读写：token bundle + partner 客户端 |
| [design-task-workspace.md](design-task-workspace.md) | 任务工作区与产物归档设计 |
| [third-party-compute.md](third-party-compute.md) | 第三方算力 / 商业域：grant 与令牌边界 |

## 运行时与平台

| 文档 | 用途 |
|---|---|
| [deployment.md](deployment.md) | 一键部署：前置条件、子命令、systemd、guard/viz、排障 |
| [run-guard.md](run-guard.md) | 运行中守卫：规则/阈值/中止链路/observe-enforce |
| [static-resource-router.md](static-resource-router.md) | 静态资源路由：结构化 args → 能力，默认不跑 LLM（CPU/GPU/KB） |
| [inbound-result-delivery-aip-submit.md](inbound-result-delivery-aip-submit.md) | 入站产物交付：aip_submit、长文本附 .md、uri 模式 |
| [guard-redteam-2026-10-29.md](guard-redteam-2026-10-29.md) | guard 红队注入实验：A/B/C/N 四组、消融与逃逸结论 |
| [agent-aic-roster.md](agent-aic-roster.md) | agent 身份（AIC）清单与平台验收记录 |
| [amp-compliance-and-gaps.md](amp-compliance-and-gaps.md) | AMP 上报合规、差距与整改 |
| [platform-request-amp-heartbeat.md](platform-request-amp-heartbeat.md) | 平台侧心跳请求口径 |
| [l2-job-pipeline.md](l2-job-pipeline.md) | L2 令牌作业流水线 |
| [runbook-add-new-agent.md](runbook-add-new-agent.md) | 新增 agent：铸造身份、注册、发现、验收 |

## 团队组件与协作方

| 文档 | 用途 |
|---|---|
| [../acps_cluster/guard/README.md](../acps_cluster/guard/README.md) | 三维事件通信风险检测器（Storage / Timing / Behavioral） |
| [../guard-api/README.md](../guard-api/README.md) | guard HTTP API：端点、鉴权、supervisor 进程内挂载 |
| [run-guard.md](run-guard.md) | 运行中守卫：规则/阈值/中止链路/observe-enforce/回放校准 |
| [../acps_cluster/guard/docs/covert-event-guard.md](../acps_cluster/guard/docs/covert-event-guard.md) | guard 设计与威胁模型映射 |
| [../partners/designer/SUBMISSION_README.md](../partners/designer/SUBMISSION_README.md) | 设计 agent（ACPs Direct + AMP→Kafka） |
| [../partners/designer/SERVER_DEPLOY.md](../partners/designer/SERVER_DEPLOY.md) | 设计 agent 部署 |
| [../partners/wenji/README.md](../partners/wenji/README.md) | 问迹（可解释问数/问答智能体，AIP 接入） |
| [../partners/wenji/INTEGRATION.md](../partners/wenji/INTEGRATION.md) | 问迹与研枢集群的互联对接与验证 |

## 前端

| 文档 | 用途 |
|---|---|
| [../acps_cluster/deploy/VIZ_DEPLOY.md](../acps_cluster/deploy/VIZ_DEPLOY.md) | cluster-viz 部署与静态托管 |
| [../acps_cluster/deploy/workspace-rollout/README.md](../acps_cluster/deploy/workspace-rollout/README.md) | 工作区发布工具包（盘点/备份/重启/审计/E2E） |
