# PEP 设计文档：基于 Admission Hook 的 AIP 委托授权层

> 版本 v1.1（2026-09-29 回改）· 适用于研枢集群（wit 2.1.0 + acps_sdk 2.2.0）
> 定位：对 AIP 协议的二次开发——为 AIP 增补 AAC 第 10 册定义但从未落地的委托授权语义。
>
> **⚠ 实现状态以 `PEP-result.md` 为准**：阶段一/二已实现并本地验证（66 单测 + demo 16/16 +
> 真实 LLM 业务闭环）。本文 v1.1 已回改**安全关键与事实性错误**（v1.0 的三处⛔：拒绝不是
> JSON-RPC 错误、ACS 无 public 字段、cnf 无法在 admission 点校验；外加签发者授权检查、
> peer_verified 前置、jti 一次性语义、TTL=600、x5c 只带 leaf）；其余 11 处实现期偏差的
> 完整清单见 PEP-result.md §6/§11/§13，不在此逐条重复。**不要只读本文评判实现**。

---

## 1. 背景与问题

### 1.1 已核实的协议现状

| 层 | 现状 | 证据 |
|---|---|---|
| wit 框架 | 提供 `AipPartnerAdmissionHooks` 准入钩子（身份验证后、业务执行前），但**默认 accept-all**，无任何授权策略 | `wit/hooks/acps/admission.pyi`: "With no such hook configured, the Partner retains the framework's default accept behavior" |
| wit 身份绑定 | 直连模式从 mTLS 证书 `acps://` URI SAN 强制提取调用方 AIC，拒绝 senderId 回退——**伪造身份已堵死** | `wit/kernel/runtime.pyi`: `_direct_peer_evidence` |
| AIP 协议 | 消息模型（TaskCommand/TaskResult/Product）无委托、授权、数据血缘表达 | `acps_sdk/aip/aip_base_model.py` |
| AAC 规范 | 定义了 Delegation Token / Actor Chain / PEP/PDP / Fail-Closed，但 SDK 零实现 | ACPs-spec-AAC §6.4/§10/§11.3 |

### 1.2 真实漏洞（演示第一幕）

任何持 CA 有效证书的 agent，可绕过 Supervisor 直连任意 partner 的 `/rpc` 端点发送 `TaskCommand(start)`，partner 照单执行——**无论该 skill 是否声明为公开**。mTLS 只证明"你是注册 agent"，不证明"你有权调我"。对公开 skill 而言"执行"尚有 ACS 声明背书；对非公开/高成本 skill，这是纯授权缺口。

### 1.3 设计目标

1. **G1 准入强制**：partner 对**非公开 skill** 只接受（a）白名单 leader 的直调，或（b）携带有效 delegation token 的调用，其余一律拒绝（fail-closed）；公开 skill（ACS `public=True`）继续向持证注册 agent 开放（见 §4.2.1 三级策略）。
2. **G2 预授权委托**：leader 可向 partner 颁发有时效、有范围、绑会话的 token，partner 持 token 直连第三方 partner 完成子任务，不经 leader 中转。
3. **G3 可审计**：每次准入裁决记审计日志（主体、actor、决策、jti）；对外错误消息遵循 AAC §12（通用消息，不泄露策略细节）。
4. **G4 零侵入**：不改 wit / acps_sdk 源码，全部通过 `aip_hooks`、dataItems 约定与 monkey-patch 级别的业务侧代码实现。

### 1.4 非目标

- 不实现完整 PDP（策略引擎）——PEP 内嵌简单策略表即可，PDP 接口预留。
- 不覆盖群组/MQ 广播场景（AAC §11.2 的 per-recipient envelope 留作扩展）。
- 不做 Product 数据血缘字段（另行立项）。

---

## 2. 总体架构

```
┌─────────── Supervisor (Leader) ───────────┐
│  本地工具 issue_delegation_token (代码工具) │
│  私钥: ws-supervisor/acps/client/          │
│        private-key.pem (Ed25519)           │
└──────┬──────────────────────┬──────────────┘
       │ ①白名单直调           │ ②continue 附带 token
       │ (现状不变)            │ (remedy-3 扩展)
       ▼                      ▼
┌─────────── Partner A (如 Citation-Integrity) ───┐
│  持 token 经自身 runtime.aip_delegate 直连 ──────┼──┐
└──────────────────────────────────────────────────┘  │
       ┌────────────────────────────────────────────┘
       ▼ ③TaskCommand(start) + token dataItem
┌─────────── Partner B (如 Scout) ──────────────────┐
│  AipPartnerAdmissionHooks (PEP)                   │
│   ├ 验 token: 签名/aud/sub/exp/scope/jti          │
│   ├ 白名单 leader: 直接放行                        │
│   ├ 记审计日志 → 允许/拒绝                          │
└───────────────────────────────────────────────────┘
```

**关键设计决策**（前期讨论已定）：

