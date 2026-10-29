# Workspace Partner：任务工作区管理员 / 存储资源实例（平台注册，2026-10-25）

> 解决的问题：partner 之间只传“上下文里的文字”（≤26KB 量级），涉及 GitHub 仓库、
> 数据集、大产物的任务走不通。工作区管理员把**存储**做成资源 agent
> （kb=数据、gpu=沙盒、eval=保险柜、workspace=工作区），伙伴间文件经协议交换，
> **文件不进上下文，只有路径和摘要进上下文**。
>
> 平台身份：`1.2.156.3088.1.BUPT.VTF97R.N9M0PW.ZB1CII.0B3X`（v1.1.0，
> `certificate.altNames.ip=8.160.176.215`），端点
> `https://8.160.176.215:8470/rpc`（mTLS）。

## 能力（全部非公开，凭 supervisor 委派令牌调用）

| 能力 | 作用 |
|---|---|
| `ws.create(task_id)` | 开 `ws-workspace/workspaces/<task_id>/`，返回路径与配额 |
| `ws.write(task_id, path, content, encoding=text\|base64)` | 写文件（配额内，原子写、唯一临时名） |
| `ws.read(task_id, path, max_bytes)` | 读文件，默认 ≤64KB；调用方上限 `ACPS_WS_MAX_READ_BYTES`（默认 1MB） |
| `ws.list(task_id, path, depth)` | 目录树（posix 相对路径/类型/字节，≤200 项，深度上限 8） |
| `ws.clone_repo(task_id, url, branch)` | 浅克隆公开 https 仓库到 `repo/<name>/`，返回树+README 摘要 |
| `ws.gc(task_id)` | **按任务**回收；批量按天回收只允许运维 CLI（见运维） |

## 安全边界

- task_id 白名单 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`；相对路径 resolve 后必须落在
  任务目录内（绝对路径显式拒绝，不做静默剥前缀）；
- 每任务配额 `ACPS_WS_TASK_QUOTA_MB`（默认 512MB），写前与克隆后复核；symlink 不计入用量；
- clone：仅 https、禁凭据、`GIT_TERMINAL_PROMPT=0`、超时 `ACPS_WS_CLONE_TIMEOUT`（默认 120s）、
  克隆后复核；**默认拒绝回环/内网/保留地址**（SSRF 防线），内网镜像用
  `ACPS_WS_CLONE_DOMAINS` 点名或显式 `ACPS_WS_CLONE_ALLOW_PRIVATE=1` / `..._ALLOW_LOOPBACK=1`；
- README 读取拒绝 symlink/逃逸；`ws.list` 不跟随 symlink（列出为 `symlink`，不读目标大小）；
- PEP：`allow_unscoped_public_entry=False`，ws.* 全部非公开——supervisor 的
  受保护伙伴表（`_protected_partners` 的 `"workspace"` 条目）自动附令牌；
  注册模式**必须** `ACPS_PEP_ENABLED=1`，否则进程直接拒绝启动；
- 任务作用域：令牌带 `task_id` claim，六能力逐次比对请求 `task_id`，缺失/不一致 fail-closed；
- 批量 `ws.gc(older_than_days)` 无法用单任务令牌安全表达，已从委派能力移除；运维用本机 CLI。

## 身份与部署（平台注册态）

`wit-workspace.service` 直接运行 `partner_workspace.py`（**不带 `--local`**），
用 `ACPS_WORKSPACE_USERNAME/PASSWORD` 登录平台认领身份；`--local` 只用于本地开发。

共享 env（`deploy/.env.production`）至少需要：

```bash
ACPS_WORKSPACE_USERNAME='<平台账号>'
ACPS_WORKSPACE_PASSWORD='<平台口令>'
ACPS_PEP_ENABLED=1
ACPS_PEP_LEADER_AICS=<supervisor AIC>     # 允许 supervisor 签发委托令牌
```

workspace 专属项放 `deploy/.env.workspace`（0600，避免污染其他伙伴的
`acpsConfig` 指纹；`wit-workspace.service` 会额外加载它）：

```bash
ACPS_PROVIDER_NAME=wit-research-lab       # 必须与平台已批 ACS 的 provider.name 一致
ACPS_SERVE_HOST=0.0.0.0
ACPS_WORKSPACE_PORT=8470
ACPS_WS_TASK_QUOTA_MB=512
ACPS_WS_CLONE_TIMEOUT=120
ACPS_WS_MAX_READ_BYTES=1048576
```

### 从本地 dev 工作区切到平台身份

本地 `ws-workspace/acps/enrollment.json` 是 dev CA 身份（`review={}`），不要直接
拷到服务器；按下面两步重建身份：

```bash
cd /opt/wit/my-project/acps_cluster
set -a; . deploy/.env.production; set +a

