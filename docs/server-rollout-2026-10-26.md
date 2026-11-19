# 服务器发布记录：研究伙伴读写任务工作区（2026-10-26）

> 对应设计/交接：[handoff-workspace-partner-access.md](handoff-workspace-partner-access.md)。
> 本次发布把"研究伙伴能读写任务工作区"从代码推到现网，并顺手修掉联调暴露的
> 4 个真实缺陷。**workspace 的 PEP 规则与 ACS 投影未改**（无指纹风险）。
>
> **2026-11-19 回注**：下表是发布当时的文件路径（包化重构前）。现在同一批代码位于
> `acps_cluster/agents/`、`acps_cluster/delegation/`、`acps_cluster/core/` 下，
> 例如 `supervisor.py → agents/supervisor.py`、`delegation/*` 不变、
> `cluster_common.py → core/cluster_common.py`。本文按历史记录保留，不作改写。

## 1. 发布内容

| 文件 | 变化 |
|---|---|
| `acps_cluster/delegation/{token,client,bridge,pep,skill_scope}.py`、`delegation/__init__.py` | 一次委派多令牌：按 audience 选凭据；ws 令牌只进 bundle；内部工具不收窄 |
| `acps_cluster/supervisor.py` | 受保护委派改为代码侧 `aip_delegate` 直发；删除本地产物镜像；随任务签发 ws bundle；提示词拆分能力委派 |
| `acps_cluster/partner_workspace_client.py`（新） | `ws_list/ws_read/ws_write` 三个模型工具 + 步数预算钩子 |
| `acps_cluster/workspace_access.py`（新） | `ACPS_WS_PARTNER_ACCESS` 开关（两侧同一口径） |
| 七个 `partner_*.py` | 装配 `AipDelegationHooks + BridgeHooks + WorkspaceAccessBudgetHooks` 与 ws 工具 |
| `acps_cluster/partner_workspace.py` | 作用域校验两级取 claims；taskId 与工作区作用域区分；能力结果统一登记 |
| `acps_cluster/cluster_common.py` | 兜底产物补 `id`；伙伴启动预热 workspace 对端 discovery 缓存 |
| `acps_cluster/discovery_metadata.py` | 内部工具不参与注册文本覆盖（按 `metadata.internal_tool`） |
| `acps_cluster/tests/*` | 新增/更新 6 个测试模块（hook / bundle / 客户端 / ACS 中性 / 种子 / 兜底 id） |

## 2. 发布过程（时间线，CST）

| 时间 | 动作 |
|---|---|
| 16:08 | 只读盘点：服务器 = `1c1a0b3` 工作树（多文件为更早版本），先取快照做 diff，确认**没有服务器独有补丁会被覆盖**（唯一差异是 experiment 缺 `AmpAccessLoggingHooks`，本发布顺带恢复） |
| 16:10 | 备份 `/opt/wit/predeploy-workspace-partner-20261026-161055`（20 个既有文件）；停 supervisor；覆盖 25 个文件；`py_compile` 通过 |
| 16:13 | 重启 `wit-workspace.service`（0s 就绪），灰度重启 scout |
| 16:16–16:33 | scout 正常上线；随后并行重启其余 6 个伙伴 + supervisor，均 active，无 ACS 指纹错误 |
| 16:34 | 第一次端到端（作用域 `e2e-1026-a`）：supervisor→workspace 的 `ws.create` 成功（`delegate_direct`），`ws.write` 被 workspace 以"非法 task_id"拒绝 → 定位为 taskId 语义混淆 |
| 16:47 | 第二轮修复部署 `/opt/wit/predeploy-e2e-fixes-20261026-164717`（supervisor/cluster_common/partner_workspace/测试）；并行重启，`ws.write` 成功 |
| 16:55 | 委派 scout 时 `ws_bundle_issued` 成功，但 scout 报 `Partner has no discovered Direct JSON-RPC endpoint` → 伙伴 discovery 缓存为空 |
| 17:03 | 第三轮部署 `/opt/wit/predeploy-ws-peer-seed-20261026-170307`（cluster_common 预热）；七个伙伴启动日志均出现"已预热 workspace 对端" |
| 17:20 | 第四轮部署 `/opt/wit/predeploy-ws-wait-fix-20261026-172044`（`aip_task refresh` 修复 + supervisor 注释）；全量并行重启 |
| 17:31 | **E2E 通过**：scout 读出 `shared/e2e-note-b.md` 并写出 `products/scout/0001-e2e-note-b.md`（manifest `actor=scout`） |

