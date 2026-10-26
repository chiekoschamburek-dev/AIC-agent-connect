# 工作区伙伴访问：服务器发布工具包

> 2026-10-26 那次发布（研究伙伴读写任务工作区）用到的脚本，整理成可复用的一页工具。
> 发布记录与验收证据见 [docs/server-rollout-2026-10-26.md](../../../docs/server-rollout-2026-10-26.md)。

## 适用场景

- 把本地 `my-project` 的一批文件推到服务器 `/opt/wit/my-project`（文件级部署，不走 git）；
- 停服务 → 备份 → 覆盖 → 语法检查 → 并行重启 → 灰度验证 → E2E → 审计；
- 需要"备份可回滚、失败即停、不静默"的发布（10-23 计量钩子事故的教训）。

## 先决条件

- 本机可 `ssh root@<host>`（默认 host 从 `ACPS_DEPLOY_HOST` 读，缺省 `8.160.176.215`）；
- 服务器上有 `/opt/wit/venv/bin/python`、项目在 `/opt/wit/my-project`、服务运行在 `wit` 用户；
- **执行位置**：除 `check_docs_links.py`（本机跑）外，`.sh` 脚本都在**服务器上**执行
  （`scp` 到 `/tmp` 后 `ssh bash /tmp/xxx.sh`）；可用 `PROJECT_DIR` / `PY` / `WIT_USER`
  环境变量覆盖默认路径；
- 服务器上的 ACS/证书/`enrollment.json` 只在服务器原地，**任何脚本都不覆盖**（见下方"红线"）。

## 脚本目录

| 脚本 | 作用 | 只读? |
|---|---|---|
| `inventory.sh` | 盘点服务器：HEAD、Python、待改文件 sha256、服务状态、测试文件 | ✅ |
| `backup_deploy.sh` | 停指定 unit → 备份到 `/opt/wit/predeploy-<name>-<ts>/` → 覆盖 → 语法检查 → 打印哈希 | ❌ |
| `restart_units.sh` | `--no-block` 并行重启 + 轮询 active + ready 行 + ACS/指纹错误扫描 | ❌ |
| `audit_deploy.sh` | 部署一致性（对比本地 sha256）、日志 JWS 泄漏、bundle 权限、镜像残留、服务/心跳 | ✅ |
| `e2e_workspace.sh` | 走 supervisor 收件箱跑真实任务（建工作区 → 伙伴读/写 → 验收），输出 trace 摘要 + manifest | ❌（会派任务） |
| `cleanup_workspaces.sh` | 按 task_id 精确回收测试工作区 + 对应 bundle 令牌 + 收件箱归档 | ❌（破坏性） |
| `run_server_tests.sh` | 服务器上跑本次相关的 unittest 子集（**注意身份差异，见下**） | ✅ |
| `parse_trace.py` | run-trace 事件摘要（delegate/bundle/mirror/run_end） | ✅ |
| `check_docs_links.py` | 本地文档内部链接检查（发布前顺手跑） | ✅ |

## 一次发布的完整流程

```bash
# 0) 本地：全量测试 + lint + 文档链接
python run_all_tests.py
python -m ruff check acps_cluster
python acps_cluster/deploy/workspace-rollout/check_docs_links.py

# 1) 打包（只带清单里的文件，按 my-project 相对路径）
tar czf /tmp/deploy.tgz -C stage acps_cluster

# 2) 服务器盘点（确认没有"服务器独有补丁"会被覆盖）
bash inventory.sh
#   也可先取服务器文件快照与本地逐个 diff；inventory.sh 打印的 sha256 足够比对

# 3) 备份 + 覆盖 + 语法检查（失败即停，不会启服务）
bash backup_deploy.sh --source /tmp/deploy-in --files deploy_files.txt \
  --name ws-partner --stop wit-yanshu@supervisor.service

# 4) 并行重启（随机抖动 0-300s，等 active 后打印 ready 行）
bash restart_units.sh --units "wit-workspace.service wit-yanshu@scout.service ..."

# 5) 审计（给本地生成的 sha256 清单可逐字节比对）
bash audit_deploy.sh --expected /tmp/local_sha256.txt

# 6) E2E（收件箱派任务；默认 2 步：workspace 写 + 伙伴读回）
bash e2e_workspace.sh --task-id e2e-$(date +%H%M) --partner scout

# 7) 清理联调产物（明确列出 task_id 才删）
bash cleanup_workspaces.sh --task-ids "e2e-2100 e2e-2105"
```

