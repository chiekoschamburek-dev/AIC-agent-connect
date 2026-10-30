# 部署指南（一键部署与其边界）

> 目标：从仓库根用一条命令完成**能自动化的部分**；把不能自动化的部分（平台账号、CA、
> 证书/身份、真实密钥）收敛成一份可勾选的清单，避免“跑到一半才发现缺东西”。

## 1. 一键部署能做什么、不能做什么

| 能自动化 | 不能自动化（必须人工） |
|---|---|
| Python 版本检查、venv、依赖安装 | 梧桐平台账号注册与审核 |
| `.env.production` 模板生成与必填项检查 | 平台签发证书、`ws-*/acps/` 身份（`enrollment.json`、私钥、trust bundle） |
| `wit_framework` / `acps_sdk` wheel 就位检查 | 真实模型 API Key、平台口令 |
| 前端 `cluster-viz` 构建 | 平台侧 ACS 审批/版本管理 |
| 全量测试、guard-api 测试 | 域名、TLS 证书、反向代理（如需公网） |
| systemd 单元渲染（路径不再写死）与安装 | Kafka/AMP 总线的网络开通（如启用） |
| 启停、状态、日志 | — |

“一键”的准确含义：**在身份与密钥已准备好的 Linux 主机上，一条命令完成构建、安装、
启动、自检**；不是“从零账号到上线全部自动”。

## 2. 前置条件

| 项 | 要求 |
|---|---|
| OS | Linux（systemd）；开发机可用 Windows/macOS 跑测试与构建 |
| Python | ≥ 3.12（`python3.12` 或 `ACPS_PYTHON` 指定） |
| Node | ≥ 20（仅前端构建；没有则 `--skip-frontend`） |
| 依赖 wheel | `vendor/wit_framework-*.whl`（官方渠道获取，仓库不分发）、`vendor/acps_sdk-*.whl`（随仓库） |
| 平台身份 | 每个 agent 的 `acps_cluster/ws-<key>/acps/`（证书/私钥/`enrollment.json`） |
| 配置 | `acps_cluster/deploy/.env.production`（由 `.env.example` 生成后填真实值） |
| 权限 | `install-units/start/stop` 需 root；服务单元以 `User=wit` 运行，需先创建 `wit` 用户 |

## 3. 首次部署（一条链路）

```bash
git clone https://github.com/chiekoschamburek-dev/AIC-agent-connect.git yanshu-AIC
cd yanshu-AIC

# 1) 建 venv、装依赖、生成配置模板（首次先 init，才有 .env.production 可检查）
./deploy.sh init

# 2) 填真实配置（平台账号/模型 Key/端口等；不要提交）
$EDITOR acps_cluster/deploy/.env.production

# 3) 环境自检（不会改任何东西）
./deploy.sh preflight

# 4) 构建前端 + 语法编译（可选跑全量测试）
./deploy.sh build --with-tests

# 5) 安装 systemd 单元（渲染 __ACPS_ROOT__ / __ACPS_PY__）
sudo ./deploy.sh install-units

# 6) 启动（默认 supervisor + 13 个伙伴 + workspace + AMP + 定时器）
sudo ./deploy.sh start

# 7) 查看状态
./deploy.sh status
```

### 平台身份与注册（人工）

`deploy.sh` 不会替你注册平台身份。首次上线按
[docs/runbook-add-new-agent.md](runbook-add-new-agent.md) 完成：

1. 在梧桐平台注册 agent，拿到账号与审核；
2. 为每个 agent 铸造/领取证书，落到 `acps_cluster/ws-<key>/acps/`；
3. 把平台已批 ACS 的 `provider.name` 写进 `.env.production` 的 `ACPS_PROVIDER_NAME`；
4. 预热发现缓存：`python acps_cluster/deploy/seed_discovery.py`；
5. 再执行 `deploy.sh start`。

> `preflight` 会检查 `ACPS_PROVIDER_NAME` 与 wheel 是否就位，但不会校验证书内容——
> 证书错误会在服务启动时以明确错误暴露，按 `journalctl -u wit-yanshu@<key>` 排查。

## 4. 子命令

