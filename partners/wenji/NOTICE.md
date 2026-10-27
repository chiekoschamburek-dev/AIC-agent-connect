# 第三方组件与数据声明（问迹模块）

本模块（`partners/wenji`）为团队自研作品，随仓库以团队许可证分发。以下组件/数据为第三方资产，
均按各自许可证使用；除特别说明外，**未修改上游源码，也未再分发模型权重**。

## 一、协议与运行时

| 组件 | 版本 | 来源 | 许可证 | 使用方式 |
|---|---|---|---|---|
| acps-sdk | 2.2.0 | AIP-PUB 开源社区（AtomGit/GitHub） | MulanPSL-2.0 | 仓库根 `vendor/` 分发；提供 AIC/ACS/AIP/ADP/AMP 数据对象 |
| wit-framework | 2.1.0 | 官方发行渠道 | 上游未提供许可证元数据 | 不随仓库分发；使用前自行安装；提供 ATR 注册、AIP Direct/Group、AMP 生命周期 |
| GB/Z 185《人工智能 智能体互联》 | — | 国家标准 | — | 协议设计与实现的依据（引用，不复制原文） |

## 二、运行时依赖（Python / 前端）

FastAPI、uvicorn、pydantic、sqlglot、psycopg、openai、pymupdf、jieba、numpy、
sentence-transformers、Pillow、opencc、reportlab、pytest 等均按其上游许可证（MIT/BSD/Apache-2.0/
LGPL 等）使用；Vue 3、Vite、Naive UI、ECharts、AntV X6 等前端依赖同理。完整清单与版本见
`backend/requirements.txt` 与 `frontend/package.json`。

## 三、模型与数据

| 资产 | 来源 | 许可证/授权 | 说明 |
|---|---|---|---|
| BGE-small-zh-v1.5（嵌入模型） | BAAI | MIT | 本地 CPU 推理；权重不入库（`models/` 已在忽略规则中） |
| Chinook 示例数据库 | https://github.com/lerocha/chinook-database | MIT | 仅作演示数据；导入脚本见 `backend/scripts/chinook_to_pg.py` |
| 知识库文档（10 份） | 本项目脚本生成 | 团队原创 | 用于演示 RAG/公式/文档质量链路，不含真实企业数据 |
| MinerU / PaddleOCR（可选） | 上游项目 | AGPL-3.0 / Apache-2.0 | 仅作为**外部 CLI** 调用（不链接、不再分发），用于复杂 PDF 解析与 OCR；未启用时系统走 PyMuPDF 路径 |

## 四、模型服务

- 引擎内核与 AIP 包装层均通过 OpenAI 兼容接口调用**校园网关**提供的
  `deepseek-v4-flash` / `qwen3.8-27b`（经团队授权的校园网通道，按平台使用条款调用）；
- 本模块不包含任何 API Key、证书私钥或平台凭据；`.env`、`.wit/`、`*.pem` 均在忽略规则内。

## 五、原创性说明

- 问迹的业务引擎（NL2SQL / RAG / 跨源 DAG / 公式计算 / 澄清 / Trace / 评测体系）为团队自研；
- AIP 适配层（`backend/app/acps/`：能力声明、桥接、任务级 AMP 记录、注册 CLI、LLM 代理等）
  为本届赛事期间新增实现；
- 上游协议栈仅通过公开 API/扩展点使用，修改范围与方式见仓库根 `NOTICE`。
