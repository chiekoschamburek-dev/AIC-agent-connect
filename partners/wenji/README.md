# 问迹（WenJi）· AIP 智能体互联的可解释问数/问答智能体

第八届全球校园人工智能算法精英大赛 · AIC·智能体互联主题赛参赛作品（团队原创）。

问迹在"能问答"之上，回答三个更硬的问题：**答案能不能被核验、协作能不能被审计、能力能不能被发现**。
它把结构化问数（NL2SQL）、文档问答（RAG）、跨源多跳与公式计算统一到自研事件驱动内核，
以全链路 Trace 让每一步可解释；并基于 GB/Z 185（AIP）国标与 AIP-PUB 开源代码（ACPs SDK + WIT）
注册到梧桐智能体互联平台，完成**注册 → 自动发现 → 跨端互联 → 访问**全流程验证。

- AIP 接入与平台验证：[`docs/aic/README.md`](docs/aic/README.md)
- 与研枢集群的互联对接：[`INTEGRATION.md`](INTEGRATION.md)
- 第三方组件与数据声明：[`NOTICE.md`](NOTICE.md)
- 演示材料索引：[`docs/aic/`](docs/aic/)（截图、视频讲稿、PPT 稿、技术报告）

## 模型通道（双通道设计）

| 层 | Provider | 说明 |
|---|---|---|
| 引擎内核（NL2SQL/RAG/跨源/公式，每次任务 4~6 次调用） | **BUPT 校园网关 deepseek-v4-flash / qwen3.8-27b** | 免费额度；经校园网 18888 反向隧道（HTTP CONNECT 代理）访问；关闭思考后时延与 DeepSeek 持平（13.1s vs 13.2s）；并发场景下 qwen3.8-27b 更快（20~30s/任务 vs 108s），流量场景选它 |
| AIP 包装层（WIT，每次任务 1~2 次调用） | **本地代理 → BUPT 网关**（`wenji-llm-proxy`） | WIT 不支持自定义代理且不经记账层；本地反代解决 DNS/代理并注入关思考，成本 ¥0 |

配置项：`LLM_PROVIDER=bupt` / `BUPT_BASE_URL` / `BUPT_API_KEY` / `BUPT_PROXY_URL` / `BUPT_ENABLE_THINKING=0`；
包装层用 `ACPS_LLM_*` 独立覆盖（当前指向 `http://127.0.0.1:18999/v1`）。
成本账本对 BUPT 记 0 价（tokens 仍入账）。**注意**：WIT 自带 provider 的调用不经过
引擎记账层，曾导致"账本显示 ¥0 但 DeepSeek 实际扣费"——现已统一走本地代理，两层均为 ¥0。

## AIP 智能体互联接入

| 层 | 实现 |
|---|---|
| ATR 可信注册 | WIT `AcpsConfig` + `AcpsRegistration.integrated()`：登录→ACS 草稿→审核→AIC→EAB→mTLS 证书 |
| AIP 交互 | `AcpsDirect(rpc)` Partner 服务端（`/rpc` · `/stream` · `/health`），mTLS 双向认证 |
| 智能体发现 | Leader 侧 `AipDelegationHooks`：`acps_discover` / `acps_delegate` / `acps_task`，可调用平台其它智能体 |
| AMP 可观测 | 六类 journal（heartbeat/metrics/access/message/system/audit），访问日志按 `correlationId=taskId` 统计 |
| 对外能力 | `wenji.ask`（综合）· `wenji.data_query`（NL2SQL）· `wenji.doc_qa`（RAG 带引用）· `wenji.schema` |

```bash
cd backend
.venv/bin/python -m pip install ../../vendor/acps_sdk-2.2.0-py3-none-any.whl /path/to/wit_framework-2.1.0-*.whl
.venv/bin/python -m app.acps.cli check          # 本地校验声明/ACS（不访问网络）
.venv/bin/python -m app.acps.cli show-acs       # 生成 ACS 草稿（注册平台上传）
.venv/bin/python -m app.acps.cli register       # 推进可信注册（需 ACPS_USERNAME/PASSWORD）
.venv/bin/python -m app.acps.cli serve          # 启动 AIP Direct Partner + AMP
```

环境变量模板：`.env.acps.example`；平台侧 CLI 配置模板：`deploy/acps/acps-cli.toml.example`。

### 部署与长期值守（云服务器 systemd 用户服务）

全栈部署在云服务器（公网 8601 直连，无代理层）：

```bash
tools/systemd/install.sh            # 安装并启动 6 个服务：db/backend/frontend/serve/amp-forwarder/traffic
tools/systemd/install.sh status     # 全部状态 + 端口 + linger
systemctl --user restart wenji-serve  # 单服务操作
```

