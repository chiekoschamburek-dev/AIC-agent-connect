# 问迹 · AIP 智能体互联接入说明

> 团队级技术报告全文：[`report/AIC_technical_report.md`](report/AIC_technical_report.md)

> 第八届全球校园人工智能算法精英大赛 · AIC·智能体互联主题赛
> 最后更新：2026-10-14（距初赛提交截止 2026-10-15 20:00 约 21 小时）

## 1. 作品定位

问迹把已有的**可解释问数/问答引擎**（NL2SQL + RAG + 跨源多跳 + 公式计算 + 澄清多轮 + 全链路 Trace）
通过 **GB/Z 185（AIP）国标**接入梧桐智能体互联平台，完成注册、发现、互联、访问全流程验证。

- **Partner 侧**：向平台注册 `wenji.*` 能力，`AcpsDirect(rpc)` + mTLS 对外服务；
- **Leader 侧**：内建 `acps_discover / acps_delegate / acps_task`，可发现并调用队友的智能体，
  在同一任务里融合多来源结果（与引擎自身的跨源 DAG 编排天然同构）；
- **可解释性加成**：AIP 调用链与引擎 Trace 对应，可在交互式前端展示"谁调用了谁、为什么、结果出处"。

## 2. 架构

```
                 梧桐智能体互联平台（wt.ioa.pub）
   ┌──────────────┐  注册/审核/AIC/EAB/mTLS   ┌───────────────────────────┐
   │ ACPs Registry│◄──────────────────────────│  WIT AcpsConfig           │
   │ ACPs CA      │                           │  （ATR 状态机）             │
   │ Discovery    │◄── ACS 发布/发现 ────────►│  AcpsDirect(rpc) Partner   │
   │ AMP Monitor  │◄── amp_*.jsonl ───────────│  AipDelegationHooks Leader │
   └──────────────┘                           └───────────┬───────────────┘
                                                          │ 同进程调用
                                      ┌───────────────────▼───────────────┐
                                      │ backend/app/acps/                 │
                                      │  capabilities → bridge → 引擎：    │
                                      │  AgentKernel / NL2SQL / RAG / ... │
                                      └───────────────────┬───────────────┘
                                                          │
                                      FastAPI 8000 + Vue3 8090（交互式演示/Trace 双视图）
```

## 3. 能力清单（ACS Skills）

| Skill | 说明 | 输入 | 输出 |
|---|---|---|---|
| `wenji.ask` | 综合问数/问答（内核自动路由 NL2SQL/RAG/跨源/澄清） | 自然语言 | 答案 + 引用 + SQL/行预览 + 状态 |
| `wenji.data_query` | 结构化问数专线 | 自然语言 | SQL + 数据行 + 图表建议 |
| `wenji.doc_qa` | 文档问答专线（RAG） | 自然语言（可限定文档） | 答案 + 文档/页码引用 |
| `wenji.schema` | 业务库表结构摘要 | 无 | 表与字段清单 |

## 4. 目录与代码

| 路径 | 作用 |
|---|---|
| `backend/app/acps/settings.py` | 全部 ACPs 环境变量（wt/custom/ioa_pub 三种形态） |
| `backend/app/acps/provider.py` | WIT 包装层模型（默认复用 DeepSeek 配置） |
| `backend/app/acps/bridge.py` | 能力 → 既有引擎的桥接；任务计数（供 AMP metrics） |
| `backend/app/acps/capabilities.py` | 4 个 `@capability`（同时投影为模型工具与 ACS Skill） |
| `backend/app/acps/agent.py` | WitAgent 声明（指令、Partner 绑定、Leader 委派、AMP hooks） |
| `backend/app/acps/runtime.py` | AcpsConfig 构造 + 注册/服务生命周期 |
| `backend/app/acps/cli.py` | `check / show-acs / register / serve / status` |
| `backend/tests/test_acps_skeleton.py` | 离线单测（声明编译 / ACS 投影 / 桥接纯函数） |
| `deploy/acps/` | acps-cli.toml 模板 + 平台接入材料说明 |
| `tools/amp_forwarder/` | AMP journal → 平台 Kafka 转发器（联调/演示） |