- **token 无状态可验证**：leader 用自己的证书私钥签名，验证方本地验签，无需预先联络目标 partner——不存在引导开销。
- **不加 need_connect 状态**：partner 遇阻只报 `awaiting-input`（报"缺什么"），满足方式由 leader 三选一：给数据 / 中转代取 / **发 token（本设计新增的 remedy-3）**。
- **角色是会话属性**：PEP 不维护"我的 leader 是谁"的静态配置，授权由白名单（环境变量）+ 会话登记表 + token 共同动态决定。partner A 调 partner B 时以自身 runtime 的 leader 角色发起（wit 的 `aip_delegate` 任何 agent 均可用），B 的 PEP 只认 token，不认"角色名"。

### 2.1 动机闭环：为什么是"leader 授权的隧道"而不是另外两条路

中转模式（现状）与 partner 自主发现模式各有死结，隧道模式同时解开：

| 模式 | 问题 |
|---|---|
| leader 中转（现状） | ① 每次补给多一轮 LLM 调用；② 长任务下 leader 上下文被大量材料搬运占据；③ 需求经 leader 转译可能失真 |
| partner 自主发现 | ① partner 无法归因：分不清信息不足是 leader 没给全还是上游 partner 没给全，因此无法判断该报 awaiting-input 还是去网络找 agent；② ADP 发现流程对单个 partner 更慢 |
| **leader 授权隧道（本设计）** | 归因与选址由唯一有名册的一方（leader）承担；需求不经过 leader 转译（结构化 need 直达目标 partner）；名册内 agent 零发现成本，名册外 agent 由 leader 走一次 ADP 发现（与现状成本相同，仅一次）。**token 为一次性准入（jti 单次消费），多次调用 = leader 重新签发（本地零成本操作）**——配额语义即"token 张数"，是否再签一票由 leader 归因决策树（§2.3）治理；一般化的 `acps_max_uses` 计数留作 CDAP 配额扩展 |

角色分工：**partner 报缺口（知需求不知供给），leader 归因+选址（知供给不知需求细节），token 即隧道**（临时性由 exp 表达，任务结束自动失效）。

### 2.2 awaiting-input 的结构化 need schema

隧道收益（转译损耗归零、leader 上下文不被材料挤占）兑现的前提是缺口描述本身结构化——否则 leader 仍需 LLM 理解自然语言需求，转译只是换了位置。约定 awaiting-input 的 dataItems 携带：

```json
{"type": "data", "data": {
  "need": "literature.search",
  "query": "graph attention network citation networks",
  "expected": "20 条，每条含标题+链接+摘要要点",
  "reason": "大纲第 3 节证据不足",
  "blocking": true
}, "metadata": {"role": "need-report"}}
```

leader 侧 `need → skill id → 名册 agent` 的映射走代码规则（同 plan_gate 模式），选址全程可不过 LLM；材料流（如 20 篇文献）从目标 partner 直达受托 partner，leader 上下文只增加"报缺→发 token→继续"一行记录。

### 2.3 leader 的归因决策树

```
awaiting-input(结构化 need)
├─ 缺口源于上游某步的产出质量问题（如只给了 abstract）
│    → 重派该上游 partner（现有纪律：回到那一步、点名要求补全，不发 token）
├─ 缺口可由名册内 partner 的 skill 满足
│    ├─ 量小 / 一次性 → leader 中转代取后 continue（现状，仍最优）
│    └─ 量大 / 多次 / 需并行 → 签发 token，continue 附带（本设计 remedy-3）
└─ 无任何来源 → continue 告知"尽力产出，缺口标注 [需补充证据]"
```

---

## 3. Delegation Token 规范（协议扩展核心）

### 3.1 格式

JWS 紧凑序列化（`header.payload.signature`），签名算法 **Ed25519**（与集群 mTLS 证书算法一致）。

```json
// header
{"alg": "EdDSA", "typ": "JWT", "x5c": ["<leader 证书 DER 的 base64（标准字母表）>"]}
```

`x5c` **只携带 leader 叶子证书**（RFC 7515：base64 标准字母表编码的 DER）——中间证书由验证方本地 trust-bundle 提供，token 缩小 ~0.7K 字符（实测 3234→2548）。**硬依赖：验证方 trust-bundle 必须包含签发中间 CA**（当前集群 bundle = Root + Infrastructure Intermediate + Agent Intermediate，天然满足）；若未来收窄为 root-only bundle，签发侧用 `include_intermediates` 逃生门退回整链。无论哪种形态，验证都无需向 Registry 查询签发者证书（与 mTLS 同一信任锚）。

