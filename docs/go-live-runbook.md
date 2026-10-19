# 一键上线（模型 → 计量 → 转发 → 派发）

> 目标：把"排行榜调用量 0"这件事从诊断到修复到产出，收敛成**一条命令 + 一次人工确认**。
> 三个前置文档：`incident-2026-10-19-ranking-zero.md`（根因）、
> `amp-access-fix-runbook.md`（计量修复）、`task-feeder.md`（派发器）、
> `model-routing-bupt-deepseek.md`（模型切换）。

## 0. 先跑自检（不改任何东西）

```bash
cd /opt/wit/my-project/acps_cluster
sudo -u wit /opt/wit/venv/bin/python deploy/preflight_check.py
```

它逐项探测并给出**可执行的下一步**。输出三类：

- `[OK]` 通过
- `[WARN]` 不拦路，但要知道（例如网关不可达、脚本 CRLF）
- `[FAIL]` 阻塞：不修就会白跑

有 `[FAIL]` 时退出码为 1；只有 `[WARN]` 为 2；全通为 0。

**为什么先自检**：这条链路有四个坑，失败时**都长得像"脚本坏了"**——

| 坑 | 表象 | 实际 |
|---|---|---|
| 网关只认校园网 IP | 连接超时 | 服务器物理不可达，需隧道或走保底 |
| 本地 dev 证书 vs 平台证书 | `ReadError` | TLS 能连上但握手被拒 |
| `amp_hooks` 没挂 | 调用全成功、榜单恒 0 | 计量没点亮 |
| 重启 `rm -f logs/*.jsonl` | 数据莫名消失 | access 证据被清空 |

## 1. 一键上线

```bash
# ① 先看会做什么（不动任何东西）
bash deploy/go_live.sh --dry-run

# ② 正式执行：自检 → 备份 → 补丁 → 装转发器 → 只重启 writer → 派 1 个真实任务
bash deploy/go_live.sh
```

脚本默认**只动 writer 一个**（试点），跑完会打印：模型路由、账本统计、
writer 侧 access 记录、以及下一步命令。

试点确认无误后：

```bash
# ③ 全量重启其余 6 个伙伴
bash deploy/go_live.sh --full
```

### 只想验收、不想改

```bash
bash deploy/go_live.sh --verify
```

## 2. 它到底做了什么

| 阶段 | 动作 | 为什么 |
|---|---|---|
| 0 | `preflight_check.py` | 任一阻塞就停，避免"跑到第 5 步才发现第 1 步没做" |
| 1 | 备份到 `/opt/wit/yanshu-golive-backup-<时间戳>/` | 一条命令可回滚 |
| 2 | `patch_amp_access.py` | 挂 `AmpAccessLoggingHooks`（幂等）+ 收窄重启清理 |
| 3 | 装 `wit-amp-access-sender` | 本地 `amp_access_*.jsonl` → Kafka `amp.access` |
| 4 | 只重启 writer + 派 1 个真实任务 | 用最小代价验证"调用 → access 记录"闭环 |
| 5 | 打印路由/账本/access 记录 | 让人一眼看出成没成 |

## 3. 怎么判断真的成了

三关，按顺序过：

```bash
# 第一关：本地有没有写出 access
tail -1 ws-writer/logs/amp_access_*.jsonl | python3 -m json.tool
#   期望：log_type=access，body 里有 caller / callee

# 第二关：转发器有没有投出去
journalctl -u wit-amp-access-sender -n 30 --no-pager
#   期望：本轮 N 条（累计命中 N，已投 N，跳过 M）

# 第三关：平台榜单（30–60 分钟后，按小时聚合）
curl -s "https://wt.ioa.pub/internal/matched-stats/top?startAt=$(date +%Y-%m-%dT%H:00:00+08:00)&endAt=$(date -d '+1 hour' +%Y-%m-%dT%H:00:00+08:00)&n=200" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print([x for x in d['callee'] if 'VTF97R' in x['aic']])"
```

## 4. 常驻派发（可选，最后再做）

试点和全量都通过后，再开自动派发：

```bash
install -m 644 deploy/wit-task-feeder.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now wit-task-feeder
journalctl -u wit-task-feeder -f
```

默认每 15 分钟派 2 个，主题池 192 个组合，用尽后自行停止。

## 5. 回滚

```bash
# 最快：把模型切回 MiniMax（不用改代码）
echo 'ACPS_MODEL_PRIMARY=none' >> deploy/.env.production
systemctl restart wit-yanshu@{scout,synthesis,writer,critic,contradiction,frontier,experiment}

# 代码回滚
cp -a /opt/wit/yanshu-golive-backup-<时间戳>/. /opt/wit/my-project/acps_cluster/
cp -a /opt/wit/yanshu-golive-backup-<时间戳>/deploy/. /opt/wit/my-project/acps_cluster/deploy/
systemctl daemon-reload
systemctl restart wit-yanshu@writer

# 停转发器
systemctl disable --now wit-amp-access-sender
```

## 6. 本地自检的这五项 FAIL 是正常的

如果你在**本机**跑 `preflight_check.py`，会看到 5 个阻塞项。它们不是脚本 bug，
而是"这件事必须在服务器上做"的证据：

| 阻塞项 | 原因 |
|---|---|
| 调用方身份 CN ≠ 平台 AIC | 本地证书是 dev CA 铸的，平台不认（私钥在服务器上） |
| 计量钩子 0/14 已挂 | 本地副本尚未打补丁（线上打） |
| 重启清理仍删 `*.jsonl` | 同上 |
| 缺 kafka-python | 本地 venv 未装（服务器 venv 有） |
| 账本目录不可写 | 本机沙箱限制 |

在服务器上跑，这五项应当全部转 `[OK]`。**如果服务器上仍报「调用方身份」不符，
说明你连的不是运行 supervisor 的那台机器**——这是唯一无法绕过的前提。

## 7. 设计取舍（为什么不是全自动）

1. **试点不跳过**：8 个 agent 同时改，一旦不对分不清是钩子没生效还是投递没生效。
2. **不自动开常驻派发**：派发会真的消耗对端模型调用。让人看过试点结果再决定。
3. **自检与执行分离**：`--verify` 可以随时跑，不会因为"检查"而改变线上状态。
4. **每步可单独执行**：脚本调的都是独立命令，失败时照提示重跑那一条即可，不必整套重来。