## 5. 快速开始

```bash
cd backend
.venv/bin/python -m pip install /path/to/acps_sdk-2.2.0-py3-none-any.whl /path/to/wit_framework-2.1.0-*.whl
.venv/bin/python -m pytest tests/test_acps_skeleton.py -q      # 7 项离线校验
.venv/bin/python -m app.acps.cli check                          # 声明/ACS 本地校验
.venv/bin/python -m app.acps.cli show-acs                       # 生成 ACS 草稿
.venv/bin/python -m app.acps.cli smoke --question "销量前十的曲目"   # 引擎冒烟（不经 AIP）
.venv/bin/python -m app.acps.cli register --watch                # 推进注册并盯审核（需账号密码）
.venv/bin/python -m app.acps.cli serve                          # 启动 AIP 服务 + AMP
```

环境变量：`.env.acps.example` → 复制为 `.env.acps` 并填写。

## 6. 平台验证清单（对应评分"必要验证场景"）

| # | 验证项 | 操作 | 留证 |
|---|---|---|---|
| 1 | 智能体注册 | `register`（或网页平台上传 ACS）→ 拿 AIC | 注册平台截图（含 AIC，脱敏组织信息） |
| 2 | 自动发现 | 队友 Leader / 平台发现 Agent 按能力检索到问迹 | Discovery 查询结果截图 |
| 3 | 跨端互联 | 队友智能体发起 `wenji.ask` 任务，问迹经 mTLS 接单并返回 | 双方任务日志 + Trace 截图 |
| 4 | 访问数据 | 平台后台访问量/调用记录 | 后台截图 + `amp_access_*.jsonl` 抽样 |
| 5 | 反向调用 | 问迹 Leader 侧 `acps_discover` + `acps_delegate` 调用队友智能体 | 委派记录/Trace 对照截图 |

## 7. 评分项映射（附件 1）

| 评分项 | 分值 | 本作品落点 |
|---|---|---|
| 创新性 | 25 | 在 AIP 开源代码（ACPs SDK + WIT）上做深度二次开发：引擎能力 Skill 化、Leader/Partner 双角色（委派与接单同进程）、Trace 与 AIP 调用链互证 |
| 需求分析 | 10 | 企业"问数民主化 + 知识问答"双痛点；异构智能体各自掌握数据/文档能力，互联后可组合出跨源分析 |
| AI 技术应用 | 20 | 稳定完成平台接入、自动发现、双向通信；NL2SQL 自修复 + RAG 忠实度自检 + 跨源 DAG |
| 项目实施 | 15 | 现有 255 测试基线 + AIP 适配层离线单测 + 一键脚本；文档化注册/服务流程 |
| 应用成效 | 10 | L2 评测基线（NL2SQL 100% / RAG 100% / 跨源 11/11）+ 平台访问数据 + 接入响应时延 |
| 总结与展望 | 10 | 复盘 AIP 改造路径；沉淀"引擎能力 → ACPs Skill"的可复用范式 |
| 附加分 | 10 | 源码规范可一键部署（1-3）· 视频完整展示全流程（1-3）· 平台访问频次（1-4，**需尽快铺量**） |

## 8. 风险与注意事项

1. **审核是唯一不可控瓶颈**：ACS 提交后等平台管理员批准，建议今晚立即提交，白天盯状态。
2. **公网可达性**：`ACPS_PUBLIC_ENDPOINT` 必须是平台/队友可访问的 HTTPS 地址，且证书 SAN 覆盖该主机；
   本机演示可用反向隧道（需转发原始 TCP 以保留 mTLS）。
3. **AMP 上报参数未定**：平台 Kafka（wt.ioa.pub:19092）的 topic/认证细节需从 QQ 群/平台文档确认，
   转发器已留好配置位（`tools/amp_forwarder/`）。
4. **访问频次附加分**：越早接入越好；可让队友 Leader 的演示脚本反复调用（每次调用都产生 access 记录）。
5. **材料脱敏**：提交包禁止出现组织/人名/学校/导师信息（全文检索后导出）。
