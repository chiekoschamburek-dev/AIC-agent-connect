# AIP 委托授权与协作域扩展草案

> 状态：内部草案 v0.1（2026-10-02 起草，2026-10-29 修订）· 未对外提交
> 关系：本草案是对 ACPs 协议体系中 **AIP**（交互）与 **AAC**（授权与访问控制）的扩展，
> 落地 AAC（ACPs 授权与访问控制分册）已定义但 SDK 未实现的委托授权语义，并增补其未覆盖的域级授权语义。
> 参考实现：研枢集群（wit 2.1.0 + acps_sdk 2.2.0，零侵入扩展点交付，189 项离线单测 +
> 三套演示全绿；实现记录见 `PEP-result.md`）。
>
> 约定语言：本文使用 **必须（MUST）/不得（MUST NOT）/应当（SHOULD）/可以（MAY）**。

---

## 1. 引言

### 1.1 问题

AIP/AAC 的现行能力回答了"连得上（ADP/ACS）、信得过（AIC/mTLS）"，但没有回答：

- **任务级**：智能体 A 代理领导 L 完成子任务时，如何在不经 L 中转的情况下，向伙伴 B
  证明"L 授权 A 就此任务调用 B 的能力 x"？（AAC §10 定义了委托语义，SDK 零实现。）
- **域级**：跨机构的受控资源共享（机构不愿公开、但需向合作方开放的高价值能力与数据）
  如何表达"谁有权授权、授权范围多大、如何撤销与对账"？（AAC 未定义任何域实体。）

### 1.2 两层结构

本草案分两部分，共享同一密码学内核（Ed25519 + x5c + 本地 trust-bundle 验证）：

| | Part A：任务级委托 | Part B：协作域 |
|---|---|---|
| 回答 | 授权**如何表达与强制执行** | 授权**由谁发起、范围谁定、如何撤销对账** |
| 载体 | Delegation Token（DT） | Domain Charter（章程）+ Grant Token（GT） |
| 时效 | 分钟级（≤10 分钟） | 订阅级（≤30 天） |
| 关系 | **GT 是根授权信封，DT 是信封内收窄的子授权**（§4.4） | |

### 1.3 术语

- **AIC**：智能体身份码（AIC 规范），取自 mTLS 证书 SAN 的 `acps://` URI，不可伪造。
- **PEP**：策略执行点，挂载于 partner 的准入钩子（身份验证后、业务执行前）。
- **签发者（issuer）/领导者（leader）**：持签发权的一方（单机形态为白名单，域形态由章程定义）。
- **受托方（delegate/consumer）**：持令牌直接调用资源的一方。

### 1.4 信任不变量

1. **证书可信 ≠ 有权使用**。mTLS 仅证明"经共认机构注册的智能体"；一切使用授权由本草案
   的令牌与策略表达。
2. **令牌原文（JWS）不得进入任何模型的上下文**（生成文本或提示词）。令牌经协议通道与
   代码侧传递；模型只可见脱敏句柄（§3.7）。
3. **fail-closed**：任一校验失败即拒绝；未预期异常必须收敛为拒绝+审计，不得裸穿传输层。

---

## 2. 角色与信任模型

```
签发者 L ──签发──► DT ──► 受托方 A ──持 DT 直连──► 资源方 B（PEP 裁决）
   ▲                                                    │
   │ Part B：章程（双签）+ GT（根信封）                    ▼
   └────────────────── 对账（双签用量报表）◄────── 使用记录（审计）
```

- 资源方 B 对其能力拥有**绝对主权**：裁决发生在 B 本地，依据是 B 本地持有的白名单/章程
  与可离线验证的令牌——不存在必须在线联络的权威点（无引导开销）。
- 身份与授权分离：身份只有一张 CA 证书；令牌是其上的授权层，**不得**触碰证书体系。

---

## 3. Part A：任务级委托令牌（DT）

### 3.1 格式

JWS 紧凑序列化，`alg=EdDSA`，`typ=JWT`。`x5c` 头部**必须**只携带叶子证书（base64 标准字母表
的 DER）；中间证书由验证方 trust-bundle 提供（硬依赖：**验证方 trust-bundle 必须包含签发
中间 CA**）。令牌**不得**携带信任锚。

### 3.2 Claims

