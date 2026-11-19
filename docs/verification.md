# 可核验事实（对外数字的唯一取值来源）

> 本页记录**本仓库当前提交**下，对外材料引用的每一个数字、它的定义与复跑命令。
> 评审若对任何数字有疑问，按本页命令在本机复跑即可；数字随提交变化时，本页一并更新。
>
> 口径由脚本固化，不靠手抄：[`acps_cluster/tools/repo_facts.py`](../acps_cluster/tools/repo_facts.py)。

## 1. 复跑命令

```bash
cd yanshu-AIC

# ① 仓库规模与代码质量（受控文件数 / 代码行数 / docstring 完整率 / 语言占比）
python acps_cluster/tools/repo_facts.py

# ② 全量测试（干净克隆：缺 dev 私钥的用例会 skip，不是 fail）
python -m pytest acps_cluster/tests acps_cluster/guard/tests guard-api/tests -q

# ③ 四组对照实验（PEP / 协作域 / Workspace / 双层 Guard），自包含、离线、不依赖仓库私钥
python experiments/ablation/run_all.py
```

## 2. 本提交的实测结果

| 指标 | 数值 | 定义与口径 |
|---|---|---|
| 受控文件 | **678** | `git ls-files` 行数，即 `git clone` 后可见的文件；`ws-*` 运行时目录、私钥、数据集不入库、不计入 |
| 全量测试 | **770 passed / 0 failed** | 干净克隆（无 `ws-*`）在 Windows 11 + Python 3.13 实测；181 项 skip 的原因是缺 dev CA 私钥、平台快照与 Linux symlink 语义，**均为环境依赖，不是失败**。换到 Linux 时 symlink 相关用例会从 skip 转为 pass（通过数略增、失败数仍为 0） |
| 核心模块 docstring | **73 / 73** | `acps_cluster/{agents,core,runtime,delegation}` + `capabilities` 下的 `.py`（不含 `__pycache__`） |
| 长公开函数 docstring | **203 / 203** | 同上范围内、不以 `_` 开头、跨行 ≥20 行的函数；短小访问器不计入 |
| 代码语言占比 | Python **91.2%** | 按代码文件字节数（`.py/.sh/.ts/.tsx/.js/.vue/.css/.html` 等），不含 Markdown/JSON/图片/PDF/wheel。GitHub linguist 另会剔除 vendored 路径，故仓库首页显示值与之有 ±1pt 差异（截图时点为 91.6%） |
| 生产 / 测试 Python | 62.2k 行 / 271 文件；24.3k 行 / 107 文件 | 物理行数（含空行与注释） |
| 名册成员 | **15** | `acps_cluster/core/cluster_roster.py` 的 `ROSTER`：1 个编排器 + 14 个伙伴 |
| 资源实例 | **4 类** | GPU / CPU / KB / Workspace 四个资源 agent |
| 对照实验 | 见 §4 | `experiments/ablation/out/*.json` 为原始数据 |

> 平台侧数据（注册审核、调用量、排行榜位次）不在仓库内，属于平台后台可核查证据；
> 本页只覆盖"仓库内可自行复算"的数字。

## 3. 主张 → 代码 → 测试 映射

对外材料里的每项机制，都能落到具体文件与用例：