```json
// payload (claims) —— 字段名与语义对齐 AAC §10.2 必需字段 + §10.3 推荐字段
{
  // ── §10.2 必需字段 ──
  "iss": "agent:1.2.156.3088.1.1.XXXXXX...",  // 签发 leader（本设计 leader 兼任 issuer，未引入 STS）
  "sub": "agent:1.2.156.3088.1.1.XXXXXX...",  // primary subject = leader（代表谁执行；AAC 语义，非受托人）
  "aud": "agent:1.2.156.3088.1.1.ZZZZZZ...",  // canonical audience = 目标 partner B
  "exp": 1790000600,                          // 建议时效 ≤ 10 分钟
  "iat": 1790000000,
  "jti": "uuid4",                             // 审计 / 撤销 / 防重放
  "scope": "acps.skill.invoke:arxiv.search",  // AAC 命名空间写法（§10.4 示例同款）
  "act": {                                    // 受托人放这里（§10.4），最外层 act 必须 == mTLS 对端 AIC
    "sub": "agent:1.2.156.3088.1.1.YYYYYY...",  // 受托 partner A（token 持有者）
    "aic": "1.2.156.3088.1.1.YYYYYY...",
    "act": {                                  // 内层 act = 历史 actor（leader），供审计
      "sub": "agent:1.2.156.3088.1.1.XXXXXX...",
      "aic": "1.2.156.3088.1.1.XXXXXX..."
    }
  },

  // ── §10.3 推荐字段（本设计采用的子集）──
  "acps_subject_type": "agent",
  "acps_target_aic": "1.2.156.3088.1.1.ZZZZZZ...",  // 目标 partner B 的 AIC（显式，替代自造 targetSkillHint）
  "acps_skill_id": "arxiv.search",                  // 本 token 授权的 skill（显式，替代自造 scope 白名单数组）
  "acps_delegation_id": "dlg-<uuid>",               // 委托链路 ID（审计关联）
  "acps_delegation_mode": "fixed",                  // 固定链路：leader→A→B
  "acps_allowed_route": [                           // §10.6：允许的 actor 路径
    "agent:1.2.156.3088.1.1.XXXXXX...",
    "agent:1.2.156.3088.1.1.YYYYYY...",
    "agent:1.2.156.3088.1.1.ZZZZZZ..."
  ],
  "acps_chain_depth": 1,
  "acps_max_chain_depth": 2,
  "cnf": {"x5t#S256": "<受托 partner A 证书的 SHA-256 指纹>"},  // §10.3/§11.3 证书级绑定（消除 act 等价绑定的偏差）

  // ── 本设计的扩展 claims（AAC 未覆盖，需在草案文档中注明偏差）──
  "sessionId": "<发起方的 AIP sessionId>",   // 会话绑定（AIP 侧概念，AAC 无对应字段）
  "parentTaskId": "<A 在 B 处所属任务的 taskId>"  // 任务血缘（同上）
}
```

语义约束（对齐 AAC §6.4 / §10）：

- **actor 绑定（§10.4）**：最外层 `act`（受托 partner A）必须等于 mTLS 对端 AIC——token 只能由受托者本人使用，防转借；内层 `act` 是历史 actor，供审计与风控，不自动赋权；
- **audience 隔离（§10.5）**：`aud` 与 `acps_target_aic` 必须都等于目标 partner 的 canonical audience——给 Partner1 的 token 不得用于 Partner2；
- **scope 收窄（§10.5）**：若未来允许 A 继续向下转委托（A 给 C 签发下一跳 token），新 token 必须 `scope <= 原 scope`、`exp <= 原 exp`、`chain_depth + 1`、新 `jti`；当前版本 max_chain_depth=2 且 PEP 拒绝链深超限，即暂不支持二级转委托；
- **不冒充**：A 调 B 时 senderId 仍是 A 自己的 AIC，`act` 表达的是"leader 委托 A 代表其执行"——对应 AAC "Delegation, not Impersonation"（§10.4 的示例正是 leader→A→B 两级 act）。

### 3.2 token 的传递通道（不违反 AAC §11.2）

AAC §11.2 只禁止**群组/MQ 广播**携带 bearer token；点对点通道合法。本设计使用两条点对点通道：

1. **remedy-3 下发**：leader 的 `continue` 命令 dataItems 中附加一个 `StructuredDataItem`：
   ```json
   {"type": "data", "data": {
      "acpsDelegationToken": "<JWS>",
      "targetAic": "<目标 partner AIC>",
      "targetSkillHint": "arxiv.search",
      "usage": "持此令牌直接向目标伙伴发起任务，勿回传给 leader"
   }, "metadata": {"role": "delegation-token"}}
   ```
2. **start 预挂**：可预见依赖时，leader 在 `start` 的 dataItems 里直接附带，省一轮 awaiting-input。

约定：`metadata.role == "delegation-token"` 的 dataItem **不进入模型提示词**——由 PEP/工具层在 `sanitize_command_for_prompt` 之前剥离（wit 已有该函数，我们在 transform hook 中拦截），防止 token 泄露进 LLM 上下文被提示注入利用。

---

## 4. Partner 侧：PEP 实现

### 4.1 模块结构

```
acps_cluster/
├── delegation/
│   ├── __init__.py
│   ├── token.py        # 签发 issue() / 验证 verify()（纯函数，无状态）
│   ├── pep.py          # AipPartnerAdmissionHooks 子类 + 策略表
│   ├── audit.py        # 审计日志（JSONL 追加写）
│   ├── client.py       # 受托方直连工具（partner 侧出站调用）
│   └── issuer_tool.py  # leader 侧本地签发工具（给 Supervisor 用）
```