## 3. 验收证据（作用域 `e2e-1026-b`）

1. **supervisor 直发**：run-trace 出现 `delegate_direct`（`partner_aic=<workspace>`、
   `skill_id=ws.create/ws.write`、`task_scope=e2e-1026-b`），不再有
   `request must be of type 'string'` 报错。
2. **令牌随任务下发**：`ws_bundle_issued` 三枚（`ws.list/ws.read/ws.write`）；
   `delegate_direct` 载荷 `ws_bundle=["ws.list","ws.read","ws.write"]`、`parts=4`。
3. **partner 真正读写**：scout 先用 `ws_list` 发现文件、`ws_read` 读到
   `联调标记 E2E-1026-B`、`ws_write` 写出 `products/scout/0001-e2e-note-b.md`。
4. **manifest 归属正确**：
   `{"action":"write","actor":"scout","path":"products/scout/0001-e2e-note-b.md","bytes":23,...}`，
   内容 sha256 与源文件一致。
5. **PEP 绑定生效**：workspace `pep-audit.jsonl` 记录
   `decision=allow rule=delegation-token caller=<scout AIC> skill=ws.read/ws.write`。
6. **旁路已消失**：部署后没有新的 `workspace_mirror` 事件；`e2e-1026-b` 目录里
   只有 `shared/`（supervisor 经令牌写）与 `products/scout/`（伙伴写），
   没有 `products/supervisor/` 本地直写产物。
7. **对端可达**：七个研究伙伴的 `acps/discovered/` 均包含 workspace AIC；
   启动日志出现 `[discovery] 已预热 workspace 对端`。
8. **服务健康**：11 个服务 active；11 份心跳文件均在 30s 内更新；
   八个伙伴源码都恢复/保持 `amp_hooks=[AmpAccessLoggingHooks()]`（含 experiment）。

## 4. 回滚

本次用到的部署/验证脚本已整理成可复用工具包：
[`acps_cluster/deploy/workspace-rollout/`](../acps_cluster/deploy/workspace-rollout/README.md)
（盘点 → 备份覆盖 → 并行重启 → 审计 → E2E → 清理，含红线与已知坑）。

每个备份目录都含被覆盖文件的 tar 包：

```bash
# 第一轮（主体改造）
ls /opt/wit/predeploy-workspace-partner-20261026-161055/files.tar.gz
# 第二轮（taskId + 兜底 id）
ls /opt/wit/predeploy-e2e-fixes-20261026-164717/files.tar.gz
# 第三轮（discovery 预热）
ls /opt/wit/predeploy-ws-peer-seed-20261026-170307/
# 第四轮（aip_task refresh）
ls /opt/wit/predeploy-ws-wait-fix-20261026-172044/
```

回滚步骤：`systemctl stop` 对应服务 → `tar xzf <备份>/files.tar.gz -C /opt/wit/my-project`
（第四/三类只备份了单文件，直接 `cp -a` 回去）→ `chown -R wit:wit` →
`py_compile` → `systemctl restart`。workspace 的 ACS/证书从未改动，无需换身份。

## 5. 附加变更（20:37）：关闭 supervisor 的 PEP 准入

**现象**：叮当的调用方 AIC 是平台会话身份、会轮换。2026-10-26 白天出现新 AIC
`1.2.156.3088.1.0001.00001.EZTK03.C6PCA1.07OK`，不在
`ACPS_SUPERVISOR_INBOUND_ALLOW_AICS` 里 → PEP `deny / no-credential`，当天 5 次
inbound 任务被判 rejected（旧 AIC `…JISTB6…` / `…KXO4IK…` 仍正常）。