| Claim | 必须 | 语义与约束 |
|---|---|---|
| `iss` | ✓ | 签发者，`agent:{AIC}`。**必须等于 x5c 叶子证书的 AIC**（签名只证明"某受信证书持有者签署"，不证明"他就是 iss 声称的人"——缺此条则社区内任何成员可冒充任意签发者） |
| `sub` | ✓ | 主授权对象（AAC §10.4：primary subject）。单级委托下 = iss |
| `aud` | ✓ | canonical 受众 `acps:agent:{目标伙伴 AIC}`。校验方对 `acps:agent:`/`agent:` 前缀宽容、AIC 部分严格 |
| `exp`/`iat` | ✓ | 时效。建议 ≤600s（MAY 上调，见 §7 偏差 1） |
| `jti` | ✓ | 一次性消费标识（§3.5） |
| `scope` | ✓ | `acps.skill.invoke:{skill_id}`，**必须**与 `acps_skill_id` 派生值一致（签发与验证双侧校验，不得为装饰性字段） |
| `act` | ✓ | 受托链：最外层 `act.aic` **必须**等于 mTLS 对端 AIC；内层 act 为历史 actor，仅供审计 |
| `acps_subject_type` | ✓ | 恒为 `agent`（AAC §10.3） |
| `acps_target_aic` | ✓ | **必须**与 `aud` 指向同一伙伴（交叉校验） |
| `acps_skill_id` | ✓ | 授权调用的能力 id |
| `acps_delegation_id` / `acps_delegation_mode` / `acps_allowed_route` / `acps_chain_depth` / `acps_max_chain_depth` | ✓ | AAC §10.3/§10.6 对齐项。固定链路模式下当前 hop (调用方 AIC, 本机 AIC) 必须出现在 route 相邻位置 |
| `sessionId` / `parentTaskId` | 可选 | 会话绑定**仅审计线索**（发起方无法预知对端会话 id，见偏差 5）；`parentTaskId` 仅血缘 |
| `cnf` | 可选 | 预留证书级绑定；当前 admission 点拿不到对端证书，存在时**必须**仅记审计标记 |
| `acps_domain` / `acps_domain_revision` | 可选 | Part B 派生时**必须**继承（§4.4） |

### 3.3 传递通道（delegated dataItem）

令牌经点对点 dataItems 传递（AAC §11.2：**不得**在群组/MQ 广播携带 bearer token）：

```json
{"type": "data",
 "data": {"acpsDelegationToken": "<JWS>", "targetAic": "<目标 AIC>", "targetSkillHint": "<skill>"},
 "metadata": {"role": "delegation-token"}}
```

处理要求：
- `metadata.role == "delegation-token"` 的数据项**不得**进入模型提示词。进入前**必须**由
  变换钩子剥离；**应当**以脱敏授权标记替换（对目标方）或落 host-only 的 local_context
  （对受托方），令牌原文全程只存在于代码侧。
- 授权标记（若实现替换）**必须**防伪造：任何调用方自带内容中出现标记哨兵（文本部件或
  结构化部件的渲染形态）一律丢弃——标记的信任来源是"本机钩子生成"，不是调用方文本。

### 3.4 验证算法（PEP 裁决序）

实现**必须**按以下顺序裁决（①–⑥ 对齐 AAC §11.3，⓪/⓪′/⓪″ 为本草案增补）：