| 主张 | 代码位置 | 测试 / 证据 |
|---|---|---|
| 三级准入 L0 公开 / L1 白名单 / L2 委托令牌 | `delegation/pep.py`（`evaluate`、`PepConfig`） | `tests/test_delegation.py`、`test_p0_security.py`；实验① 12 类非法调用 0/12 → 12/12 |
| 委托令牌：签名 / 证书链 / 受众 / 时效 / 权限范围 | `delegation/token.py`（`issue_token`/`verify_token`） | `tests/test_delegation.py`；实验① 逐类案例见 `out/exp1_pep.json` |
| 防重放与单次消费（jti） | `delegation/pep.py`（`JtiRegistry`） | `tests/test_delegation.py`（重放用例） |
| 主体一致性（`act.aic == 对端 AIC`） | `delegation/pep.py` | `tests/test_delegation.py`（act 不符 → `act-peer-mismatch`） |
| 凭证隔离：令牌不进模型上下文 | `delegation/client.py`（`DelegationTokenScrubHooks`、host-only `local_context`/令牌文件）、`delegation/vault.py`（jti 句柄） | `tests/test_delegation.py`、`test_p0_security.py` |
| 裁决可审计：规则码留痕、对外统一文案 | `delegation/audit.py`（JSONL，不记完整令牌） | `tests/test_delegation.py`（审计字段断言） |
| 双签章程与修订连续性 | `delegation/domain_manifest.py`（`MIN_SIGNATURES=2`、`verify_revision_continuity`） | `tests/test_domain.py`、`test_entry_grant.py`；实验② |
| 冻结 / 撤销 / 版本校验（运行中热变更） | `delegation/domain_manifest.py`、`delegation/pep.py`（验签前查域状态） | `tests/test_domain.py`；实验② 8/8（含冻结、移除成员、配额用尽、到期、域外盗用、非授权签发者） |
| 配额：调用次数 / 签发侧速率 | `delegation/quota.py`（`grant_uses`、`grant_issues`，sqlite 持久化） | `tests/test_domain.py`；实验② 配额用尽路径 |
| 配额：资源量（GPU 卡时） | 章程 `resourceQuota` → `delegation/domain_manifest.py`；上下文传递与执行 `delegation/resource_budget.py`；记账 `delegation/quota.py`（`resource_uses`）；运行时检查 `agents/partner_gpu.py`（`gpu.run` 开跑前查余量、跑完按 `duration_s` 记账） | `tests/test_resource_budget.py`（17 项：解析 / 账本 / 上下文 / 用尽拒绝 / 跑后记账 / PEP 接线） |
| Workspace 四分区（repo 只读 / products 追加 / scratch 前缀强制 / shared 乐观锁） | `agents/partner_workspace.py`（`ws_write`、`_manifest_append`） | `tests/test_workspace_partner.py`；实验③ 越界写入 0/4 → 4/4、身份伪造 0/4 → 4/4、归因 0/8 → 8/8 |
| 证据链：令牌句柄 → PEP 审计 → 沙盒日志 → 产物哈希 | `delegation/vault.py`、`delegation/audit.py`、`agents/partner_experiment.py`（`artifactSha256`）、`agents/partner_workspace.py`（`manifest.jsonl`） | `tests/test_workspace_partner.py`（`prev_sha256` 链）；实验③ 归因字段齐全 8/8 |
| 三维通信风险检测（存储 / 时序 / 行为） | `acps_cluster/guard/covert_event_guard.py` | `acps_cluster/guard/tests/`、`guard-api/tests/`；实验④b 异常检出 13/16、误报 6/17 且全部 LOW |
| 旁路适配与告警发布（只读、不自动阻断） | `acps_cluster/guard_adapter.py` | `tests/test_guard_adapter.py` |
| 运行守卫 RunGuardHooks（7 条规则，observe/enforce） | `agents/supervisor.py`（`RunGuardHooks`：wall-clock / no-activity / progress-stall / error-streak / repeat-loop / step-budget / partner-not-finishing） | `tests/test_run_guard.py`；实验④b 止损 83.6%–95.9% |
| 双向互联：Supervisor 委派 + Partner 主动上报 | `agents/reporting.py`（`AutoForwardRequestHooks`）、`acps_cluster/bidirectional/` | `tests/test_bidirectional.py`、`test_reporting.py` |
| 名册 15 个成员与 ACS 投影 | `core/cluster_roster.py`、`deploy/make_acs.py` | `tests/test_local_acs.py`、`test_cluster_logic.py` |
| 一键部署 8 个子命令 + systemd 单元 | `deploy.sh`、`acps_cluster/deploy/*.service` | `docs/deployment.md` |
| 四组对照实验可复跑 | `experiments/ablation/*.py` | `python experiments/ablation/run_all.py`，原始数据 `out/*.json` |

## 4. 对照实验摘要（最近一次复跑）

| 组 | 变量 | 结果 |
|---|---|---|
| ① PEP 准入 | `cfg.enabled`（官方默认 accept / 研枢裁决） | 非法调用拦截 0/12 → **12/12**；合法调用 3/3 → **3/3**；裁决延迟 p50≈8.2 ms、p99≈14.2 ms |
| ② 持续协作域 | 是否启用治理层（8 项热变更场景） | **8/8** 符合预期；官方默认对全部请求直接放行 |
| ③ Workspace | 裸 `write_text` / 受控工作区写入 | 越界拒绝 0/4 → **4/4**；身份伪造拦截 0/4 → **4/4**；归因字段 0/8 → **8/8** |
| ④ 双层 Guard | 检测开/关；守卫 observe/enforce | 异常检出 **13/16**（漏报 3/16 全为三维同时规避）；正常样本误报 6/17 且**全部 LOW**；止损节省 **83.6%–95.9%** |

方法、逐场景明细与"为什么这样设计"见
[`experiments/ablation/实验报告-三机制对照-真实数字.md`](../experiments/ablation/实验报告-三机制对照-真实数字.md)。

## 5. 口径说明

- 本页所有数字都是**本提交、本机**实测值；数字随提交变化时本页同步更新，历史值以提交历史为准。
- 测试数受环境影响的只有 **skip**（缺 dev 私钥 / 平台快照 / Linux symlink 语义），
  失败数为 0 才是验收线；换机器复跑时通过数可能略有增减，属正常。
- 语言占比存在算法差异：本脚本按代码文件字节数、不做 vendored 剔除；GitHub linguist
  会剔除 vendored/生成文件，所以仓库首页的显示值与本页数值有 ±1pt 差异。
- 平台侧数据（15 节点审核上线、调用量、排行榜位次）来自平台后台，仓库内不含原始记录；
  该类数字的证据是平台截图与后台查询，不在本页的"可复算"范围内。
- 机制的**设计与实现取舍**（为什么这样写、替代方案、已知代价）在各设计文档里：
  [`docs/PEP_design.md`](PEP_design.md)、[`docs/RFC_delegation_domain_draft.md`](RFC_delegation_domain_draft.md)、
  [`docs/PEP-result.md`](PEP-result.md)、[`docs/workspace-partner.md`](workspace-partner.md)、
  [`docs/run-guard.md`](run-guard.md)。本页只负责"数字能不能复算"。