**决策**（用户拍板）：现网 supervisor 不做 PEP 准入。

**改动**：

- `/etc/wit-supervisor-pep.env` 置 `ACPS_PEP_ENABLED=0`（保留 leader AIC 与入站白名单
  两行，便于恢复；原文件备份 `…/wit-supervisor-pep.env.bak-20261026-203742`）；
- `supervisor.py` 增加"关闭态"显式启动告警（避免静默降级），随本次部署；
- 只重启 `wit-yanshu@supervisor.service`（ExecStartPre 抖动 10s）。

**影响**：不再校验入站白名单、不再写 PEP 审计；任何持 mTLS 的对端都能触发完整编排
（消耗 LLM 配额）。委托令牌的签发/校验、token 脱敏、skill 收窄等**不受影响**
（`pep_decide` 只在 `enabled=False` 时短路准入）。

**恢复**：把 `ACPS_PEP_ENABLED` 改回 `1` 并重启；建议同时把固定 AIC 白名单换成
"平台 AIC 前缀"匹配，避免下次轮换再误拒（未实现，属后续项）。

**验证**：

- `/proc/<MainPID>/environ` 含 `ACPS_PEP_ENABLED=0`；
- 启动日志出现 `⚠ PEP 准入已关闭（ACPS_PEP_ENABLED=0）…`；
- `pep_decide` 在关闭态直接 `accept()`（`tests/test_delegation.py` 有覆盖）；
- 服务 `常驻已上线`，心跳正常。

## 6. 附加变更（21:02）：可视化"最近任务"列表

**背景**：并发时 `live.json` 只跟"开始时间最新"的一条 run，其余任务只能靠产物里的
`?run=` 链接看；同一任务的主 run 与 `submit_job` 作业子 run 还会互相抢指针（20:46
实测：live 指向 3 事件的作业子 run，17 事件的主 run 不在页面上）。

**改动**：

- `viz_live.py` 新增 `scenarios/index.json`：每次落盘更新本 run 的一条记录
  （run_id / 指令摘要 / started_at / updated_at / done / events），保留最近 40 条；
  跨进程用 flock 串行化；索引缺失时用已有 `run-*.json` 兜底生成，超过 5 分钟没有
  更新的历史 run 标记为已结束。
- 前端（`cluster-viz`）侧栏新增"最近任务"：读 `index.json`，点选即切换到该 run 的
  回放（退出实时跟随），"● 跟随最新"回到 `?live=1`。
- 部署：备份 `/opt/wit/predeploy-viz-20261026-210223`（前一版 `index.html` + `assets/`）；
  前端 dist 覆盖到 `/opt/wit/cluster-viz/`；`viz_live.py` 覆盖后重启 supervisor。

**验证**：`/scenarios/index.json` 返回 200，40 条历史 run 全部列出（已结束标记正确）；
页面引用新 `assets/index-DrfVVoRn.js`；`tests/test_viz_live.py` 新增 4 条索引用例。
无头浏览器实测：`?live=1` 侧栏出现"最近任务"（时间/事件数/run_id，当前项高亮），
指针陈旧时顶部标"已中断 · N 分钟无更新"；`?run=run-b8b63a83e5ee` 仍能正常回放
（8.3/534s，侧栏同步高亮该条）。

## 7. 事故与修复（21:23）：重启 supervisor 杀掉正在跑的作业

**现象**：用户 21:08 提交的 WWW Demo 初稿任务，主 run 21:08:59 交付受理回执后转后台作业
`job-20261026-210827-0ac3`；作业跑到 step 17（21:22:53，已克隆 26MB 仓库、正在 ws.list/ws.read
与伙伴委派）后进程消失，`status.json` 停在 `running`，没有报告。

