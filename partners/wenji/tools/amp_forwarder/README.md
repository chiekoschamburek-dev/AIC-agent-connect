# AMP 上报转发器（外部 Agent → 梧桐平台 Kafka）

**平台事实（2026-10-14 实测确认，队友管线交叉验证）**：

| 项 | 值 |
|---|---|
| Broker | `wt.ioa.pub:19092`（解析到 49.232.42.120） |
| 协议 | **明文 TCP，无 TLS、无 SASL**——不要给客户端配证书或账号口令 |
| Topic | `amp.<type>`：heartbeat / metrics / access / message / system / audit |
| Topic 创建 | 平台开启 auto-create，首次发送即建 |
| Discovery aliveMap | **依赖 heartbeat 进 Kafka**：断心跳会被判离线，按"在线"筛选时发现不到 |
| Access 计数 | `correlationId` 必须是 **taskId**，平台按 taskId 双方配对算 1 次调用/被调用 |
| Message 计数 | 需 `messageId`（收发一致配对）+ `eventType`（至少 send/receive） |

WIT 在 workspace 下写六类 `amp_*.jsonl` journal；本工具增量读取并按原样投递到平台 Kafka
（队友「研枢」的 `amp_heartbeat_sender.py` 采用同样的"原样转发"模式，已验证可行）。

## 用法

```bash
# 安装 kafka 客户端（二选一）
.venv/bin/python -m pip install kafka-python        # 纯 Python，推荐
# .venv/bin/python -m pip install confluent-kafka   # 性能更好

# 1) dry-run：打印将要上报的记录与目标 topic（无需 kafka 客户端）
python3 tools/amp_forwarder/forwarder.py --journal-dir .wit/wenji-aip-agent --dry-run

# 2) 真实上报（持续模式，serve 起来后尽快启动——heartbeat 决定 discovery 可见性）
export AMP_KAFKA_BOOTSTRAP=wt.ioa.pub:19092   # 默认即此，可省略
python3 tools/amp_forwarder/forwarder.py --journal-dir .wit/wenji-aip-agent
```

offset 状态文件默认 `.amp-forwarder-state.json`（已 gitignore）。

## 时序提醒

```
serve（wit 启动，开始写 amp_heartbeat_*.jsonl）
   └─► forwarder 立刻跟上（本工具，持续模式）
          └─► 平台 Kafka → Monitor → Discovery aliveMap 在线
                 └─► 队友按"在线"筛选才能发现「问迹」
```

**端点可达 ≠ 能被发现**：只保证 RPC 端口通而 heartbeat 没进 Kafka，队友依然发现不了你。