```
⓪ 传输层身份已验证？（peer_verified 为假 → 拒绝，含公开能力）
⓪′ [未验签预检] 令牌声明 acps_domain 且：域未知 → 拒绝；域状态非 active → 拒绝
   （冻结是急停：不做密码学开销；基于未验签 claim 分支是安全的——只会更拒，不会 fail-open）
①② 框架已完成：mTLS 对端证书 → peer AIC；AIP senderId 绑定
③ 令牌验签（grant 画像走 §4.3 独立入口；delegation 画像继续）：
   3a  链/签名/叶子公钥类型；iss == 叶子 AIC
   3a′ 签发者授权：iss ∈ 白名单 ∪ 域签发者（**必须**先于 jti 认领，否则不可信签发者可烧毁 jti）
   3a″ [域派生] peer 是域成员且角色被允许调用 (本机, acps_skill_id) 资产
   3b  aud（与 acps_target_aic 交叉）/ 时效 / TTL / scope 一致
   3c  [偏差] 撤销：无 CRL/introspection，以短 exp 为上界（§7 偏差 1）
   3d  jti 一次性消费
   3e  act.aic == peer AIC
   3f  [偏差] cnf：等价以 3e 主体级绑定实现
   3g  固定链路 hop 相邻校验
   3h  sessionId（存在则必须相等；仅审计线索）
④ 资源动作：skillId（调用方自报，声明式——见偏差 6）与 acps_skill_id 一致；链深上限
④′ 执行侧核对（应当）：业务 handler 执行前应核对"实际执行的 skill == 准入裁决所依据的
   skillId"。skillId 只能约束声明值，模型实际调用的工具可能与声明不符；执行侧核对
   （实现形态：工具调用包装钩子对账本任务的准入记录）构成纵深第二道。非诚实调用方的
   主防线仍是白名单与令牌——本条不改变偏差 6 的定性，只收窄其暴露面
⑤⑥ 内嵌策略（PEP 兼 PDP）；allow → 业务；deny → 审计
⑦ 外层兜底：任何未预期异常收敛为拒绝 + 审计 + 通用文案，不得裸穿传输层
```

拒绝的线上形态：任务以 `rejected` 状态建档返回，`status.dataItems[0].text` 为通用文案
（§3.6）；协议层**不**携带错误码。

### 3.5 一次性语义

DT 为**单次准入**令牌：每枚 jti 只放行一次。多次调用 = 签发者重新签发（本地零成本）。
配额语义即"token 张数"。一般化形式 `acps_max_uses`（n>1）属 Part B 的 GT（§4.7）。
会话绑定（(jti, sessionId) 组合放行）**不得**用于放宽一次性——会话 id 在本流程不可预测
（偏差 5）。

### 3.6 错误语义

对外消息**必须**为通用文案（AAC §12：不泄露策略细节）；细分原因码只进审计：

| 审计码 | 线上文案 | 说明 |
|---|---|---|
| -32008 | Authentication required | 缺凭据 / peer 未验证 |
| -32010 | Invalid access token | 验签、时效、aud、scope、jti 重放、签发者未授权、域未知 |
| -32009 | Authorization failed | act/route/skill/策略类失败、域冻结、成员/角色、配额用尽 |

### 3.7 签发侧（句柄方案）

签发工具的返回值**不得**包含 JWS 原文（工具结果会回灌模型，等价于令牌进上下文）。
**必须**实现句柄机制：签发入库（进程内 jti→JWS），模型只获 `{jti, expiresAt, audience,
skillId, delegateAic, targetAic}`；下发动作由代码侧按句柄取出原文、组装 §3.3 数据项并
发出。库中条目一次性消费；重发 = 重新签发。

---

## 4. Part B：协作域

### 4.1 实体

| 实体 | 职责 |
|---|---|
| Domain | 多方协作边界：成员、资产、签发者、状态、期限 |
| Member | 域内主体：`{aic, role}`，role ∈ provider/consumer/auditor |
| Asset | 域内受控能力：`{providerAic, skill, allowedMembers(角色集)}` |
| Grant Token (GT) | 域级授权凭证（根信封） |
| Use Record | 使用记录（审计），对账输入 |

### 4.2 域章程（Charter）

一份签名的 JSON 文档，字段：`domainId, revision, issuedAt, status(active|frozen),
validUntil, members[], assets[], issuers[], maxGrantsPerDay, signatures[]`。

- **签名**：每方一条 JWS（`typ=DOMAIN`），签名对象为不含 `signatures` 的正文的规范字节串
  （sort_keys + 紧凑分隔符 + UTF-8）；验证时**必须**校验"签名对应正文与当前正文逐字节
  一致"（防张冠李戴）。
- **法定人数**：建域与**修订**均要求 ≥2 个不同签名者——修订权 = 创建权（防单方 bump
  版本驱逐/拉入成员）。
- **revision**：签名保证完整性，不保证新鲜性。同 `domainId` 多版本并存时**必须**取最高
  revision；裁决永远按本地最高版本；令牌携带的 `acps_domain_revision` 仅审计线索。
- **状态**：`frozen` = 域内急停（§4.5）。
- 部署形态：MVP 为各资源方本地持有的文件（与 trust-bundle 同源）；目标形态为 registry
  侧元数据注册表（接口不变——PEP 只依赖"给我一个策略源"）。