**根因**：`submit_job` 用 `subprocess.Popen(..., start_new_session=True)` 起 `jobs_runner`，
但 `start_new_session` 只换会话、**不换 cgroup**；`wit-yanshu@supervisor.service` 默认
`KillMode=control-group`，重启 supervisor 时 systemd 会杀掉同 cgroup 的所有子进程——包括
正在跑的作业。当晚 20:57 / 21:01 / 21:24 三次重启各杀掉一批作业（job-204627、201348、182856
同样 `interrupted / process-exited`）。

**修复**：drop-in `/etc/systemd/system/wit-yanshu@supervisor.service.d/killmode.conf`
设 `KillMode=process`（重启只杀主进程，作业继续跑；作业自带 3600s 看门狗，死进程由
`jobs_store.resolve_liveness()` 按 pid 存活判定收口为 `interrupted`）。已 `daemon-reload`
生效、无需重启。死作业已收口，工作区与 `request.txt` 保留。

**遗留**：作业进程与 supervisor 仍在同一 slice（只是不再被 stop 连带杀掉）；更彻底的做法是
用独立 `wit-job-runner.service`/transient unit 承载，属后续项。

## 8. 等待工具修复（21:30）：论文作业"卡在 workspace/scout"的根因

用被中断的那份作业（pid 3437484，17.8 分钟 / 16 步）做时间拆解：

| 项 | 数据 |
|---|---|
| 模型每步 | 16 步，平均 **54s/步**，合计 864s（最长单步 211s） |
| `acps_task(action="wait")` | **11 次 × 33.5s**（每次 45s 上限），且每次都要再走一步模型 |
| `wait_partner` | 1 次，**0 秒报 `AipError: Unsupported task action`** |
| `wait_many` | 每次探针都是 `TimeoutError` |
| 工作区委派 | 单次 35–419s（ws.list/read 各是一次完整 workspace LLM 任务） |
| scout 委派 | 216s |

**根因**：wit 的 `aip_task` 只接受 `refresh/continue/complete/cancel`；`wait_partner` 用了
`action="get"`（立刻报 Unsupported），`wait_many` 用 `action="wait"` 又被 30s 的
`wait_for` 掐成超时。两个"代码侧等待"工具全废，模型只能退回 45 秒一轮的
`acps_task wait` 轮询——每一轮还要一次模型调用（约 50s），即**一次等待 ≈ 95 秒**。

**修复**：

- `wait_partner` / `wait_many` 的探针统一改 `action="refresh"`（代码侧轮询到终态）；
  `wait_partner` 遇到失效引用返回结构化错误 + 处置建议，不再抛 AipError；
- 提示词补充：**把多次工作区操作合并到一次委派**（不要一个文件一次委派）、
  `ws.clone_repo` 已返回文件树/README 不要重复 `ws.list`、等待优先用
  `wait_partner`/`wait_many`；
- 验证：`test_supervisor_tools.py` 新增 3 条（探针必须 refresh、轮询到终态、失效引用
  结构化），42 passed。

**预期**：等待不再烧步数；但模型每步约 50s 的时延仍是主要成本，论文类作业仍预计
15–35 分钟（此前 25–45 分钟）。更彻底的做法是让 supervisor 直连 `ws.*`（不经
workspace 的 LLM 任务），属后续大改。

## 9. 直连存储端点（22:04）：workspace 存储不再经过 LLM

**改动**：workspace 进程内新增只绑 `127.0.0.1:8471` 的 HTTPS 端点（`POST /ws`），
supervisor 的 `ws_direct` 工具与伙伴的 `ws_list/ws_read/ws_write` 持"委托令牌 +
本机客户端证书"直接执行 ws.*，不经 workspace 的模型；AIP agent（8470）保留给平台/模型。

**鉴权**：双向 TLS（服务端用 trust-bundle 验客户端证书）+ 调用方身份取自证书 SAN
`acps://<AIC>` 且必须等于令牌 `act.aic` + issuer/aud/skill/task_id 全校验；不做
leader 免令牌旁路；每次调用落 `ws-workspace/logs/ws-direct-audit.jsonl`。

