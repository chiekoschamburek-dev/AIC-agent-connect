# 答辩 PPT 逐页内容稿（12 页）

> 配合 `ppt_outline.md` 的结构；每页给出标题、正文要点与视觉素材，
> 制作时直接取用；导出 PDF 提交（不得出现学校/人名/导师，截图打码 organization）。

| 页 | 标题 | 正文（可直接排版） | 视觉素材 |
|---|---|---|---|
| 1 | 问迹 · AIP 智能体互联的可解释问数/问答智能体 | 一次能力声明，三处复用（模型工具 / ACS 发现 / 文档）；注册·发现·互联·访问全流程通过梧桐平台验证 | 01_home_aip.png |
| 2 | 问题：数据智能体的三重孤岛 | ①协议割裂——数据/文档/业务智能体接口各异；②发现缺失——运行期不知道"谁能做这件事"；③协作不可核验——调用链与访问留痕分散 | 三痛点示意（自绘） |
| 3 | 方案总览 | 引擎层（问数/问答/跨源/公式/澄清，全链路 Trace）＋ ACPs 适配层（约 1.2k 行自研，基于 AIP-PUB 的 ACPs SDK 2.2.0 + WIT 2.1.0）＋ 梧桐平台（注册/发现/监控） | 架构图（报告 §3.2.1 重绘） |
| 4 | 可信注册 | ATR integrated 流程：登录→ACS 草稿→审核→AIC→EAB→mTLS 双证书→激活校验（CN/SAN/EKU/私钥/信任链五项全过才启动）；AIC：`...BFZ0FH.F2KZXJ.15ZZZG.19CN` | 注册平台截图（打码）+ 证书链示意 |
| 5 | 能力即声明 | `@capability` 一次声明 → 模型工具 + ACS Skill + 文档；4 个对外能力：wenji.ask / data_query / doc_qa / schema | 代码片段 + discovery 命中截图 |
| 6 | 互联实测：全状态机 | 问迹⇄研枢-Supervisor 双向委派；自测覆盖 accepted→working→**awaiting-input**（澄清 continue 流）→awaiting-completion→completed；mTLS 身份绑定（证书 AIC == senderId） | selftest 终端输出 |
| 7 | 可观测：AMP 六类日志 | 心跳→平台 Kafka→Monitor→Discovery aliveMap（在线可见）；访问按 taskId 配对计数；自研 TaskAccessHooks 补齐平台计数口径（wit 边界记录为 rpc-id，任务级补发 taskId）；metrics 实时上报真实成功率/时延分位 | amp 日志样本 + 前端 AIP 面板 |
| 8 | 引擎硬指标 | NL2SQL 100%（61 例）/ RAG 100%（41 例）/ 跨源 11/11 / 澄清 30/30；主备模型三重复零方差 | 指标表（报告 §5.2） |
| 9 | 效率与成本 | 检索 10→1000 篇 P50 7→133ms；Schema Linking 11→71 表 55→160ms；AIP 公网 health P50≈0.16s、任务 P50≈3.8s；缓存命中 89%；全部实验成本 ¥4.19 | 效率矩阵图 + AIP 时延表 |
| 10 | 开源改造划分 | 上游复用：ACPs SDK / WIT（未修改，MulanPSL-2.0）；自研：能力声明与引擎桥接、任务级 access 钩子、会话隔离（contextvar）、注册 CLI、AMP 转发器、云原生部署（systemd 六服务） | 划分表（报告 §3.2.3） |
| 11 | 创新点 | ①能力声明即资产——声明/发现/工具三处一致；②Leader/Partner 同体——接单与委派共存，与引擎跨源 DAG 同构；③Trace×AIP 互证——单机可解释升级为跨智能体可审计 | 三卡片 |
| 12 | 展望 | AIP Group 多方协同、stream 增量输出、更多 Skill（图表/报表）、接入模板回馈社区 | 路线图 |

## 制作提示

- 主色沿用前端 #d85a3b；每页一个论点，字数 ≤60
- 数据页（8/9）用表格呈现，来源标注报告章节
- 第 6/7 页是评分硬性项（AI 技术应用 + 应用成效），篇幅可稍大