### 4.3 Grant Token 画像

与 DT 共享验证内核，claim 形态差异：

| 维度 | DT（任务级） | GT（域级） |
|---|---|---|
| act 链 | 有（受托人） | **无**（直接授予） |
| `aud` | 目标伙伴 | **持有者自身**（`acps:agent:{sub}`）——主体级绑定：只能由 sub 本人使用 |
| TTL | ≤600s | ≤30 天（建议默认 7 天） |
| `scope` | `acps.skill.invoke:{skill}` | `acps.domain.grant:{domainId}` |
| 资产表达 | 单 skill | `acps_assets: [{providerAic, skill}]`（**可跨多个资源方**） |
| 配额 | jti 一次性 | `acps_max_uses: n`（§4.7） |
| 域关联 | 继承自父 GT | `acps_domain` + `acps_domain_revision` |

GT 的验证入口为独立函数（链/签名/iss==叶子/时效/结构），语义裁决（主体绑定/成员/角色/
配额/域状态）由 PEP 域路径执行。

### 4.4 派生与嵌套（GT → DT）

签发者持 GT 为伙伴派生 DT 时，**必须**：

1. **继承** `acps_domain` 与 `acps_domain_revision`（缺失继承即"洗白"漏洞：无域标识的
   派生令牌会掉回白名单路径，绕过域冻结）；
2. `exp(DT) ≤ exp(GT)`；
3. `acps_skill_id(DT) ∈ acps_assets(GT)`（收窄规则，对齐 AAC §10.5：子授权 ≤ 父授权）；
4. **嵌入父 grant 的 JWS 原文**（`acps_parent_grant`），验证方必须重验父签名并核对
   jti/domain/sub/aud/assets/exp/max_uses/revision。只继承自报的 parent_jti/exp/
   max_uses 不构成密码学锚点：任何持签发者私钥的代码都能伪造 1e9 配额，使
   "PEP 独立复核"名存实亡。父 grant 的 `sub` 必须等于 DT 的 `iss`（持有者绑定），
   且父 grant 的 `iss` 必须属于域 issuers。

### 4.5 域状态与冻结

- 冻结语义：**该域一切授权链路即时死亡**——GT 本身、全部已派生 DT、后续签发。
- 实现：PEP 对一切携带 `acps_domain` 的令牌（不论画像）在**验签之前**查域状态（§3.4 ⓪′）。
- 语义边界：域冻结杀死的是"域派生的授权"；签发者的固有白名单权限是资源方自有配置，
  不受域冻结影响。**全局急停 = 资源方从自有白名单摘除**，即时生效。
- **传播延迟（MUST 声明）**：章程文件形态下，冻结生效 = 各资源方重载章程；最坏生效窗口
  = min(存量派生令牌剩余 TTL, 章程分发延迟)。目标形态以 registry 推送收窄。

### 4.6 成员与角色裁决

携带域标识的令牌，验证密码学有效性之后**必须**依次：

1. **主体绑定**（防盗用）：GT 的 `sub` == 调用方 AIC；
2. **成员资格**（防非成员）：调用方 ∈ 章程 members（纵深价值：即使签发者误发/作恶给
   非成员签出合法签名 GT，PEP 兜底拒绝）；
3. **角色许可**：成员角色 ∈ 目标资产 `allowedMembers`。资产解析规则：调用方声明了
   skillId 则**必须**命中 GT 在本资源方的资产集；未声明且 GT 在本资源方仅覆盖一个资产
   时取该资产，覆盖多个时**必须**拒绝（要求声明）。

### 4.7 配额（双轨，不得混用）

| 配额 | 语义 | 计数位置 | 存储 |
|---|---|---|---|
| `maxGrantsPerDay`（章程） | 签发侧：签发者每日可发 GT 数 | 签发工具 | **必须**持久化 |
| `acps_max_uses`（GT） | 使用侧：单枚 GT 可准入次数 | PEP | **必须**持久化（进程重启清零 = 配额失效）；计数**必须**原子 |

### 4.8 对账（双向认账）

- 资源方按周期从使用记录聚合报表：`{domain, domainRevision, window, providerAic,
  usage:[{callerAic, skill, count}], totalUses}`；
