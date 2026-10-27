# （已退役）

## 为什么需要桥接

问迹引擎（DB + 向量 + LLM 桥接）跑在开发机上；团队 VPS（8.160.176.215，2C/1.8G，hkr 无
sudo）只提供公网入口。方案是**原始 TCP 中继**，mTLS 端到端语义完整保留：

```
调用方（TLS+mTLS）
   │
   ▼  ①安全组放行 PUBLIC_PORT
VPS 0.0.0.0:PUBLIC_PORT        socat（hkr 可跑，无需 root）
   │
   ▼
VPS 127.0.0.1:19000            sshd 反向转发（默认即可绑定回环）
   │        SSH 隧道（ServerAlive 保活）
   ▼
开发机 127.0.0.1:8443          wit serve_direct_partner（TLS 1.3 + 双向证书校验）
```

证书视角：wit 签发的 server 证书 SAN 覆盖公网 IP（8.160.176.215），调用方握手时直连到
开发机上的 wit 监听，验证通过；客户端证书同样端到端透传。

## 已确认的 VPS 事实（2026-10-14）

| 端口 | 归属 |
|---|---|
| 8444 / 8599 | 队友「研枢」已注册智能体（Supervisor / KB-Partner） |
| 8590 | 队友「研枢」已注册智能体（证书 O=Yanshu 实证）——**不可用** |
| 8461 | 某已注册智能体 |
| 8445 / 8446 / 5178 | 研枢 partner 进程 / cluster-viz |
| **8601（已放行）** | 管理员已开安全组；桥接已绑定 |

## 用法

```bash
cd tools/vps_bridge
PUBLIC_PORT=<管理员分配的端口> ./bridge.sh up       # 预部署中继+隧道
./bridge.sh status                                 # 两侧状态
./bridge.sh down                                   # 拆除
```

管理员放行安全组端口后，无需其它操作，链路即刻可用（隧道与 socat 已先行启动）。

## 注意

- 隧道由开发机主动保持；开发机断网/重启后重跑 `bridge.sh up`。
- `ACPS_PUBLIC_ENDPOINT` 必须填 `https://8.160.176.215:<PUBLIC_PORT>`（**不带路径后缀**，
  WIT 监听器路由在根路径 `/rpc` `/health`，ACS 端点会自动补 `/rpc`）。
- 验证顺序：`bridge.sh status` → 本机 `serve` → 公网 `curl -k https://8.160.176.215:$PORT/health`
  （/health 是本机运维探活，对外任务是 `/rpc`，截图别搞混）。