### 4.2 pep.py 核心逻辑

```python
from wit.hooks.acps.admission import (
    AipPartnerAdmissionHooks, AipPartnerAdmissionDecision, AipPartnerAdmissionRequest,
)
from .token import verify_token, TokenError
from .audit import audit_log

PEP_VERSION = "yanshu.pep.v1"

async def pep_decide(req: AipPartnerAdmissionRequest) -> AipPartnerAdmissionDecision:
    # ── §11.3 步骤 1/2（框架已完成）：mTLS 对端证书 → peer AIC；AIP senderId 绑定 ──
    # req.leader_aic 即验证过的 peer AIC（wit 严格模式强制证书 SAN 提取，拒绝 senderId 回退）
    # 本地补充：TLS server AIC == 本机 AIC（隐含成立，wit 监听即本机证书）

    # 前置：peer_verified 为假一律拒绝（fail-closed，含公开 skill）。
    # 直连严格模式下框架恒传 True；防的是未来其他 delivery（notification/group）
    # 允许 senderId 回退时，白名单与 act 绑定建立在未验证身份上。
    if not req.peer_verified:
        audit_log(req, decision="deny", rule="peer-not-verified")
        return AipPartnerAdmissionDecision.reject("authentication required")

    # 策略 0：公开 skill 免 token（保住社区互联线，见 §4.2.1）
    if _requested_skill_is_public(req):
        audit_log(req, decision="allow", rule="public-skill")
        return AipPartnerAdmissionDecision.accept()

    # 策略 1：白名单 leader（环境变量 ACPS_PEP_LEADER_AICS，逗号分隔）
    if req.leader_aic in LEADER_ALLOWLIST:
        audit_log(req, decision="allow", rule="leader-allowlist")
        return AipPartnerAdmissionDecision.accept()

    # ── §11.3 步骤 3：token 校验（顺序对齐规范子序列）──
    token = _extract_token(req.data_items)      # 按 §3.2 约定查找
    if token is None:
        audit_log(req, decision="deny", rule="no-credential")
        return AipPartnerAdmissionDecision.reject("authentication required")

    try:
        # 3a issuer/signature（x5c 链 + trust-bundle 验签）
        #    内含「iss 必须等于 x5c 叶子证书 AIC」（2026-09-28 安全修复一）：签名只证明
        #    "某个受信证书持有者签的"，不证明"他就是 iss 声称的那个人"——没有这条，
        #    社区内任何 agent 都能签出 iss=leader 的 token
        # 3b canonical audience（aud 与 acps_target_aic == 本机 AIC，两者交叉核对）/ expiry / scope
        # 3c revocation：显式偏差——无 CRL/introspection 通道，以自包含 token + ≤10min exp 替代撤销
        #    （短期 token 的暴露窗口即撤销上界，见 §4.2.2 偏差声明）
        claims = verify_token(
            token,
            trust_bundle=TRUST_BUNDLE,
            expected_aud=MY_AIC,
            now=time.time(),
        )
    except TokenError as e:
        # 统一对外错误（AAC §12）：不区分过期/签名错/aud 错；错误码只进审计
        audit_log(req, decision="deny", rule="invalid-token", reason=str(e))
        return AipPartnerAdmissionDecision.reject("invalid access token")

    # 3a' 签发者授权（2026-09-28 安全修复二）——「证书可信」≠「有权签发」：
    #     iss 必须落在白名单（否则受信社区成员自签 iss=自己的 token 可通过全部其余校验，
    #     L2 等价于没有授权）。**必须在 jti 认领之前**：不可信签发者若能烧掉 jti，
    #     等于白送一个 DoS 面。此检查即未来 CDAP 域章程/资产策略的插件点：
    #     白名单是签发权表的单机退化形式。
    if normalize(claims["iss"]) not in LEADER_ALLOWLIST:
        audit_log(req, decision="deny", rule="issuer-not-authorized")
        return AipPartnerAdmissionDecision.reject("invalid access token")

    # 3d jti / replay（规范序在 act 之前）——一次性准入：每枚 token 只放行一次，
    #     多次调用 = leader 重新签发（配额语义即 token 张数，见 §2.1）
    if not jti_registry.claim(claims["jti"], ttl=claims["exp"] - claims["iat"]):
        audit_log(req, decision="deny", rule="replay")
        return AipPartnerAdmissionDecision.reject("invalid access token")

    # 3e act 与 peer AIC 绑定（受托人 == 实际调用方；主体级绑定，见 §4.2.2）
    if claims["act"]["aic"] != req.leader_aic:
        audit_log(req, decision="deny", rule="act-peer-mismatch")
        return AipPartnerAdmissionDecision.reject("authorization failed")

    # 3f cnf 与 mTLS client certificate 绑定——⛔ 实测不可行：admission 请求不暴露对端
    #    证书（只有 leader_aic/partner_aic/delivery/peer_verified 等），证书指纹无从校验。
    #    以 3e 的主体级绑定等价替代；cnf 若存在只记审计标记 cnfUnverifiable（见 §4.2.2）

    # 3g 固定链路模式：当前 hop 与 acps_allowed_route 对齐
    #    当前 hop = (调用方 AIC, 本机 AIC)，必须出现在 route 的相邻位置
    if claims.get("acps_delegation_mode") == "fixed":
        route = [a.split(":")[-1] for a in claims.get("acps_allowed_route", [])]
        hop = (req.leader_aic, MY_AIC)
        if not _is_adjacent_in_route(route, hop):
            audit_log(req, decision="deny", rule="route-not-allowed")
            return AipPartnerAdmissionDecision.reject("authorization failed")

    # 3h 会话绑定（可选 claim，非 §11.3 条目）：有则必须相等，无则跳过——
    #    remedy-3 流程下 leader 无法预知 A→B 的 sessionId（B/coordinator 侧生成），
    #    会话只能做审计线索，不能做授权边界（ACPS_PEP_REQUIRE_SESSION_BINDING 可强制）
    if claims.get("sessionId") and claims["sessionId"] != req.session_id:
        audit_log(req, decision="deny", rule="session-mismatch")
        return AipPartnerAdmissionDecision.reject("authorization failed")

    # ── §11.3 步骤 4：解析资源与动作 ──
    # 4a 目标 skillId == acps_skill_id（token 授权的 skill，显式字段）
    if _requested_skill(req) is not None and _requested_skill(req) != claims["acps_skill_id"]:
        audit_log(req, decision="deny", rule="skill-mismatch")
        return AipPartnerAdmissionDecision.reject("authorization failed")

    # 4b 链深（固定模式当前版本 max=2 且 A 不得转委托：chain_depth == max 即拒）
    if claims.get("acps_chain_depth", 0) >= claims.get("acps_max_chain_depth", 2):
        audit_log(req, decision="deny", rule="chain-depth-exceeded")
        return AipPartnerAdmissionDecision.reject("authorization failed")

    # ── §11.3 步骤 5/6：构造 AuthorizationRequest → PDP ──
    # 当前版本无独立 PDP：内嵌策略表（L0/L1/L2 三级 + token 校验）即裁决逻辑，
    # PDP 接口预留（见阶段四）。allow → 进入业务 handler。

    audit_log(req, decision="allow", rule="delegation-token",
              jti=claims["jti"], delegation_id=claims.get("acps_delegation_id"))
    return AipPartnerAdmissionDecision.accept()
```

