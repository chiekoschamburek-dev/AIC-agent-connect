# 问题反馈：平台 ACS 缺 `certificate.altNames` 时，IP 端点证书只含 URI SAN，wit 本地校验拒签

> **2026-10-25 更正（实测）**：真正原因不是 wit 的 CSR 不写 SAN。按
> [ATR 规范](https://github.com/ACPs-community/acps-specs)（`04-ACPs-spec-ATR`）
> 第 (9)(12) 步，平台 CA **从注册 ACS 的 `certificate.altNames` 构造 SAN**，不读 CSR。
> 旧 workspace 记录 ABKIJE（v1.0.0）的 ACS 是 `certificate: {}`，所以 CA 只签出
> `URI:acps://{AIC}`；`_validate_public_endpoint` 要求 IP 端点必须有 IP SAN，
> 因而拒签。CSR SAN 补丁（`cluster_common._patch_ca_csr_san()`）对本平台 CA 无效
> ——实测 CA 返回的 client/server 证书都只有 URI SAN。
> **已按修复路径注册 v1.1.0**：AIC
> `1.2.156.3088.1.BUPT.VTF97R.N9M0PW.ZB1CII.0B3X`，ACS 写入
> `certificate.altNames.ip=["8.160.176.215"]`，平台 APPROVED；旧 ABKIJE 弃用。

> 建议投递：AIP-PUB 社区问题反馈（规则原文鼓励问题反馈；格式沿用
> [issue-wit-2.1.0-discovery-filter-400.md](issue-wit-2.1.0-discovery-filter-400.md)）
> 分类：框架缺陷（wit 材料生成）× 平台交互（wt.ioa.pub CA 校验）
> 发现日期：2026-10-25 · 实测环境：wit 2.1.0（服务器与本地同版本）· CPython 3.12 · Linux/Windows 双端复现

---

## 一、现象

`HttpCaClient.issue_certificate_pair()` 走通登录、EAB、权威 ACS 拉取后，本地预证书
（self-signed 前置材料）生成即被 wit 自身的
`certificate_validation._validate_public_endpoint()` 判定：

```
CaClientError: certificate material validation failed: server_public_endpoint_san_mismatch
```

平台 CA 未收到任何请求——拒绝发生在**客户端本地预校验**：`public_endpoint` 的
hostname 可解析为 IP 时，预证书 SAN 必须包含 `x509.IPAddress` 条目，而 wit 生成的
预证书里没有。

## 二、复现

条件：`AcpsConfig.custom(..., public_endpoint="https://<IP>:<port>/rpc",
certificate=AgentCertificateRequest(ip=("<IP>",)))`（`certificate` 请求已显式含 IP）。

三种配置全部复现同一拒签：

| # | `certificate` 请求 | `acs.final.json`（含权威 endPoints） | 结果 |
|---|---|---|---|
| 1 | `AgentCertificateRequest(ip=("8.160.176.215",))` | 无（清理后缺失） | 拒签 |
| 2 | 同上 | 已预写平台权威 ACS（endPoints=同 IP） | 拒签 |
| 3 | `AgentCertificateRequest(dns=("8.160.176.215",), ip=("8.160.176.215",))` | 同上 | 拒签 |

判定函数（`certificate_validation.pyc :: _validate_public_endpoint` 反编译要点）：
`hostname = urlsplit(public_endpoint).hostname`；`A = ip_address(hostname)`；
`if A not in cert.SAN.get_values_for_type(x509.IPAddress): raise …_san_mismatch`。
即：IP 端点 → 预证书必须含 IPAddress SAN，而生成器没有写入。

**对照组**：DNS 端点不受影响——同一集群内 `dns` 类请求的 agent（如 scout 系）
注册/换签均正常；已签发的存量证书（2026-10-09/14 签发）也含
`IP Address:8.160.176.215`，说明平台 CA 与历史流程都支持 IP SAN，缺陷定位在
wit 2.1.0 的**预证书生成**环节（`ip` 类请求未进入预证书 SAN，或被丢弃）。

## 三、影响

- `public_endpoint` 为 **IP 直连形态**（无域名）的 agent 无法通过
  `serve_direct_partner` / 注册认领流程上线——被 wit 自身拦截（状态 `blocked`，
  端口不监听）；
- 规避路径：为 agent 配置 DNS 形态端点（需域名解析到服务器）。

## 四、期望

`_validate_public_endpoint` 校验前，材料生成器将
`AgentCertificateRequest.ip` 条目写入预证书 SAN（与 DNS 条目同通道）；
或 `issue_certificate_pair` 提供显式 `extra_sans` 参数。

## 五、更正后的处置（2026-10-25 实测）

### 已确认事实

- 用 `switch_step2` 对 ABKIJE 领证，CA 返回的 client/server 证书
  `subjectAltName` 只有 `URI:acps://1.2.156.3088.1.BUPT.VTF97R.ABKIJE.U8KHPP.149W`；
- 注册 ACS 的 `certificate` 是 `{}`，没有 `altNames`；
- ATR 规范明确 CA 从 ACS 的 `certificate.altNames` 取 SAN，**不读 CSR**；
- 因此 `_patch_ca_csr_san()` 对本平台 CA **无效**，保留它只对"读取 CSR SAN 的
  CA"有意义，不能作为本问题的修复。

### 修复进展

1. **已执行**：新建 ACS v1.1.0，`certificate` 写入
   ```json
   {"altNames": {"ip": ["8.160.176.215"]}}
   ```
   平台已 APPROVED，新 AIC
   `1.2.156.3088.1.BUPT.VTF97R.N9M0PW.ZB1CII.0B3X`；旧 ABKIJE（v1.0.0）弃用。
   下一步对新 AIC 跑 `switch_step2` 领证（CA 应签出 IP SAN），再按
   [workspace-partner.md](workspace-partner.md) 切身份。
2. 保留旧 ABKIJE 或改 DNS 端点均不再需要。

### 不要做的事

- 不要只改 wit 的 `_validate_public_endpoint` 跳过校验：CA 证书本身没有 IP SAN，
  平台/其他 agent 的 mTLS 客户端仍会拒绝，属于把故障推迟到连接阶段。
- 不要在 ABKIJE 记录上反复跑 `switch_step2`：每次都会登录共享平台账号，
  可能踢掉其他 agent 会话，且结果不会变。

`deploy/discovery_diagnostics/switch_step2_try_ca_issue.py` 保留复现参数与
"CA 返回证书 SAN" 的诊断能力；1.1.0 已注册，下一步是对新 AIC 领证并切换。