## 命令细节

### `backup_deploy.sh`

```text
--source DIR     解包后的目录（里面按 my-project 相对路径布局）
--files FILE     文件清单（默认同目录 deploy_files.txt）
--name NAME      备份目录名后缀（默认 workspace-rollout）
--stop "UNIT..." 发布前停掉的 unit（空格分隔；可不给）
--no-compile     跳过语法检查
```

行为：
- 备份 `/opt/wit/predeploy-<name>-<ts>/files.tar.gz`（只含清单里**已存在**的文件），
  并把服务器上原本不存在的文件写进 `absent.txt`（新文件）；
- `cp -a` 覆盖后 `chown -R wit:wit`；
- 用 `ast.parse` 做语法检查（不写 `.pyc`，避免 root 污染 `__pycache__`）；
- 任一步失败立即退出（`set -e`），服务保持停着，等人工处理。

### `restart_units.sh`

```text
--units "u1 u2 ..."   必填
--timeout 600         等 active 的总秒数
```

`systemctl restart --no-block` 让随机抖动并行发生（否则逐个等 0-300s 会拖很久）；
随后轮询 `is-active`，最后打印每个 unit 的 `已上线/常驻已上线` 行，
以及 `journalctl` 里的 ACS/指纹/Traceback 扫描结果。

### `audit_deploy.sh`

```text
--expected FILE   本地 `sha256sum` 清单（"hash  path" 两列，路径含 acps_cluster/ 前缀）
--host HOST       默认 $ACPS_DEPLOY_HOST 或 8.160.176.215
```

检查项：文件哈希一致性、run-trace 里的 JWS 原文（应为 0）、bundle 令牌文件权限（600）、
`products/supervisor` 历史残留、服务与心跳新鲜度。任一处不符返回非 0。

### `e2e_workspace.sh`

```text
--task-id ID       必填（工作区作用域；同时用作收件箱文件名）
--partner KEY      默认 scout（名册 key）
--marker TEXT      默认 "联调标记 <task-id>"
--timeout 600      等 run_end 的秒数
```

它派的是**两步**任务（避开 supervisor 8 分钟同步预算）：
1. 委派 workspace `ws.write` 写 `shared/<task-id>.md`；
2. 委派 partner 用 `ws_list/ws_read/ws_write` 读回并写 `products/<partner>/...`。

结果不写 outbox（收件箱 run 没有 `aip_submit`），脚本以 run-trace 的 `run_end` 为完成信号，
然后打印 `delegate_direct` / `ws_bundle_issued` / manifest 行，供人核对。

### `cleanup_workspaces.sh`

```text
--task-ids "id1 id2 ..."   必填（只删列出的任务）
--with-bundles 1           默认 1：同时删 bundle JSON 里 taskId 命中的令牌文件
```

以 `wit` 用户跑 `partner_workspace.py --gc-task`，删除项会落 `.gc-log.jsonl` 墓碑；
**不会**碰 `feed-*` 等真实任务。

## 红线（脚本刻意不做的事）

- 不覆盖 `ws-*/acps/`（ACS 终稿、证书、私钥、enrollment）——这些只在服务器原地；
- 不改 workspace 的 ACS/能力声明（会触发 `acs_fingerprint_changed_after_submission`）；
- 不做"只按 issuer 放行"之类放宽 PEP 的改动；
- 不把 token 写进日志/模型上下文（E2E 脚本只打印事件摘要，不打印 JWS）。

## 已知坑

| 坑 | 说明 |
|---|---|
| 服务器单测不可信 | 服务器是平台身份，本地测试按 dev AIC/CA 写断言（`PepConfigEnvTest` 等会假失败）。验证以"灰度重启 + 真实 E2E"为准；`run_server_tests.sh` 只作冒烟，失败先看是不是身份断言 |
| 服务重启有 0-300s 随机抖动 | unit 的 `ExecStartPre` 里 `sleep random(0,300)`；必须并行重启，别串行等 |
| 收件箱 run 没有 `aip_submit` | 结果走 `final_text`，不会写 outbox；等 `run_end` 事件而不是等 outbox 文件 |
| 同步预算是 8 分钟 | supervisor 单 run 超过 `ACPS_SUPERVISOR_SYNC_BUDGET_MINUTES` 会被拦截，E2E 要拆成少步骤 |
| 参数引号 | 远程命令带引号/循环时，一律写成 `.sh` scp 过去执行，别在 `ssh "..."` 里拼 |