前提：`sudo loginctl enable-linger $USER`（已启用）。重启/注销后服务自动拉起。
数据库为用户态源码编译的 PostgreSQL 16 + pgvector（`~/opt/pgsql-16`，无需 root）。

## 快速启动

```bash
./deploy/quick_start.sh          # 一键：PG(pgvector) + Chinook 导入 + 术语库 + 知识库摄入 + 后端 + 前端 + 冒烟
```

- 后端 API：`http://localhost:8000` · 前端（Vue 3 + Naive UI）：`http://localhost:8090` · 数据库：`localhost:5433`（chinook/chinook123）
- 无需 API key 即可启动：默认 mock provider 可跑通全部流水线；真实模型在 `.env` 填
  `DEEPSEEK_API_KEY`/`QWEN_API_KEY` 并设 `LLM_PROVIDER=deepseek|qwen`（主力已定案 DeepSeek）
- 受限网络：`.env` 的 `DOCKER_REGISTRY_MIRROR`（镜像）/ `HF_ENDPOINT`（模型下载）已配置

## 功能一览

| 能力 | 说明 |
|---|---|
| 问数 NL2SQL | 术语改写→Schema Linking（71 表下 P50 160ms）→双步生成→sqlglot 校验→只读执行→自修复（≤3 轮）→空结果归因 |
| 问答 RAG | 查询改写（可选 HyDE）→混合检索（向量+BM25+RRF）→重排→引用生成→**逐句忠实度自检**（≤2 轮收敛重写） |
| 跨源多跳 | HYBRID 任务 DAG：依赖链/并行子任务/分区溯源，覆盖 5 类跨源问题（11/11 用例） |
| 公式计算（#6） | FormulaIR 自动登记（摄入期 LLM 抽取+校验）→三通道参数绑定（db/doc/user）→LaTeX→SymPy→双通道校验 |
| 澄清与多轮 | 槽位矩阵（外置配置+同义词词表）→选项式澄清→回填续查；指代消解改写（含改写对照展示） |
| 文档管理台 | 上传→质量报告→一键修复（方向/清晰度/OCR/繁简）→修复前后对比→修复后问答（清晰度实测 13x 提升） |
| 可解释性 | 全链路 Trace 树（时间线+DAG 双视图）· SQL/改写/引用对照 · PDF 页级定位 · 成本账本 |

## API 一览（`backend/app/main.py`）

| 类别 | 端点 |
|---|---|
| 对话编排 | `POST /api/chat`（JSON）· `POST /api/chat/stream`（SSE，契约见 docs/contracts/trace.md） |
| 单引擎 | `POST /api/nl2sql` · `POST /api/rag` |
| 文档管理 | `POST /api/docs/upload` · `GET /api/docs` · `GET /api/docs/{id}/quality` · `POST /api/docs/{id}/repair` · `GET /api/docs/{id}/pdf` |
| 可解释性/运维 | `GET /api/trace/{turn_id}` · `GET /api/trace` · `GET /api/usage`（含缓存经济 stats）· `GET /api/schema` · `GET /api/terms` · `GET /api/health` |

## 仓库结构

```
backend/
  app/
    core/       配置(.env) · LLM 抽象层(provider/缓存/记账) · Trace 模型 · Prompt 模板(11 家族,已冻结)
    db/         PG 会话(连接重试) · Schema 元数据/语义卡
    nl2sql/     改写(术语三级匹配) · Schema Linking(召回+精排+列级) · 校验/执行/空结果归因
    rag/        Embedding 抽象 · pgvector+BM25 · RRF · 重排 · 忠实度自检 · 查询改写/HyDE
    ingestion/  IR · 质量评估(#9) · 复杂度评分(#8) · MinerU/PaddleOCR 路由 · 修复工作流 · 公式抽取
    agent/      编排内核：意图/工具/HYBRID DAG/澄清/改写 · Trace+会话持久化 · SSE
    acps/       AIP 互联适配层：ACPs 配置/能力声明/WIT Agent/Runtime/CLI（见 docs/aic/）
    formula/    公式引擎：登记(store) · LaTeX→SymPy · 参数绑定 · 双通道校验
  eval/         分级评测 run_levels(L0/L1/L2) + 12 个用例文件 161 例 + 专项 runner(跨源/澄清/多轮/消融)
  scripts/      Chinook 导入 · 术语库抽取/别名生成 · 知识库 PDF 生成/摄入 · 基准语料/效率矩阵 · 冒烟
  tests/        255 个测试（单测 + 集成，含安全边界）
frontend/       Vue 3 + TS + Vite + Naive UI：对话流(SSE) · 数据卡片 · 引用 · 澄清按钮 · Trace 双视图 · 文档管理台
deploy/         docker-compose(pgvector+backend+frontend) · quick_start.sh · acps/(平台接入材料)
tools/          amp_forwarder/(平台 AMP 上报转发器) · 其它运维脚本
data/           db_raw(Chinook SQLite) · db_schema(DDL/术语种子) · docs_raw(10 份知识库) · docs_bad(3 份坏文档) · docs_bench(可再生成)
docs/
  aic/          AIP 接入与参赛文档（架构/注册流程/24h 冲刺计划/脱敏说明）
  contracts/    Trace JSON Schema · 摄入 IR · Prompt 静态层 · eval 用例格式（均已冻结，含变更记录）
  design/       设计报告与说明书（提交骨架）· Agent 内核设计
  milestones/   W1 demo 截图 · W5 效率矩阵（图+报告）
  onboarding/   B/C 上手文档
  weekly/       W1~W5 周报 · W4 验收报告 · 交接清单
```