校验顺序逐条对齐 AAC §11.3：⓪peer_verified 前置【实现加固】→ ①传输层身份（框架）→ ②协议身份绑定（框架）→ ③token（issuer/签名【含 iss==叶子 AIC】→ **签发者授权【白名单，jti 之前】** → audience/expiry/scope → revocation【显式偏差】 → jti/replay（一次性） → act 绑定 → cnf【⛔无法校验，等价声明】 → allowed_route 对齐 → 会话绑定【可选，仅审计线索】）→ ④资源动作（skillId → 链深）→ ⑤⑥内嵌 PDP → ⑦allow 进 handler → ⑧deny 记审计。任一失败即 fail-closed（AAC §5）；admission 外层另有 catch-all 兜底——任何未预期异常收敛为「拒绝 + 审计 + §12 通用文案」，绝不裸穿到 RPC 层（wit 的 admission 分发无 try/except，实测确认）。

**jti 防重放**：一次性准入语义——每枚 token 只放行一次（多次调用 = leader 重新签发，配额即 token 张数，见 §2.1）。进程内 `{jti: exp}` 字典 + 定期清理即可（演示规模足够）；生产可换 sqlite（复用 `AipTaskStore` 的库）。一般化的 `acps_max_uses` 计数（n>1）留作 CDAP 配额扩展，不建议用 (jti, sessionId) 会话绑定放宽——会话在本流程不可预测（见 3h）。

#### 4.2.2 与 §11.3 的两处显式偏差（草案文档必须声明）

1. **revocation/active status 无通道**：我们无 CRL 消费与 introspection 端点。替代方案：token 自包含 + `exp ≤ 10min`——短期 token 的暴露窗口即为撤销上界；配合 jti 一次性消费（每枚 token 每个任务只准入一次），重放面进一步收窄。阶段四可加 sqlite jti 黑名单作为主动撤销的降级通道。
2. **cnf 绑定以 act 等价实现，证书级绑定不可行（⛔ v1.0 判断错误，v1.1 更正）**：§11.3 明列 "cnf 与 mTLS client certificate 绑定"，但实测 `AipPartnerAdmissionRequest` **不暴露对端证书**（只有 task_id/session_id/leader_aic/partner_aic/delivery/peer_verified/data_items/command_params），指纹无从校验——v1.0 写的"指纹从 TLS 层取、成本低、建议做进阶段一"做不到，须等 wit 扩展该请求对象。已实现的是 `act.aic == peer AIC` 的**主体级**绑定（足以阻止转借：A 的 token 只能由 A 的证书使用）；签发侧仍写 `cnf`（供未来校验），PEP 侧遇到只记审计标记 `cnfUnverifiable`、不拒绝。