- **双方**（资源方 + 各使用方）对报表正文签名；验证要求签名方覆盖双方且正文一致；
- 报表**必须**记录裁决所依据的 `domainRevision`（版本仲裁依据）；
- 使用记录**不得**包含令牌原文（只记 jti/原因码/域/技能，对齐 AAC §13）。

---

## 5. 审计要求

每次裁决（含放行）**应当**落审计记录，字段：`ts, decision, rule, callerAic, myAic,
taskId, sessionId, delivery, jti, delegationId, code, reason, domain, domainRevision,
skillHint, pepVersion`。`reason` 为稳定原因码；对外永不输出。

规则码注册表（初始值）：`public-skill, public-skill-unscoped, leader-allowlist,
no-credential, peer-not-verified, invalid-token, issuer-not-authorized, issuer-not-in-domain,
replay, act-peer-mismatch, route-not-allowed, session-mismatch, skill-mismatch,
chain-depth-exceeded, pep-misconfigured, pep-internal-error, domain-unknown, domain-frozen,
domain-not-member, domain-asset-not-allowed, grant-subject-mismatch, grant-not-member,
grant-role-not-allowed, grant-skill-required, quota-exhausted, delegation-token,
domain-delegation, domain-grant`。

---

## 6. 安全考量（已验证的攻击与对策）

| 攻击 | 对策（规范要求） | 发现轮次 |
|---|---|---|
| 持证成员自签令牌越权（iss 任意填） | §3.2 iss==叶子 AIC + §3.4 3a′ 签发者白名单/域签发者（先于 jti） | 实现期审计 |
| 授权标记伪造（提示注入冒充授权事实） | §3.3 哨兵过滤覆盖文本与结构化部件的渲染形态 | 第四轮 |
| 令牌经模型上下文泄露（"复述上一个工具结果"类注入） | §3.7 句柄方案（MUST）+ §3.3 剥离/替换 | 第四轮/闭环轮 |
| **域状态穿透**（冻结后已派生 DT 仍有效） | §4.4 强制继承 + §3.4 ⓪′ 验签前域预检 | 第九轮审查 |
| 派生"洗白"（剥离域标识绕过冻结） | 同上（继承为 MUST，无域标识的令牌只走白名单路径，其权力来源另有边界声明） | 第九轮审查 |
| 章程旧版本回放（签名有效但已作废） | §4.2 revision 高版本胜出 + 修订双签法定人数 | 第九轮审查 |
| jti 烧毁 DoS（泄露令牌被第三方抢先消费） | §3.4 3a′ 置于 3d 之前（不可信签发者不得烧毁 jti）；已知残留面：令牌原文泄露后任何成员可烧毁 GT/DT 的使用次数（低危，需先泄露） | 第五轮已定性 |
| L0 放行的是智能体不是能力 | 已知边界：skillId 为声明式（AIP v2 命令不携带）；缓解=单一可见性拆分部署。**禁止**把断网沙盒内的执行类能力声明为 public | 第五轮定性 |

---

## 7. 与 AAC 的偏差声明（规范性附录）

实现与本草案对 AAC 的显式偏差，**必须**在采用时一并声明：

1. **撤销**：无 CRL/introspection 通道。DT 以 ≤600s exp 为撤销上界 + jti 一次性；GT 以
   域冻结 + `acps_max_uses` 为撤销手段。目标形态：交换层（长期凭证在线换短期令牌）+ 
   registry 章程推送。
2. **cnf 证书级绑定**：admission 点不暴露对端证书，以 `act.aic == peer`（DT）/`sub == peer`
   （GT）主体级绑定等价实现；admission 请求暴露对端证书后升级。
3. **拒绝形态**：非 JSON-RPC 错误，为任务级 `rejected` + §12 通用文案；错误码仅入审计。
4. **ACS 无 public 字段**：`public` 为本机能力声明注解，无线上语义；"公开=承诺向注册社区
   开放"是本草案约定语义，成为线上声明需 ACS 契约扩展。
5. **sessionId**：仅审计线索，不做授权边界（发起方不可预知对端会话 id）。
6. **skillId 声明式**：AIP v2 命令不携带能力标识，技能维度校验只约束诚实调用方；真正的
   边界是白名单与令牌（+单一可见性部署建议）。
7. **admission 单点裁决**：准入只在 `start` 裁决一次；`continue` 不再过准入（任务查键
   绑定同一对端，无法冒换）。

