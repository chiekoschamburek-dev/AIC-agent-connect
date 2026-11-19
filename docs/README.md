# 文档索引

> 单一事实源：对外引用的数字（受控文件数、测试通过数、docstring 完整率、语言占比）
> 以 [`verification.md`](verification.md) 为准，它由 `acps_cluster/tools/repo_facts.py`
> 与 `run_all_tests.py` 复算得出；本文只说明"哪篇文档讲什么、是否仍然有效"，
> 不复制会漂移的计数。

## 先读这三篇

| 文档 | 用途 |
|---|---|
| [../README.md](../README.md) | 项目定位、目录结构、五部分导览、快速开始、安全约定 |
| [architecture.md](architecture.md) | 架构、三项互联创新、模块职责、guard 适配层、整合接缝 |
| [../NOTICE](../NOTICE) | 上游组件、外部组件、修改范围与许可证说明 |

## 核验与实验

| 文档 | 用途 |
|---|---|
| [verification.md](verification.md) | **可核验事实**：每个对外数字的定义、复跑命令、主张→代码→测试映射 |
| [../experiments/ablation/实验报告-三机制对照-真实数字.md](../experiments/ablation/实验报告-三机制对照-真实数字.md) | 四组对照实验的方法、完整结果与复跑方式 |
| [PEP-result.md](PEP-result.md) | PEP/域/配额逐轮实现记录与审查缺陷清单（含当时的用例数，历史口径） |

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
| [bidirectional-invocation.md](bidirectional-invocation.md) | 跨智能体双向调用：单向的根因、入站技能快速通道、probe/pack 取证 |
| [p0-verification-report.md](p0-verification-report.md) | 多入口编排 P0：ACS 指纹、后台作业底座、问迹并发的验证结论与命令 |
| [multi-entry-p1.md](multi-entry-p1.md) | 多入口编排 P1：research.submit/status、幂等根任务、按技能入站白名单 |
| [partner-reporting.md](partner-reporting.md) | 伙伴 → Supervisor 上报任务：run-scoped 工具，不改 ACS/不新增技能 |
| [multi-entry-p2-grant.md](multi-entry-p2-grant.md) | 多入口编排 P2：域章程双签、grant 签发/核验、问迹侧最小改动点 |
| [multi-entry-p3-dispatch.md](multi-entry-p3-dispatch.md) | 多入口编排 P3 设计：回派子任务、根任务上下文、按 root_id 取证 |
| [multi-entry-orchestration-plan.md](multi-entry-orchestration-plan.md) | 实施计划：Partner 多入口接入、Supervisor 统一编排、请求分流、防循环与业务验收 |
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

## 问题单与现场记录

| 文档 | 用途 |
|---|---|
| [server-rollout-2026-10-26.md](server-rollout-2026-10-26.md) | 研究伙伴读写工作区的现网发布记录与四个实测缺陷 |
| [incident-2026-10-09-code-sync-acs-guard.md](incident-2026-10-09-code-sync-acs-guard.md) | 代码同步误伤 ACS/guard 的复盘 |
| [incident-2026-10-14-kb-endpoint-firewalld.md](incident-2026-10-14-kb-endpoint-firewalld.md) | KB 端点被 firewalld 拦截的定位 |
| [incident-2026-10-15-platform-cert-expired.md](incident-2026-10-15-platform-cert-expired.md) | 平台证书到期的处置 |
| [incident-2026-10-19-ranking-zero.md](incident-2026-10-19-ranking-zero.md) | 排行榜归零的口径排查 |
| [incident-2026-10-23-amp-hooks-lost.md](incident-2026-10-23-amp-hooks-lost.md) | AMP hooks 丢失与恢复 |
| [issue-wit-2.1.0-ip-endpoint-san.md](issue-wit-2.1.0-ip-endpoint-san.md) | 平台 ACS 缺 `certificate.altNames` 导致 IP 端点证书只有 URI SAN |
| [issue-wit-2.1.0-discovery-filter-400.md](issue-wit-2.1.0-discovery-filter-400.md) | discovery 过滤条件 400：平台已废弃旧参数 |
| [issue-wt-dingdang-cannot-discover-our-agents.md](issue-wt-dingdang-cannot-discover-our-agents.md) | 外部 agent 发现不到本集群成员的排查过程 |
| [issue-dingdang-task-payload.md](issue-dingdang-task-payload.md) | 外部任务载荷不兼容（dataItem 结构）的定位与规避 |