| 命令 | 作用 |
|---|---|
| `./deploy.sh preflight` | 只检查 Python/Node/wheel/配置/systemd，不改文件 |
| `./deploy.sh init [--with-guard-api]` | 建 `.venv`、装依赖、生成 `.env.production`、根 `.env` 符号链接 |
| `./deploy.sh build [--skip-frontend] [--with-tests]` | compileall + 前端构建 + 可选测试 |
| `sudo ./deploy.sh install-units` | 渲染并安装 `acps_cluster/deploy/*.service`（跳过队友的 `gjw-designer.service`） |
| `sudo ./deploy.sh start [--with-guard-api] [--partners "scout writer"]` | `systemctl enable --now` 默认单元 + 伙伴实例 |
| `sudo ./deploy.sh stop [--with-guard-api]` | 反向停止（先伙伴后 supervisor） |
| `./deploy.sh status` | 逐单元状态摘要 |
| `./deploy.sh logs [unit]` | `journalctl -u <unit> -n 200`，默认 supervisor |

## 5. 启动哪些单元

| 单元 | 说明 |
|---|---|
| `wit-yanshu@supervisor` | 常驻 supervisor（收件箱 + AIP 8444） |
| `wit-yanshu@<key>` | 伙伴实例：scout / synthesis / writer / critic / contradiction / frontier / experiment / citation / gpu / cpu / kb |
| `wit-workspace` | 工作区资源实例（8470） |
| `wit-amp-mirror` / `wit-amp-sender` | AMP 心跳镜像与上报 |
| `wit-task-feeder` | 任务派发器（可选） |
| `wit-task-prune.timer` / `wit-ledger-report.timer` | 事件裁剪 / 台账日报 |
| `wit-guard-api`（可选） | guard HTTP API（8778，只读旁路） |

伙伴列表可用 `ACPS_PARTNERS` 或 `--partners` 覆盖；`workspace` 不要放进 `@` 实例（有独立单元）。

## 6. guard 与可视化

- **guard 三层**：PEP（事前准入）、`RunGuardHooks`（事中止损，`ACPS_RUN_GUARD=observe|enforce`）、
  `guard-api` / `guard_adapter`（事中事后取证）。详见 [run-guard.md](run-guard.md)。
- **guard-api**：`./deploy.sh init --with-guard-api` 安装依赖；
  `sudo ./deploy.sh start --with-guard-api` 启动 `wit-guard-api`；
  也可独立运行 `python -m guard_api --db … --port 8778`。
- **cluster-viz**：`./deploy.sh build` 产出 `cluster-viz/dist/`，交给 nginx/静态服务托管；
  回放数据目录由 `ACPS_VIZ_SCENARIO_DIR` 指向，详见
  [../acps_cluster/deploy/VIZ_DEPLOY.md](../acps_cluster/deploy/VIZ_DEPLOY.md)。

## 7. 路径与可移植性

- `deploy.sh` 以**脚本所在目录**为仓库根，可用 `ACPS_ROOT` 覆盖；解释器用 `ACPS_PY` 覆盖。
- systemd 单元里的 `__ACPS_ROOT__` / `__ACPS_PY__` 由 `install-units` 渲染，仓库里不再写死
  `/opt/wit/my-project`。
- 旧的 `acps_cluster/deploy/archive/discovery_diagnostics/` 已归档到
  `acps_cluster/deploy/archive/discovery_diagnostics/`，只作历史排障参考，不属于部署链路。

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| `preflight` 报缺 `wit_framework` | 从官方渠道获取 wheel 放入 `vendor/`；仓库因许可证不分发 |
| `init` 的 pip 安装失败 | 确认 Python ≥3.12；离线机可先 `pip download` 再 `--no-index` |
| `start` 后单元起不来 | `./deploy.sh logs wit-yanshu@supervisor`；常见为 `.env.production` 缺值或证书不匹配 |
| `wit-yanshu@<key>` 反复重启 | `TimeoutStartSec=400` 内启动抖动属正常；持续失败看 `ws-<key>/logs/` |
| 前端 404 | 确认 `cluster-viz/dist` 已被静态服务指向，且 `ACPS_VIZ_SCENARIO_DIR` 可写 |
| guard-api 401 | 检查 `ACPS_GUARD_TOKEN` 与 `Authorization: Bearer` 一致（Bearer 修复已含） |

## 9. 验证清单

```bash
./deploy.sh preflight
python run_all_tests.py                 # 632 passed / 182 skipped（干净克隆基线）
python -m pytest guard-api/tests -q     # 20 passed（装了 fastapi/uvicorn/httpx 时）
curl -s http://127.0.0.1:8778/health   # 启用 guard-api 时
./deploy.sh status
```