# 1) 平台 CA 按目标 AIC 签双证书（在 ws-workspace-idswitch 副本里）
#    注意 env 放在 sudo 之后：sudo 默认会清掉调用方的变量，
#    `VAR=... sudo -u wit ...` 会把 CREDS_PREFIX/TARGET_AIC 静默丢掉。
sudo -u wit env \
  CREDS_PREFIX=WORKSPACE \
  TARGET_AIC=1.2.156.3088.1.BUPT.VTF97R.N9M0PW.ZB1CII.0B3X \
  PUBLIC_ENDPOINT=https://8.160.176.215:8470/rpc \
  /opt/wit/venv/bin/python deploy/archive/discovery_diagnostics/switch_step2_try_ca_issue.py

# 2) 写回身份：review.metadata.active、authoritativeAcs、registrationId、
#    acpsConfig 与 acsFingerprint 全部按平台记录重算
sudo -u wit /opt/wit/venv/bin/python deploy/archive/discovery_diagnostics/switch_step4_apply_identity.py \
  --ws ws-workspace --aic 1.2.156.3088.1.BUPT.VTF97R.N9M0PW.ZB1CII.0B3X \
  --creds-prefix WORKSPACE --port 8470
```

然后搬证书材料（**这一步漏了就是“服务自称新 AIC、证书却是旧的”**）：

```bash
cp -p ws-workspace-idswitch/acps/client/certificate.pem ws-workspace/acps/client/
cp -p ws-workspace-idswitch/acps/client/private-key.pem  ws-workspace/acps/client/
cp -p ws-workspace-idswitch/acps/server/certificate.pem ws-workspace/acps/server/
cp -p ws-workspace-idswitch/acps/server/private-key.pem  ws-workspace/acps/server/
cp -p ws-workspace-idswitch/acps/trust-bundle.pem        ws-workspace/acps/
chown wit:wit ws-workspace/acps/client/*.pem ws-workspace/acps/server/*.pem \
  ws-workspace/acps/trust-bundle.pem
```

装服务并启动：

```bash
install -m 644 deploy/wit-workspace.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wit-workspace
journalctl -u wit-workspace -n 30 --no-follow   # 期待：已上线 AIC=…N9M0PW…
```

`wit-workspace.service` 带 0–300s 抖动和 300s 重试间隔：平台同账号单会话，
workspace 重启不应把 scout/supervisor 踢下线。

### 自检

```bash
sudo -u wit /opt/wit/venv/bin/python deploy/check_new_agent.py --ws ws-workspace
# 部署前必须全绿（workspace 已加入两个校验器）
python deploy/archive/discovery_diagnostics/verify_overlay_matches_registered.py
python deploy/archive/discovery_diagnostics/verify_projection_stability.py
```

## ACS 投影对齐（重要）

平台已批记录是控制台按手写稿批准的：skill `name` 是中文、没有 `inputModes`、
`certificate` 为空。代码投影必须与它逐字一致，否则重启会触发
`acs_fingerprint_changed_after_submission`。因此：

- 覆盖表由 `_platform_snapshot/acs.live.workspace.json` 生成，**不要手改**
  `registered_acs_overlay.json`；
- `WorkspaceRuntime` 只对 ACS 投影做三件事：去掉 `certificate`、显式
  `defaultInputModes=["application/json"]`、六个 skill 置空 `input_modes`；
- 平台 ACS 里的 `ws.gc` 描述仍写着“按天批量回收”，实际能力面只允许 `task_id`；
  下次抬版本重新注册时同步改文案。
- **IP SAN 已修复**：平台 CA 按 ATR 规范从 ACS 的 `certificate.altNames` 构造 SAN，
  不读 CSR。旧 ABKIJE（1.0.0）记录 `certificate: {}` 已弃用；新记录 N9M0PW（1.1.0）
  写入 `certificate.altNames.ip=["8.160.176.215"]`，领证会包含 IP SAN。
  CSR SAN 补丁对平台 CA 无效，保留仅作通用兼容。详见
  [issue-wit-2.1.0-ip-endpoint-san.md](issue-wit-2.1.0-ip-endpoint-san.md)。

## 编排接入

- 名册 `cluster_roster.ROSTER` 的 `workspace` 条目（8470）——supervisor 的
  `render_roster_text` 会自动把它（含六能力）注入编排上下文；
- 涉及仓库/大文件的任务，编排第一步委派 `ws.create`（+`ws.clone_repo`），
  后续委派文本携带 `task_id` 与相关路径；
  **一次委派只授权一个 ws.\* 能力**（supervisor 按 request 文本里最先出现的受保护
  能力签一枚令牌），所以创建与克隆要拆成两次委派；
- GPU 沙盒要消费工作区文件时，经既有 `gpu.data.fetch` 通道从工作区暂存（不跨机挂载）。

## 研究伙伴读写工作区（2026-10-26 新增，无协作域）

partner 以前拿不到工作区令牌，也没有客户端工具，clone 下来的仓库对它们不可见。
现在走"supervisor 代签、partner 持令牌直连"：

- **supervisor 侧**：委派研究伙伴时（`acps_delegate` 与 `acps_delegate_public` 两条
  路径都算），`_ws_bundle_parts()` 为该伙伴另签 `ws.list` / `ws.read` / `ws.write`
  三枚令牌（`delegate_aic=<partner>`、`target_aic=<workspace>`、`task_id` claim、
  TTL ≤ `ACPS_WS_PARTNER_TOKEN_TTL`，默认 600s），作为 DataItem 随任务下发。
  令牌原文不进任何模型上下文；某个伙伴未开放或签发失败时静默跳过（fail-open），
  它那边的 ws 工具会返回 `awaiting`。
- **partner 侧**：`delegation/client.py` 的 `DelegationTokenScrubHooks` 把 ws.\* 令牌
  按 skillId 收进 **bundle**（ContextVar + host 令牌文件），并给模型留一条脱敏标记
  （"本任务另持有 N 枚工作区令牌"）。`partner_workspace_client.py` 暴露三个模型工具
  `ws_list` / `ws_read` / `ws_write`：task_id 固定取自令牌 claim（模型改不了作用域），
  令牌从 bundle 取，调用走 `aip_delegate` 直连 workspace。
- **开关**：`ACPS_WS_PARTNER_ACCESS`（逗号分隔名册 key）。默认七个研究伙伴
  （scout/experiment/synthesis/writer/critic/contradiction/frontier）全开；置空串即关闭。
  supervisor 与 partner 两侧读同一份口径——不一致时只会"有令牌没工具"（工具返回
  awaiting），不会越权。
- **伙伴侧对端发现**：伙伴启动时 `cluster_common.seed_workspace_peer()` 会把 workspace
  的 ACS 写进自己的 `acps/discovered/`（伙伴的发现缓存本来是空的，不预热就报
  `Partner has no discovered Direct JSON-RPC endpoint`）；无需平台发现接口、无需网络。
- **task_id 语义**：协议层给伙伴的 `taskId`（`task-<uuid>`）是 AIP 任务编号，**不是**
  工作区作用域；工作区 `task_id` 只取委派文本里的 `task_id=<值>`（并核对令牌 claim）。
- **PEP 取令牌按 audience**：一次委派会同时带任务令牌（aud=partner）与三枚工作区令牌
  （aud=workspace），`token.extract_token_for_audience()` 只取发给本机的那枚，外发的
  ws 令牌忽略；`act == peer` 与 task_id 校验不变。
- **ACS 中性**：三个 ws 客户端工具 `acps_skill=False` + `metadata.internal_tool`，
  `discovery_metadata` 的注册文本覆盖与 `SkillScopedToolsHooks` 的非公开能力收窄都会
  跳过它们，workspace 的 ACS 指纹与版本不变。

## 四分区布局与产物归档（2026-10-26 定案，第四创新点落地面）

每任务工作区内部分四个语义分区——"归档审计"与"文件总线"各自有干净的安全模型：

```
workspaces/<task_id>/
├── manifest.jsonl        台账：每变更一行（actor/action/path/前后 sha256），flock 跨进程安全
├── repo/                 只读参考区（仅 ws.clone_repo 服务端写入）
├── products/<actor>/     产物归档：追加式，服务端按 <actor>/<seq>-<名> 版本化，不可覆盖
├── scratch/<actor>/      各调用方草稿领地：服务端强制前缀=令牌 act.aic 派生身份
└── shared/               文件总线：委派输出契约指定路径，if_match 乐观锁防互踩
```

- **写入规则按分区而定**（服务端强制，非纸面约定）：ws.write 只接受四分区路径；
  `..` 走廊落出分区即 zone-escape 拒绝；调用方身份从委派令牌 act.aic 派生
  （名册 key 优先，AIC 尾段兜底）——scratch 写别人领地会被物理归位到自己领地。
- **if_match 乐观锁**（shared/）：传上次读到的 sha256，不符返回 conflict+当前 hash；
  传 `absent` 表示预期不存在；ws.read 返回全文件 sha256（读侧凭据）。
- **产物归档不再有 supervisor 旁路**（2026-10-26 改）：此前 supervisor 收尾用
  `partner_workspace.archive_products()` 本地直写 `products/`，绕过了 workspace 服务、
  也绕过了令牌校验（产物只落在 supervisor 机器的目录里，workspace DB 看不到）。
  该旁路与 `ACPS_WS_MIRROR` 开关已一并删除；现在 `products/` 只由**持令牌的
  `ws.write`** 写入——partner 用 `ws_write` 写自己的产物，编排需要归档 leader 产物时
  显式委派一次 `ws.write`（actor 由令牌 `act.aic` 派生）。
- **保留与回收**：ws.gc 按任务回收，墓碑落根级 `.gc-log.jsonl`（台账随目录消失，
  回收动作本身幸存可审计）。

## 任务作用域

ws.* 的授权粒度 = “持令牌”且“令牌 task_id claim 与请求一致”：

- leader 委派 workspace 时，`ResourceDispatchAuthHooks` 从委派参数或请求文本
  提取 `task_id`（约定写法 `task_id=<id>`），写入令牌 claim 后再附令牌；
- 委派研究伙伴时 supervisor 签的三枚 ws 令牌同样带 `task_id` claim；partner 的
  `ws_list/ws_read/ws_write` 工具签名里没有 task_id——作用域只从 claim 取，
  模型改不了；
- workspace 六能力收到请求时比对令牌 claim 与请求 task_id，不一致/缺失即拒
  （`scope-mismatch` / `scope-missing` / `scope-required`），fail-closed；
- 本机直调（无委派）同样拒绝——工作区访问只存在委派这一条受控通道。

## 运维

```bash
# 环境变量
ACPS_WS_ROOT / ACPS_WS_TASK_QUOTA_MB / ACPS_WS_CLONE_TIMEOUT / ACPS_WS_MAX_READ_BYTES
ACPS_WS_CLONE_DOMAINS / ACPS_WS_CLONE_ALLOW_PRIVATE / ACPS_WS_CLONE_ALLOW_LOOPBACK

# 研究伙伴工作区访问（supervisor 与 partner 两侧同一口径）
ACPS_WS_PARTNER_ACCESS=scout,experiment,synthesis,writer,critic,contradiction,frontier
ACPS_WS_PARTNER_TOKEN_TTL=600          # 秒，上限 600
ACPS_WS_CLIENT_TIMEOUT=180             # partner 侧 ws 调用等待上限
ACPS_WS_CLIENT_MAX_READ_BYTES=262144   # partner 侧 ws_read 上限
ACPS_WS_CLIENT_MAX_WRITE_BYTES=262144  # partner 侧 ws_write 单次上限
ACPS_WS_PARTNER_MIN_STEPS=16           # 带 bundle 的入站任务步数下限

# 按任务精确回收（也可委派 ws.gc(task_id)）
python partner_workspace.py --gc-task task-20261001
# 批量按天回收：只允许在本机以服务账号执行，不经委派能力
python partner_workspace.py --gc-older-than-days 7
```

测试：`tests/test_workspace_partner.py`（投影/逃逸/配额/截断/克隆闸门/SSRF/
symlink/超时/批量 GC 闸门/名册接线）、`tests/test_switch_workspace.py`（换身份 review/指纹）、
`tests/test_partner_workspace_client.py`（partner 侧客户端/令牌缺失/过期/超限）、
`tests/test_partner_workspace_acs.py`（开关不改变 ACS 指纹）。