#### 4.2.1 三级准入策略与社区互联线

纯 fail-closed（只放白名单 + token）会切断集群当前的社区价值线：partner 的公开 skill（`@capability(public=True)`，ACS 里对全网声明）目前**允许任何注册 agent 经发现调用**（实测社区外部 agent 调用过），这正是 ACPs "互联"的生态意义。因此准入策略为三级：

| 级 | 条件 | 依据 |
|---|---|---|
| L0 公开能力 | 请求的 skill 在**本机 capability 声明**中 `public=True`，且调用方持有效 CA 证书且 peer_verified（⛔ v1.0 写"查本机 ACS"——实测 ACS 无 `public` 字段，`@capability(public=True)` 不投影进 ACS，可见性只能取本机声明） | 对齐 ADP 可发现边界（AAC §8.6）；`public` 目前是纯本地注解，无线上语义 |
| L1 白名单 leader | 调用方 AIC 在 `ACPS_PEP_LEADER_AICS` | 集群内部编排关系；同时是 L2 的**签发者白名单**（3a'） |
| L2 委托 token | 有效 delegation token（含签发者授权），且 skill == `acps_skill_id` | 本设计 |

另有一条实现期补的规则（v1.0 未定义，天真实现会架空 G1）：**请求未指名 skill 时**按 L0 放行，仅当本机**全部** capability 都声明为公开（且确有公开能力）；一旦存在非公开能力，未指名 skill 的请求即 fail-closed 走 L1/L2——否则任何非白名单调用方只要不声明 skill 就能进。副作用：混合可见性的 partner 会关闭其"匿名自由文本入口"。

**行为变更预警（必须提前告知）**：开启 PEP 后，非公开 skill 的外部调用会从"默认执行"变为"拒绝"。当前各 partner 的 skill 均 `public=True`，故社区外部调用**实际不受影响**；未来新增非公开/高成本 skill 时，外部调用者需按 L2 获取 token。审计日志从 PEP 开启起覆盖全部三级裁决（此前外部调用无审计——这本身是补上的可观测性缺口）。

### 4.3 注册方式（零侵入）

> **环境变量全景(2026-10-06 审查校订,代码实际读取 10 个)**:`ACPS_PEP_ENABLED` /
> `ACPS_PEP_LEADER_AICS` / `ACPS_PEP_AUDIT_PATH` / `ACPS_PEP_DOMAINS_DIR` /
> `ACPS_PEP_QUOTA_PATH` / `ACPS_PEP_MAX_TOKEN_TTL` / `ACPS_PEP_MY_AIC` /
> `ACPS_PEP_TRUST_BUNDLE` / `ACPS_PEP_REQUIRE_SESSION_BINDING` / `ACPS_PEP_UNSCOPED_PUBLIC`。
> 本文其余小节若只列了其中一部分,以此清单为准。


每个 partner 的 `build_xxx_agent()` 增加一行：

```python
# partner_scout.py 等
from delegation.pep import build_pep_hook

def build_scout_agent(provider) -> WitAgent:
    return WitAgent(
        ...,
        aip_hooks=[build_pep_hook()],   # ← 新增；与现有 aip_guard 的 transform hook 并列
    )
```

配置经环境变量注入（沿用 cluster_common 的惯例）：

| 变量 | 含义 | 默认 |
|---|---|---|
| `ACPS_PEP_ENABLED` | PEP 总开关（灰度用，关=回退默认 accept） | `0`（先观察，再全量开） |
| `ACPS_PEP_LEADER_AICS` | 白名单 leader AIC 列表 | supervisor 的 AIC |
| `ACPS_PEP_TRUST_BUNDLE` | CA 信任束路径 | `<workspace>/acps/trust-bundle.pem` |
| `ACPS_PEP_MY_AIC` | 本机 AIC（aud 校验用） | 从 enrollment.json 读取 |

**部署注意**：wit 的 admission 钩子在**协议身份验证之后**触发（pyi 明确），无需额外中间件。**拒绝的返回形态（⛔ v1.0 判断错误，v1.1 按实测更正）**：不是 JSON-RPC error，而是 HTTP 200 + `TaskResult.status.state = "rejected"` 建档返回，§12 通用文案放在 `status.dataItems[0].text`；协议层不携带 `-32008/-32009/-32010`，错误码只进审计记录（便于对号 §12）。另：admission 只在 `start` 时裁决一次，`continue` 不再过准入（但任务查键绑定同一对端 AIC，发送者无法冒换）。

### 4.4 受托方出站调用（client.py）

partner A（如未来的 Citation-Integrity）收到 token 后如何直连 B？**复用 wit 自带能力**：`WitRuntime.aip_delegate(partner_aic=..., request=...)` 对任何 agent 可用（wit 2.1.0 已内置 `AipDelegationHooks`）。我们只需包一层工具：

