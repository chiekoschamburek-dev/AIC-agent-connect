# kb 公网端点不可达：firewalld 漏放 8599（2026-10-14）

状态：**已修复并实弹验证**。影响面：kb 的 RPC 端点（`https://192.0.2.10:8599/rpc`）
在 firewalld 层从未放行——本机互访（公网 IP 回环）与外部调用方全部 TCP 不可达；
kb 服务本体一直是健康的。

## 症状与定位链（kb 实弹委派 smoke 暴露）

替换自愈/预热修复后的验收 smoke：leader **能解析 kb 端点**（roster → AIC D86STV → 8599/rpc），
但委派全部卡在传输层：`ConnectError: All connection attempts failed`，两个远端任务卡 `dispatching`。

分层测试（服务器本机）：

```
ss -tlnp             → 0.0.0.0:8599 在听（pid=kb），TCP 层没有"没在听"的问题
curl 127.0.0.1:8599  → TCP OK；带 supervisor 客户端证书 mTLS → HTTP 200   ← kb 本体健康
curl 192.0.2.10:8599 → TCP FAIL（本机连自己的公网 IP=回环/hairpin 路径）
curl 192.0.2.10:8445 → TCP OK（scout，同一路径）                        ← hairpin 本身是通的
firewall-cmd --list-ports → 22 5178 8443 8444-8448 8449 8450 8461 8468 8590 8591
                              ↑ 没有 8599！                                ← 根因
```

结论：本机/外部到公网 IP 的连接经 hairpin，落到 firewalld INPUT，被"未放行端口"丢弃；
回环地址 `127.0.0.1` 不受影响——所以"服务在跑 + 心跳在线"的假象下，**任何走注册端点的
调用方（含同机 supervisor）都到不了 kb**。

## 修复与验证

```bash
firewall-cmd --permanent --add-port=8599/tcp && firewall-cmd --reload
```

证据（修复后）：

- 本机 hairpin `192.0.2.10:8599` TCP OK；mTLS（supervisor 证书）→ **HTTP 200**；
- 外网（笔记本）直连 8599 可达（云安全组本就放行，只缺本机 firewall）；
- **kb 实弹委派 smoke 全绿**：`kb.schema` 一次委派成功，产物回收、product_gate 校验通过
  （task_68d6a5bdc04e，steps=11，无自愈归档）。

## 教训（已同步进 runbook）

1. 新 agent 上线清单必须包含"endpoint 端口 `firewall-cmd --permanent --add-port`"——
   此前清单只列了端口分配，没列放行步骤；
2. "服务 active + 平台 alive" **不等于**"端点可达"：心跳是出站链路（AMQP→Kafka），
   与入站 RPC 完全独立。每一个新端点都应做一次"本机 hairpin + 外部直连 + mTLS 200"三连测；
3. 回环地址上的验证（127.0.0.1）不能替代注册端点验证（公网 IP）。