**实现已知限制（非 AAC 偏差；采用本草案时应当一并声明）**：

- **配额计数器跨进程竞态**：使用侧/签发侧账本的单连接事务（deferred BEGIN）在**多进程
  并发**下存在 COUNT→INSERT 竞态——两个进程可能同时通过同一限额后各自记账。单写者/
  演示形态无影响；生产形态必须改用 `BEGIN IMMEDIATE` 独占写事务或收敛为单写者服务。
  本参考实现按单写者规模**声明而非实现**该加固。
- **④′ 执行侧核对未实现**：参考实现当前防线为声明式 skillId + 白名单/令牌；工具调用
  包装核对列为 SHOULD（见 §3.4），随 wit 暴露稳定的工具包装钩子后落地。

---

## 8. 一致性要求

PEP 实现**必须**：⓪–④ 全序裁决、⓪′/3a′/⑦ 三处增补、句柄签发、哨兵过滤、审计不落原文、
fail-closed 兜底。实现 Part B 者**必须** additionally：4.4 派生继承三则、4.6 三级裁决、
4.7 双轨持久配额、4.2 revision 规则。

准入级别：L0 公开能力（本机声明 public + 已验证对端）；L1 白名单；L2 令牌（DT/GT）。

---

## 9. 实现状态与证据（资料性附录）

- 参考实现：wit 2.1.0 零侵入（`WitAgent.aip_hooks` / `WitHooks` 扩展点），acps_sdk 2.2.0。
- 测试：189 项离线单测（2026-10-29 实测；起草时为 173 项，含规范 §4.1 校验码示例对拍、域状态穿透回归、配额持久性）。
- 演示：准入 16 场景、GPU 资源 7 场景（真实 CUDA 执行）、域级 11 场景（双资源方）。
- 量化：脚本化对比 2.94–2.96× 加速（两轮实测） / 并行度 1→3 / leader 上下文 196KB→0；真实 LLM 对比
  （LongCat-2.0）1.73x / 回合 7→2 / 峰值上下文 9579→2358 字符 / 材料 0 字节过 leader。

## 10. 待定项（OPEN）

STS 与独立 PDP；CRL/交换层；registry 托管章程与冻结推送；discovery 可见性过滤（域管
可见性、策略管访问的另一半）；多方阈值治理；互惠记账（credit 对冲）；AIP 命令携带
skillId 的协议级绑定。

---

## 11. Part C：域可见性过滤（发现层，Phase D）

> 定位：域管可见性（发现层），策略管访问（交互层）——与 Part A/B 的分层互补。
> 可见性是**元数据层收窄**（存在性保密），不是安全边界（访问控制仍在 PEP）。

### 11.1 声明（wit → ACS）

skill 级可选字段，经 wit `@capability(acps_skill={...})` 透传进 ACS（实测透传，wit 零改动），
随 DSP JSONB 原样到达发现服务：

```json
{"id": "gpu.run", "visibility": "domain", "domains": ["lab-x-y"]}
```

无字段 = `public`（默认；**存量数据零影响**）。三档语义：public（人人可见）/ 橱窗
（可见但 PEP 门槛）/ domain（仅域成员可见）。

### 11.2 查询身份

调用方 AIC 经协议自带的扩展槽 `DiscoveryContext.metadata["callerAic"]` 携带
（acps_sdk 明文"供上下游约定使用"——零请求协议变更）。无身份 = 匿名。目标形态：
mTLS 证书 SAN 提取（同接口）。伪造身份的收益上限是"看见"——访问由 PEP 拦截。

### 11.3 过滤语义（发现服务 MUST）

- skill 无 `visibility` → 可见（兼容）；
- `visibility=="domain"` → caller ∈ `domains` 中任一域的成员才可见；
- agent 过滤后无可见 skill → 默认整个 agent 不出现（存在性不泄露；这是比"能力不可见"
  更强的可选策略，不是过滤器的必然结果）；
- **能力可见 ≠ agent 可见**：若部署目标是"只隐藏能力、保留 agent"，给该类 agent 加一个
  public 非 domain 的 profile 能力（如 `agent.info`），或让发现服务返回 `skills: []`
  的 agent 壳；后者会泄露 agent 存在性，需协议/前端支持；
