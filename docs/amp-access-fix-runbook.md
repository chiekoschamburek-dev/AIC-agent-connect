# 排行榜调用量修复：服务器执行手册

> 对应诊断：[incident-2026-10-19-ranking-zero.md](./incident-2026-10-19-ranking-zero.md)
> 目标：让 access 记录真正产生并进入平台，使排行榜上的调用量从 0 变为真实数字。

## 0. 交付物清单

| 文件 | 作用 |
|---|---|
| `acps_cluster/deploy/patch_amp_access.py` | 幂等补丁：给 14 个 agent 挂 `AmpAccessLoggingHooks`，收窄重启清理范围；支持 `--dry-run` / `--only` / `--revert` |
| `acps_cluster/deploy/amp_access_sender.py` | access 专用转发器：`ws-*/logs/amp_access_*.jsonl` → Kafka `amp.access` |
| `acps_cluster/deploy/wit-amp-access-sender.service` | 上述转发器的 systemd 单元 |
| `acps_cluster/deploy/apply_amp_access_fix.sh` | 一键编排（备份 → 打补丁 → 装服务 → 试点重启 → 打印验证） |

## 1. 本地已验证的事实（打补丁前可以先信这几条）

| 验证项 | 结果 |
|---|---|
| 补丁可识别 14 个模块 | ✅ 14/14 |
| 打补丁后全部 `py_compile` 通过 | ✅ |
| 幂等（重复运行） | ✅ 第二次全部"已挂载，跳过" |
| 回滚后与原始文件逐字节一致 | ✅ |
| 14 个 agent 构建后 `amp_hooks` 含钩子 | ✅ 14/14 |
| ACS 投影未被污染（不触发指纹保护） | ✅ ACS 里无任何 amp 痕迹 |
| 转发器能识别 access / 跳过非 access | ✅ 2 收 1 跳 |
| 回滚脚本可精确还原 | ✅ |

补丁实际改动每个文件只有 **2 行**：

```diff
+from wit.hooks.acps.amp import AmpAccessLoggingHooks
         aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
+        amp_hooks=[AmpAccessLoggingHooks()],
```

> 过程中的一个真实教训：`partner_kb.py` 头上有 `from __future__ import annotations`，
> 早期版本把 import 插到它前面会导致 `SyntaxError`。现已修正，并给补丁加了
> "先 compile 再落盘"的自检——改坏了宁可不动。

## 2. 执行（在服务器上，root）

```bash
# 先看会改什么，不落盘
bash /opt/wit/my-project/acps_cluster/deploy/apply_amp_access_fix.sh --dry-run

# 确认后执行（会备份到 /opt/wit/amp-access-fix-backup-<时间戳>/）
bash /opt/wit/my-project/acps_cluster/deploy/apply_amp_access_fix.sh
```

脚本会**只重启 writer 一个**作为试点，并轮询等待 `amp_access_*.jsonl` 出现。
其余 6 个伙伴等试点确认后再手动全量：

```bash
systemctl restart wit-yanshu@{scout,synthesis,critic,contradiction,frontier,experiment}
```

## 3. 验证（三关，逐关过）

### 第一关：本地有没有写出 access

```bash
tail -1 /opt/wit/my-project/acps_cluster/ws-writer/logs/amp_access_*.jsonl | python3 -m json.tool
```

期望：

```json
{"schema_version":"1.0","log_type":"access","aic":"...VTF97R....",
 "body":{"durationMs":..., "caller":{"aic":"..."}, "callee":{"aic":"..."}}}
```

⚠️ **注意**：只有**真实经过 AIP 传输层**的调用才会产生记录。
在本机直接 `python partner_writer.py` 或跑 `delegation/demo_*.py` **不计数**。
必须从外部（另一个 agent 的委派、叮当、或 mTLS 客户端）打进来。

### 第二关：转发器有没有投出去

```bash
journalctl -u wit-amp-access-sender -n 30 --no-pager
# 期望滚动出现：本轮 N 条（累计命中 N，已投 N，跳过 M）
```

如果"累计命中"一直涨而"已投"不涨，看失败原因（多半是 broker 权限）。

### 第三关：平台榜单有没有出现

榜单按小时聚合，**有 30–60 分钟延迟**，别改完立刻查：

```bash
curl -s "https://wt.ioa.pub/internal/matched-stats/top?startAt=$(date +%Y-%m-%dT%H:00:00+08:00)&endAt=$(date -d '+1 hour' +%Y-%m-%dT%H:00:00+08:00)&n=200" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print([x for x in d['callee'] if 'VTF97R' in x['aic']])"
```

> 参数要求：`startAt` / `endAt` 必须**按小时对齐**且用 `+08:00`，
> 否则返回 `400 amp-invalid-time-range`。

## 4. 排障对照表

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 重启后仍无 `amp_access_*.jsonl` | 该模块补丁没生效 | `grep -n "amp_hooks" partner_xxx.py`，没有就重跑补丁 |
| 日志文件出现但一直 0 字节 | 没有真实调用经过传输层 | 从外部打一次 mTLS 委派 |
| 有文件有内容，转发器"跳过"计数涨 | 记录不是 `log_type=access` | 检查是否误读了 heartbeat 文件 |
| 转发器"已投"涨但榜单仍 0 | 平台侧消费延迟 | 等 1 小时再看；查 `accessMissingHours` |
| 服务起不来 | import 路径 / 权限 | `journalctl -u wit-yanshu@writer -n 50` |

## 5. 回滚

```bash
# 1) 代码与单元回滚（.bak 精确还原）
cd /opt/wit/my-project/acps_cluster
sudo -u wit /opt/wit/venv/bin/python deploy/patch_amp_access.py --revert

# 2) 停掉转发器
systemctl disable --now wit-amp-access-sender.service

# 3) 重启服务
systemctl daemon-reload
systemctl restart wit-yanshu@writer
```

## 6. 风险与注意

1. **不要动心跳链路**。`wit-amp-sender` 与 `wit-amp-mirror` 是平台判在线的命脉，
   停掉 1–2 分钟全集群会被判离线。本次修复**完全不碰**它们，access 走独立的
   服务和独立的 topic。
2. **首次上线用 `--init-offsets`**，避免把历史文件整体回灌（历史上本来也是空的）。
3. **同账号单会话**：该平台同账号重复登录会踢掉旧 token，重启多个 partner 时
   systemd 已有 0–300s 随机抖动，别用 `for` 循环瞬间重启全部。
4. **磁盘**：access 是逐次交互一条，量上来后 `ws-*/logs/` 会长得比心跳快得多。
   建议后续加 logrotate（按天切、保留 N 天），但**先别在修复窗口里做**。

## 7. 修完之后：调用量怎么来

计量通了以后，榜单数字取决于**真实经过传输层的交互次数**。榜单是平台横向排名、
榜首已到 5510 次，所以需要持续且多样的调用。这正是"不要一直干重复任务"的意义：

- **不同 agent**：8 个 agent 里目前只有 scout 能在发现层被检索到（其余要么是
  dev 身份、要么描述文本太弱），这本身是另一个可优化点；
- **不同能力**：scout 检索 / synthesis 精读 / writer 写作 / critic 审稿 /
  contradiction 查矛盾 / frontier 探测前沿，天然是一条完整的科研流水线，
  一次真实研究任务就能自然覆盖 4–6 个 agent；
- **重复调用同一能力**既容易被认为是刷量，也不产生新价值。

建议下一步做**任务派发器**：从真实研究主题池里取题 → 按流水线分派给不同 agent →
每次主题不重复 → 自然累积调用量。这需要先把本文的计量修好，否则派发再多也是 0。
