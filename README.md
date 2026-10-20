# 研枢（Yanshu）：智能体互联的受治资源协作网络

研枢是面向科研协作的多智能体集群，基于 GB/Z 185《人工智能 智能体互联》与
ACPs（Agent Collaboration Protocols）协议体系构建。项目把 GPU 算力、CPU、评测、
知识库、算法模型等科研资源封装为可被协议发现和委托的智能体，在 supervisor 的调度下
完成文献研究、论文复现、算力共享等任务。

项目重点不只是“连得上”，而是“谁有权用”：通过任务级 PEP 准入、Ed25519 委托令牌、
协作域与发现层可见性过滤，让资源访问可授权、可计量、可撤销、可审计。完整设计、
实验与证据见 [project.md](project.md)。

## 核心能力

- **任务级 PEP 委托授权**：public / 白名单 / 令牌三级准入，策略缺失时 fail-closed。
- **Ed25519 JWS 委托令牌**：iss/sub/aud/act 信任链、jti 防重放，令牌对模型不可见。
- **协作域**：跨机构双签章程、域内 grant、发现层按域过滤与响应脱敏。
- **资源智能体**：GPU / CPU / 评测 / 知识库 / 模型 / HPC 等 partner，按资源风险配置执行底座。
- **真实业务链路**：DeepResearch、AutoResearch、第三方算力 Grant、引用核验。
- **工作流可视化**：将 supervisor run 记录为 cluster-viz Scenario，可回放任务流程。
- **明确失败**：实验被证伪、审查不通过、伙伴拒绝编造结果时如实回传，不补数字。

## 仓库结构

```text
acps_cluster/
  delegation/       PEP、委托令牌、协作域、令牌保管与对账
  deploy/           部署、预检与运维脚本
  tests/            单元与集成测试
  partner_*.py      supervisor 及各资源/研究伙伴
capabilities/       arXiv、评测、知识库、模型等能力实现
docs/               运行手册、事故复盘、接入与验收文档
agents.py           早期单 Agent 示例入口（历史代码）
hooks.py            早期示例 Hook（历史代码）
project.md          项目总览、实验记录与证据索引
requirements.txt    Python 依赖
vendor/             随仓库分发的 acps-sdk wheel
```

## 环境要求

- Python **3.12–3.14**（`wit-framework 2.1.0` 的要求）。
- 可选 Docker：部分 GPU 集成测试会真实启动容器；无 Docker 时相关用例跳过。
- 推荐在 Linux 生产环境运行集群；Windows 可用于本地开发与单元测试。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 先将官方发行的 wit_framework-2.1.0 wheel 放入 vendor/
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
```

说明：

- `wit-framework` 未提供许可证信息，**不随本仓库分发**，请从官方发行渠道获取后放入
  `vendor/`，或预先安装到当前环境。
- `acps-sdk 2.2.0` 随仓库分发，采用 MulanPSL-2.0。
- `.env` 含模型密钥与集群账号，已被 `.gitignore` 排除；不要提交真实值。

## 测试

全量测试（仅建议在本地开发机执行；部分用例读取本地 dev CA 私钥并可能启动 Docker）：

```bash
python -m pytest acps_cluster/tests -q
```

最近一次全量基线：**622 passed，84 subtests passed**（2026-10-20，早于本轮域硬化新增用例）。
本轮新增的域相关回归共 62 项，可用下面的无副作用子集复跑。

生产机可只跑无副作用子集：

```bash
python -m unittest \
  acps_cluster.tests.test_delegation \
  acps_cluster.tests.test_domain \
  acps_cluster.tests.test_domain_hardening \
  acps_cluster.tests.test_visibility \
  acps_cluster.tests.test_p0_security.ReviewGateTest \
  acps_cluster.tests.test_p0_security.TokenFileTest
```

> 域相关离线子集（章程修订链/父 grant 锚点/可见性）当前 62 项：
> `test_domain` 31 + `test_domain_hardening` 16 + `test_visibility` 15。

更多注意事项见 [acps_cluster/tests/README.md](acps_cluster/tests/README.md)。

## 运行

- supervisor 入口：`python acps_cluster/supervisor.py`
- 部分资源 partner 支持本地直连模式，例如：
  `python acps_cluster/partner_gpu.py --local`
- 标准运行需要先配置 registry、CA、discovery、workspace 与 dev/平台身份；具体步骤见
  [docs/runbook-add-new-agent.md](docs/runbook-add-new-agent.md) 和
  [docs/go-live-runbook.md](docs/go-live-runbook.md)。
- 模型路由支持北邮网关主用、MiniMax 保底，见
  [docs/model-routing-bupt-deepseek.md](docs/model-routing-bupt-deepseek.md)。
- 作业编排与任务派发见 [docs/l2-job-pipeline.md](docs/l2-job-pipeline.md) 和
  [docs/task-feeder.md](docs/task-feeder.md)。

## 配置

主要环境变量见 [.env.example](.env.example)，包括模型端点、ACPs registry/CA/discovery
地址、各 partner 端口、可选的可视化目录和第三方算力 Grant 文件路径。

仓库中的示例与 ACS 快照统一使用 RFC 5737 文档地址（`192.0.2.0/24`、
`198.51.100.0/24`、`203.0.113.0/24`）和 `2001:db8::/32`；这些地址不可路由，
部署前请替换为实际地址或域名。部署脚本中的账号和路径同样需按实际环境替换。
退役代码与历史排障脚本不随公开快照分发，仅保留在维护者的本地历史中。

## 安全约定

- 不提交 `.env`、`tokens/`、`**/secrets/`、私钥、运行日志和证书备份；`.gitignore`
  已显式覆盖这些路径。
- 不提交 `wit_framework` wheel（无许可证信息）；本地文件不会被纳入版本控制。
- 部署/事故文档中可能保留历史环境地址，使用前请替换为自己的环境。
- 发现安全问题请优先私下联系项目维护者，不要在公开 issue 中粘贴凭据或可利用细节。

## 第三方组件与许可

| 组件 | 说明 |
|---|---|
| `acps-sdk 2.2.0` | MulanPSL-2.0，随仓库分发于 `vendor/` |
| `wit-framework 2.1.0` | 无许可证元数据，不随仓库分发；需从官方发行渠道获取 |
| ACPs 上游社区 | [AIP-PUB/ACPs-community](https://github.com/AIP-PUB/ACPs-community)，MulanPSL-2.0 |

本项目顶层 `LICENSE` 尚未添加；如需再分发本项目代码，请先补充许可证文件。

## 文档索引

- [project.md](project.md)：系统总览、创新点、验证形态与量化证据
- [docs/resource-agents-authz-acceptance.md](docs/resource-agents-authz-acceptance.md)：资源智能体授权验收
- [docs/third-party-compute.md](docs/third-party-compute.md)：第三方算力 Grant 接入
- [docs/runbook-add-new-agent.md](docs/runbook-add-new-agent.md)：新增 agent 运行手册
- [docs/l2-job-pipeline.md](docs/l2-job-pipeline.md)：L2 作业化交路
- [docs/task-feeder.md](docs/task-feeder.md)：任务派发器
- [docs/go-live-runbook.md](docs/go-live-runbook.md)：上线执行手册
