# 平台 wt.ioa.pub TLS 证书过期（2026-10-15，平台侧）

状态：**已解除——平台侧已续签**（2026-10-15 23:55 CST 服务器实测
`openssl s_client -serverName wt.ioa.pub` → `notAfter = Jan 13 12:24:27 2027 GMT`；
同日 23:55 的 discovery 预热中 designer 那条走"平台公开接口"成功即为佐证）。
事故本身：平台侧、我方无法修复，发现于 2026-10-15 20:16 CST（维护窗口收尾核验时），
过期约 2h47m～至续签前。

## 证据

```
$ openssl s_client -connect wt.ioa.pub:443 -servername wt.ioa.pub
subject=CN = wt.ioa.pub
issuer =C = US, O = Let's Encrypt, CN = YE1
Verify return code: 10 (certificate has expired)
notBefore=Jul 17 09:29:53 2026 GMT
notAfter =Oct 15 09:29:52 2026 GMT   ← 已过期约 2h47m（发现时刻 12:16 UTC）
链：leaf（过期）× ← YE1 intermediate（有效至 2028-09）× ← ISRG Root（2032-09）✔
```

特征：**中间 CA 与根均正常，仅叶子证书未续签**（Let's Encrypt 正常应在到期前 ~30 天
自动续期——平台的 certbot/ACME 续期链路疑似失效）。

## 影响

- 一切经 HTTPS 访问平台的客户端（含 mTLS）**全部证书校验失败**：我们的
  discovery/ATR 查询、社区成员的调用、平台控制台（浏览器会告警/拦截）都不通；
- **我方服务本体不受影响**：9/9 服务 active；AMP 心跳走 Kafka（不经 HTTPS），
  平台侧 alive 状态不受此影响；委派类**出站**平台调用在续签前会失败；
- 我方继续正确校验证书（**不**做 verify 关闭等降级），等待平台续签即恢复。

## 处置

1. **通知平台方**（急）：续签 Let's Encrypt 证书并 reload（certbot renew + nginx reload），
   并排查自动续期为何没跑（timer/cron 失效？）；
2. 我方不动作、不降级；平台恢复后即可用既有只读脚本复核 discovery/alive；
3. 本条并入给平台方的反馈包（与 filter 400、语义索引、记录停用等事项并列）。