**实现坑**：平台签发的 agent 证书 EKU 只有 `clientAuth`，OpenSSL 以
`unsupported certificate purpose` 拒绝把它当服务端证书。改为首次启动生成**自签 EC
证书**（serverAuth、SAN=workspace AIC），客户端按 DER 指纹固定
（`ws-workspace/acps/direct/server.pem`）。

**验证**（生产服务器 smoke）：
`ws.create` 26ms / `ws.write` 10ms / `ws.read` 11ms / `ws.list` 9ms（AIP 委派为
35–419 秒）；漏令牌 → `token-required`；用 scout 令牌 + supervisor 证书 →
`act-peer-mismatch`；审计落盘 12 行。

**回滚**：`ACPS_WS_DIRECT=0` 后重启 workspace/supervisor/伙伴即回到纯 AIP 委派；
代码备份 `/opt/wit/predeploy-ws-direct-20261026-214348`。

## 10. 作业被看门狗收口（23:38 真实中断）与预算调整

**现象**：`job-20261026-223813-4d32`（www-demo 投稿初稿）在 22:38:13 启动，23:38:13
**被作业看门狗强制终止**（`exit_code=124`，日志 `运行超过 3600s，强制终止`），
`report.md` 为空。这次不是前端误报——前端只是如实显示"已中断"。

**时间都花在哪**：

| 步骤 | 耗时 |
|---|---|
| scout 第 1 轮检索 | 23:15:16 → 23:15:57 |
| scout 第 2 轮补充检索 | 23:20:44 |
| scout OpenAlex 引用核验 | 23:03:04 → 23:07:35 |
| synthesis 精读 6 篇 | 23:03:04 → 23:07:35 |
| scout 第 4 轮 arXiv 重核（OpenAlex 结果不对后换源重试） | 23:10:07 → 23:12:07 |
| writer 写作 | 23:15:16 起，23:36 又被重派一份，23:38 被杀 |

根因不是单点：**4 轮检索 + 6 篇精读 + 写作 + 审稿**本来就超过 60 分钟预算；
另有"writer 还在写、supervisor 又重派一份"的重复消耗。

**调整**：

- `deploy/.env.production`：`ACPS_JOB_MAX_RUNTIME / LOCK_TIMEOUT / WAIT_SECONDS`
  3600 → **7200**（2 小时）；
- supervisor 纪律新增：
  - 检索最多 **2 轮**（首轮 + 1 次补充）；引用核验最多 **1 轮**，失败如实标注
    "未核验"，**不换数据源重试**；
  - 精读最多 **4 篇**（超出只挑最相关的）；
  - `wait_partner` 返回 `still_working=true` 时只能继续等，**绝不重新委派同一伙伴**；
- 前端"5 分钟无事件=已中断"的误报另由心跳修复（`Recorder.touch`，见提交 `531870a`）；
  本次 3600s 收口是真实中断，两者不要混为一谈。
- **失败状态显式化**：`viz_live.mark_run_failed(run_id, reason)` 会把 scenario 补一条
  `done` 事件、把 `index.json` 的条目写成 `done=true, status=failed, note=reason`；
  `jobs_runner` 在看门狗收口与失败收尾时调用它（run_id 由 supervisor 写入
  `ACPS_VIZ_RUN_ID_FILE`）。前端据此显示"失败"，不再靠 5 分钟无事件猜"已中断"；
  真正还在跑的等待由 `Recorder.touch()` 续命。

**遗留**：作业没有断点续跑，超时只能重跑；后续可考虑"进度感知看门狗"
（伙伴任务仍在更新就续期），而不是固定墙钟。

## 11. 遗留

- supervisor 自身产物自动归档（`products/supervisor/`）未做，当前由编排显式
  `ws.write`；需要时按 handoff §4.4 备选实现；
- 一次委派只授权一个 `ws.*` 能力（`ws.create` + `ws.clone_repo` 要分两次委派）；
- partner 令牌 TTL 600s，超长任务末尾会拿到"令牌已过期"的 awaiting；
- bundle 令牌文件只读不删，过期条目读取时过滤，暂无清理 timer。