- **保留行的 ACS 体必须同步脱敏**：响应中任何路径携带的 `acs.skills` 不得包含该调用方
  不可见的技能声明（名称/描述/绑定域）——否则混合可见性 agent 的公开行会把域资产
  "带出"（存在性泄露的另一半）；
- 投影缺失（分发失败）→ 域资产对所有人不可见（fail-closed）。

**已知实现限制（MVP 缓解，非正解）**：过滤发生在检索截断之后，采用"超取 limit×K →
过滤 → 截回 limit"（K=3，钳到协议上限 50）。窗口内不可见占比超过 (1-1/K) 时仍可能
欠填（域成员可见资产被挤出的假阴性）。正解为谓词下推进 SQL（届时回列方案：可见性
入索引列）。

### 11.4 复用边界（不重复造轮子）

- 发现服务侧**零表结构变更**：查询时读 `Agent.acs` JSONB（对存量已同步数据同样生效）；
- 域成员数据来自**策略投影** JSON——签名验证在**导出方**（持双签章程，复用 Part B 的
  verify_manifest），服务端只做哑读取与成员判定，不重复实现密码学；
- 配置 `DOMAIN_POLICY_DIR` 为空 = 特性关闭 = 行为零变化。

### 11.5 兼容性矩阵

| 组合 | 行为 |
|---|---|
| 新服务 + 老声明（无字段） | 全 public 可见，零变化 |
| 新服务 + 老客户端（无身份） | 匿名：见存量全部，不见新域资产 |
| **老服务 + 新声明** | 未知字段被忽略（实测 `.get()` 摄取），资产照常可见——降级安全 |

## 附录 A：示例

**A.1 DT claims**（节选，单级委托）：
```json
{"iss": "agent:1.2…LEADER", "sub": "agent:1.2…LEADER", "aud": "acps:agent:1.2…TARGET",
 "exp": 1790000600, "iat": 1790000000, "jti": "uuid4", "scope": "acps.skill.invoke:gpu.run",
 "act": {"sub": "agent:1.2…DELEGATE", "aic": "1.2…DELEGATE",
         "act": {"sub": "agent:1.2…LEADER", "aic": "1.2…LEADER"}},
 "acps_target_aic": "1.2…TARGET", "acps_skill_id": "gpu.run",
 "acps_delegation_mode": "fixed", "acps_allowed_route": ["agent:…L", "agent:…D", "agent:…T"],
 "acps_chain_depth": 1, "acps_max_chain_depth": 2,
 "acps_domain": "lab-x-y", "acps_domain_revision": 1}
```

**A.2 GT claims**（节选，双资产）：
```json
{"iss": "agent:1.2…LEADER", "sub": "agent:1.2…CONSUMER", "aud": "acps:agent:1.2…CONSUMER",
 "exp": 1790604800, "iat": 1790000000, "jti": "uuid4", "scope": "acps.domain.grant:lab-x-y",
 "acps_domain": "lab-x-y", "acps_domain_revision": 1,
 "acps_assets": [{"providerAic": "1.2…GPU", "skill": "gpu.run"},
                  {"providerAic": "1.2…DATA", "skill": "dataset.fetch"}],
 "acps_max_uses": 5}
```

**A.3 章程**（节选）：
```json
{"domainId": "lab-x-y", "revision": 2, "issuedAt": "…", "status": "active",
 "members": [{"aic": "…GPU", "role": "provider"}, {"aic": "…CONSUMER", "role": "consumer"}],
 "assets": [{"providerAic": "…GPU", "skill": "gpu.run", "allowedMembers": ["consumer"]}],
 "issuers": ["…LEADER"], "maxGrantsPerDay": 50,
 "signatures": ["<JWS typ=DOMAIN by LEADER>", "<JWS typ=DOMAIN by GPU>"]}
```

**A.4 校验码测试向量**（AIC 规范 §4.1）：数据码 `1.2.156.3088.1.1.34C2.478BDF.3GF546` +
盐 `0x1234` → CRC 原值 `0x646C` → Base36 `0JU4`。

## 附录 B：注册表

- `dataItem.metadata.role`：`delegation-token`（§3.3）、`need-report`（结构化报缺，非规范）。
- 令牌 `typ`：`JWT`（DT）、`GRANT`（GT）、`DOMAIN`（章程签名）、`RECON`（对账签名）。
- 审计规则码：见 §5。
- 错误码：-32008 / -32009 / -32010（仅审计）。