## 评测与验证

```bash
cd backend
python eval/run_levels.py --level L0                 # 每类 5 例 smoke
python eval/run_levels.py --level L1                 # 全量、当前配置模型
python eval/run_levels.py --level L2 --providers deepseek,qwen --repeats 3   # 主备×3 重复
python eval/run_cross_source.py [--only-ids cs-011]  # 跨源专项
python scripts/demo_scenarios.py                     # 演示场景覆盖矩阵（10/10）
python scripts/bench_efficiency.py                   # 效率矩阵（3 文档规模 × 2 表规模，出图）
```

**最新基线（L2 全量，30 runs 零基础设施失败、三重复零方差）**：

| Family（用例数） | DeepSeek（主力） | Qwen（备胎） |
|---|---|---|
| NL2SQL（61） | **100%** | 83.6% |
| RAG 答案命中（41） | **100%**（检索 100%） | 93% |
| 多轮（8 脚本） | **8/8 · 34/34 轮** | 6/8 · 31/34 |
| 跨源多跳（11） | **11/11** | 9/11（路由 100%） |
| 澄清（30） | **30/30** | 7~8/30 |

结论：**路由与模型无关，生成质量与模型强相关**；效率矩阵（检索 10→1000 篇 P50 7→133ms、
Schema Linking 11→71 表 P50 55→160ms）与缓存经济（前缀命中 ~89%，节省 53% 成本）见
[docs/milestones/w5_efficiency](./docs/milestones/w5_efficiency/report.md) 与 `GET /api/usage`。

## 开发

```bash
# 后端
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q          # 255 passed（集成用例需 DB 已启动）
.venv/bin/python scripts/smoke_nl2sql.py      # NL2SQL 冒烟（mock 驱动）
.venv/bin/python scripts/smoke_rag.py         # RAG 检索冒烟（需嵌入模型）
.venv/bin/python eval/model_selection.py      # 选型评测（需 API key）

# 前端
cd frontend && pnpm install && pnpm test && pnpm build
pnpm dev                                       # http://localhost:5173，/api 代理到 8000
```

## AIC 提交物对照（智能体互联主题赛）

| 要求 | 现状 |
|---|---|
| 技术报告（按附件 2 大纲，PDF ≤10M） | 素材齐备：`docs/design/design_report.md` + `docs/design/technical_implementation.md`；AIC 版按 `docs/aic/README.md` 映射表重写 |
| 演示视频（MP4 3–5 分钟，含梧桐平台全流程） | 待录制：注册/发现/互联/访问 + 后台访问数据截图（`docs/aic/plan_24h.md` 有分镜清单） |
| 答辩 PPT（PDF） | 待制作：复用设计报告亮点页 + AIP 架构与平台数据 |
| 佐证材料 | 评测基线（L2 全量）· 效率矩阵 · 平台访问截图 |
| 代码包（网盘） | 本仓库 + `data/db_schema` + `data/docs_raw`(10 份) + 一键启动脚本；命名遵循 `团队编号-赛题名称-作品名称-XX` |

## 里程碑

W1 地基/选型/双引擎最小闭环 → W2 编排内核/缓存/Prompt 冻结 → W3 能力增强 #1/#2/#4/#5 +
Schema Linking → W4 跨源五类型/公式引擎/L2 首测 → **W5 打磨：L2 终验 + 效率矩阵 + 文档/视频/打包**。
逐周状态：[W5_progress.md](./docs/weekly/W5_progress.md)（含 B/C W5 复验与 A 收官）；
W4 验收报告：[W4_acceptance.md](./docs/weekly/W4_acceptance.md)。
