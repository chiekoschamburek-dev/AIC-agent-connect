# AIC·智能体互联技术报告（骨架）

> 按《附件 2 AIC·智能体互联技术报告参考大纲》组织；本文件是写作骨架 + 证据索引，
> 提交前导出为 PDF（≤10M），并全文脱敏（组织/人名/学校/导师/原赛事痕迹）。
> 素材来源：`docs/design/design_report.md`、`docs/design/technical_implementation.md`、
> `docs/milestones/`、`backend/var/eval/`（评测）、`docs/aic/README.md`（接入）。

## 一、作品概述

### 1.1 行业与技术背景
- 异构智能体互通难题：数据智能体与文档智能体各自封闭，"问数"与"搜知识"割裂。
- GB/Z 185（AIP）与 ACPs 协议族给出统一交互/发现/注册/监控框架；梧桐平台提供验证场景。
- 本作品切入点：把已验证的可解释问数/问答引擎通过 AIP 国标互联化，
  实现"可发现、可调用、可追溯、可计费度量"的数据智能体。

### 1.2 作品目标定位
- 硬性目标：接入梧桐平台完成注册、自动发现、跨端访问全流程（截图 + 日志为证）。
- 业务场景：跨源数据分析（数据库 + 企业文档）、公式计算、澄清多轮问答。
- 预期指标：NL2SQL/RAG/跨源评测基线（见表）、AIP 接入响应时延、平台访问次数。
- 验证要求：注册/发现/互联/访问全流程 + 后台访问数据留存。

### 1.3 核心创新（写 3 条，配图）
1. 引擎能力 Skill 化：既有 4 类能力投影为 ACS Skills，模型工具与平台发现共用一份声明（`capabilities.py`）。
2. Leader/Partner 同体：同一个 Agent 既能接单（Direct Partner）又能发现/委派（AipDelegationHooks），
   与引擎自身的跨源 DAG 编排同构。
3. Trace × AIP 调用链互证：引擎全链路 Trace 与 AIP task 生命周期对齐，可解释性从"单机"升级为"跨智能体"。

## 二、需求分析

### 2.1 行业痛点
- 协议不统一、跨平台无法互通、平台适配成本高（对齐评分"需求分析"）。
- 数据类智能体接入成本：重复实现注册/证书/监控 vs 复用 ACPs SDK + WIT。

### 2.2 场景与功能需求
- 文献/平台文档调研：AIP-PUB 规范、梧桐接入指南、ACPs 教程。
- 功能需求：问数、文档问答、跨源、schema 共享、任务状态机（accepted/working/awaiting-*/completed）。
- 性能/兼容/部署指标：见 `docs/milestones/w5_efficiency/`。

## 三、技术选型与方案设计

### 3.1 基础开发资源
- ACPs SDK 2.2.0 + WIT 2.1.0（MulanPSL-2.0，见仓库根 `vendor/` 与 `NOTICE`）；
- 遵循 GB/Z 185 的 AIC/ACS/ATR/AIP/ADP/AMP 六块协议；
- 开发环境：Python 3.12、PostgreSQL/pgvector、Vue3（`deploy/quick_start.sh` 一键部署）。

### 3.2 整体架构
- 图：交互式引擎（FastAPI 8000 + Vue 8090） + ACPs 适配层（WIT runtime） + 梧桐平台。
- 协议适配层：`backend/app/acps/{settings,provider,agent,runtime}.py`。
- 平台通信：`AcpsDirect(rpc)`，mTLS，`/rpc` + `/health`。

### 3.3 关键技术选型
- 注册发现：ATR integrated 流程（登录→ACS 草稿→审核→AIC→EAB→证书→finalize）。
- 数据传输：AIP Direct TaskCommand/TaskResult；结构化产物用 `StructuredDataItem`。
- 安全：mTLS + AIC 身份绑定（证书 URI SAN）；私钥材料仅本地 workspace。
- 协同调度：Leader 侧 `acps_discover/delegate/task` + 引擎 planner 的双层编排。

### 3.4 开源代码改造说明（评分"创新性"关键）
- 复用：ACPs SDK/WIT 作为协议栈（未改上游代码）；
- 自研：能力声明/桥接/注册 CLI/AMP 转发器 + 引擎侧全部流水线；
- 改造点清单：Skill 投影、任务计数指标、Access correlation=taskId 对齐平台统计。

## 四、项目实施

### 4.1 实施方案
- 流程：需求拆解 → 引擎盘点 → 能力 Skill 化 → ATR 注册 → 平台联调 → 全流程验证。
- 周期/资源/分工（按实际情况，脱敏）。

### 4.2 关键技术落地
- 注册全流程代码走读（`cli.py register --watch` 截图）；
- AIP 服务端启动与健康检查（`serve` 截图 + `/health`）；
- 与队友智能体互联时序图（发现→委派→回调→融合）；
- 兼容性问题与解决：公网端点/DNS/SAN、反向代理保留 mTLS、AMP 字段对齐。

## 五、应用成效

### 5.1 功能成果
- 平台后台截图：智能体发现、稳定访问；多智能体协同完整演示。

### 5.2 量化成效
| 指标 | 数值 | 证据 |
|---|---|---|
| NL2SQL 准确率（L2/61 例） | 100%（DeepSeek） | `backend/var/eval/` 基线表 |
| RAG 答案命中（41 例） | 100%（检索 100%） | 同上 |
| 跨源多跳（11 例） | 11/11 | `eval/run_cross_source.py` |
| 澄清（30 例） | 30/30 | `eval/` |
| Schema Linking 时延 | 11→71 表 P50 55→160ms | `docs/milestones/w5_efficiency/` |
| AIP 接入响应时延 | 待测（`/health` + task 往返） | 联调日志 |
| 平台累计访问次数 | 待统计 | 平台后台 + AMP access |

### 5.3 必要验证场景完成情况
- 逐项对照 `docs/aic/README.md` 第 6 节清单，附截图/日志编号。

### 5.4 开放开源贡献（鼓励项）
- AIP-PUB 社区 issue/分享截图（如有）；本仓库组件说明与许可证声明。

## 六、总结与展望

### 6.1 经验总结
- 成功经验：能力声明即资产（一次声明三处复用：模型工具/ACS Skill/文档）；
- 不足：AMP 上报链路依赖平台参数、公网部署门槛。

### 6.2 未来规划
- 引入 Group 模式做多方协同；扩展更多 Skill（图表生成、报表）；
- 把"引擎能力 → ACPs Skill"沉淀为接入模板，降低其它数据智能体接入成本。

## 七、附录

1. 测试数据：评测 JSON、效率矩阵图、AMP access 抽样；
2. 完整工程源码：仓库结构 + 一键部署 + 改造说明（`docs/aic/README.md` §4）；
3. 参考文献：GB/Z 185、ACPs 规范、梧桐使用指南、Chinook、BGE 等；
4. 佐证材料：演示视频说明、平台接入录屏、后台截图、软著/专利（如有）；
5. 开源贡献材料（如有）。

## 写作待办（明天填）

- [ ] 架构图重画为"引擎 + ACPs 适配 + 梧桐平台"（可用 draw.io/PPT）
- [ ] 平台截图编号入文（注册/AIC/发现/互联/访问各 1-2 张，注意脱敏）
- [ ] 时延数据：从 access journal 统计 P50/P95 后写入 5.2
- [ ] 队友智能体互联时序图（简单时序即可）
- [ ] 全文检索：组织/人名/学校/导师/原赛事关键词