```python
@capability(id="direct.fetch", description="持委托令牌直接向目标伙伴检索文献", public=True, tags=["delegation"])
async def direct_fetch(target_aic: str, query: str, token: str) -> list[dict]:
    # 1. 用 token 构造含 delegation-token dataItem 的请求
    # 2. runtime.aip_delegate(...) 走 mTLS 直连
    # 3. 返回前用 aip_guard 同款外壳包裹结果（防提示注入）
```

token 从任务 dataItems 中由代码侧剥离后注入工具参数，**不经过 LLM 复述**（LLM 只看到"有一个可用的授权令牌，target=X"，拿不到 JWS 原文——防止 token 被写进草稿泄露）。

---

## 5. Leader 侧：签发工具

### 5.1 issuer_tool.py

Supervisor 已有 `plan_gate` / `product_gate` 本地代码工具的先例，签发照此模式做成本地工具：

```python
from wit import Tool

def build_issue_token_tool(trust: SigningContext) -> Tool:
    """本地工具 issue_delegation_token(delegate_aic, target_aic, skills, session_id, parent_task_id, ttl_seconds=300)
    → 返回 {ok, token, expiresAt}。私钥从 ws-supervisor/acps/client/private-key.pem 加载，进程内缓存。"""
```

### 5.2 Supervisor 剧本扩展（提示词纪律追加）

在 `_DELEGATION_DISCIPLINE` 中 awaiting-input 处按 §2.3 决策树扩展：

> · 伙伴报缺时先归因：缺口源于上游某步产出质量（如资料缺摘要）→ 重派该上游伙伴补全；
> · 缺口是任务范围外的新需求且量大/需多次检索：若名册内伙伴的 skill 可满足 → 调用本地工具
> `issue_delegation_token` 签发受托令牌，用 acps_task 的 continue 把令牌发给等待中的伙伴，
> 要求它持令牌直接向目标伙伴完成检索；**令牌原文不得出现在你生成的任何文本里**；
> · 若名册内无可满足的伙伴 → 走 acps_discover 发现（与现状相同），发现成功后同样以 token 方式放行；
> · 量小的一次性补给仍可由你中转提供，不必一律发令牌。

### 5.3 平行扇出（G2 的性能收益落点）

中转模式下 leader 只能串行代取；remedy-3 下 partner A 可对多个目标同时 `aip_delegate`（wit 的异步任务天然支持），这是 demo 中量化对比的核心指标之一。注意扇出是**多目标**：`aud` 绑定单目标，每个目标一枚 token（leader 一次签发多枚即可，均为本地零成本操作），与 token 一次性准入（§2.1）不冲突。

---

## 6. 审计（audit.py）

JSONL 追加写 `ws-<partner>/logs/pep-audit.jsonl`，每行：

```json
{"ts": "2026-09-27T10:00:00Z", "decision": "deny", "rule": "invalid-token",
 "callerAic": "1.2...A", "myAic": "1.2...B", "taskId": "...", "sessionId": "...",
 "jti": "…", "reason": "token expired", "pepVersion": "yanshu.pep.v1"}
```

遵循 AAC §13：不记录完整 token，只记 jti 与原因码。对外响应永远是通用 `authorization failed`。

---

## 7. 实施计划

### 阶段一：token 与 PEP 核心（先行，可独立验收）

| 步骤 | 内容 | 产出 |
|---|---|---|
| 1.1 | `delegation/token.py`：Ed25519 签发/验签（cryptography 库，证书链嵌入 x5c，trust-bundle 验链） | 单测：签发→验证通过；篡改/过期/错 aud/错 sub 各一例拒绝 |
| 1.2 | `delegation/pep.py` + `audit.py`：admission hook、白名单、jti 登记 | 单测：五类请求（白名单/有效 token/无凭据/坏 token/重放）裁决正确 |
| 1.3 | 集成到 scout（第一个启用 PEP 的 partner），`ACPS_PEP_ENABLED=1` 灰度 | 冒烟：supervisor 正常链路不受影响 |

### 阶段二：漏洞复现与修复演示（比赛 demo 核心）

| 场景 | 操作 | 预期 |
|---|---|---|
| S1 漏洞复现 | `ACPS_PEP_ENABLED=0`，用 critic 的证书直连 scout 调一个**非公开** skill（demo 前将某个 scout skill 的 `public` 改为 False，如新增 `scholar.deep_fetch`） | 任务被执行（现状缺陷：公开与否都执行） |
| S2 PEP 生效 | 同上，`ACPS_PEP_ENABLED=1` | `task rejected` + `status.dataItems[0].text` 为 §12 通用文案（"Authentication required"）+ 审计行（**不是 JSON-RPC 错误**，见 §4.3 更正） |
| S2b 社区不受伤 | 外部 agent 证书直连 scout 的公开 skill（`arxiv.search`，`public=True`） | 正常执行 + 审计行（L0 放行，验证社区互联线未断） |
| S3 委托放行 | supervisor 签发 token（`acps_skill_id=scholar.deep_fetch`），critic 持 token 直连 scout | 任务执行 + 审计含 jti / acps_delegation_id + actor chain 记录 |
| S4 越界拒绝 | 过期 token / 篡改 payload / token 的 `acps_skill_id` 指向其他 skill / `act` 与调用方证书不符 / **`acps_allowed_route` 不含当前 hop** / 重放 jti / **受信社区成员自签 token（iss=自己，链有效）** / 签发者不在 trust-bundle / 会话绑定不符 | 全部 `task rejected` + §12 通用文案（错误码只进审计，对号 §12：token 类 -32010、act/route/策略类 -32009；`cnf` 用例因 admission 拿不到对端证书改为"accepted + cnfUnverifiable 审计标记"，见 §4.2.2） |

S1/S2/S3/S4 固化为 `delegation/demo_pep.py` 脚本（写法参照当年的 `delegate_smoke.py`——该文件 2026-10-04 已随 leader_cli 原型归档至 `archive/`），一键输出四段对比结果。

### 阶段三：真实用例接线

- 新增 Citation-Integrity partner（草稿逐条引用核验，天然需要持 token 批量调 scout）；
- supervisor 剧本加 remedy-3 纪律；
- 量化对比 demo：同一核验任务，中转模式 vs 委托模式的总时延、leader LLM 调用次数、并行度。

### 阶段四（时间允许）

- 协议扩展草案文档（RFC 风格：delegated dataItem 规范、token claims、PEP 校验序、错误码），作为"二次开发"的独立交付物；
- jti 登记落 sqlite、PDP 接口抽离。

### 风险与对策

| 风险 | 对策 |
|---|---|
| admission hook 触发时机与预期不符（pyi 与实现可能有偏差） | 阶段 1.3 先在 scout 灰度验证；`ACPS_PEP_ENABLED` 可随时回退 |
| token dataItem 意外进入模型提示词 | transform hook 剥离 + 单测断言提示词中无 JWS 字样 |
| leader 私钥读取权限 | 私钥仅 supervisor 进程可读（沿用现有文件权限）；签发走本地工具不暴露给任意 partner |
| Ed25519 证书算法与 x5c 链验证的兼容 | 验链用 trust-bundle 与 mTLS 同源；单测覆盖 |

---

## 8. 与规范的关系（答辩口径备忘）

- **AIP §2** 把"智能体主动委托任务"列为协作性目标——本设计是该目标在机制层的落地；
- **AAC §10.2/§10.4** token 必需字段与 `act` claim——本设计完整采用（sub=primary subject、act=受托人、最外层 act 与 mTLS 对端一致）；`sessionId/parentTaskId` 为 AIP 侧扩展 claims，草案文档中注明偏差；
- **AAC §10.3** 推荐字段——采用 `acps_target_aic / acps_skill_id / acps_delegation_id / acps_delegation_mode / acps_allowed_route / acps_chain_depth`，不自造等价字段；scope 采用 `acps.skill.invoke:<skill>` 命名空间写法；
- **AAC §10.5** audience 与 scope 收窄——二级转委托（A→C）按收窄规则预留，当前版本链深上限封死；
- **AAC §11.3** PEP 校验顺序——逐子步骤对齐（含 jti/replay 先于 act、skillId 解析与链深属步骤 4、固定模式 allowed_route 对齐），并在 jti 之前补了**签发者授权**检查（白名单，规范未要求但安全必需）；两处显式偏差见 §4.2.2（revocation 以短 exp 替代；cnf 在 admission 点无法校验、以 act 主体级绑定等价）；
- **AAC §12** 错误映射——**错误码只进审计不上线**（协议层拒绝形态是任务级 `rejected` + §12 通用文案，见 §4.3 更正）：缺凭据 `-32008`、token 类失败（签名/过期/aud/重放/**签发者未授权**）`-32010`、act/route/策略类失败 `-32009`；对外消息一律通用文本，细节进审计；
- **AAC §8.6** ACS/ADP 公开边界——L0 公开能力免 token，fail-closed 只作用于非公开 skill；注意 `public` 是本机 capability 注解、不投影进 ACS（§4.2.1 更正），"声明公开 = 向注册社区开放"目前是本实现的约定语义，成为线上声明需 ACS 契约扩展（另行立项）；
- **AAC §11.2** 禁止群组广播携带 bearer token——本设计仅用点对点 dataItems 通道，合规；
- **AAC §11.3** PEP 校验顺序——本设计逐条对齐（链→签名→时效→aud→actor 绑定→scope）；
- **AAC §12/§13** 错误不泄露策略细节、审计不记完整 token——均已遵循；
- wit 框架零改动，全部通过官方扩展点（`aip_hooks` admission/transform hooks）交付。


阶段四（加分项，非阻塞）： RFC 草案文档、jti 落 sqlite、PDP 接口抽离、中转 vs 委托的量化对比（时延/LLM 调用次数/并行度——这本来是 demo 的核心指标之一）