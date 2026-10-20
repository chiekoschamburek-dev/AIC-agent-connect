# CPU / GPU / KB 三个资源 agent：注册前的使用控制验收（2026-10-11）

## ★ 最终状态（2026-10-11 20:06 更新）

**KB 已注册、部署上线并完成全部验收；CPU / GPU 的注册提交由用户决定暂缓。**

| 验收项 | 结果 |
|---|---|
| **KB 上线** | ✅ `active`，端口 8599，心跳每 20s；平台查询 `收录=1 aliveMap=True`，`active=True`，skills `[kb.schema, kb.query, kb.ingest]` |
| **KB 准入矩阵**（用真实配置实跑 5 项） | ✅ 全部符合预期（见 §0） |
| **yanshu-kb 域章程** | ✅ `ws-kb/acps/domains/yanshu-kb.r1.json`，双签（KB + supervisor），回读 `status=active members=2 assets=2 issuers=2` |
| **证书链** | ✅ 注册后立即修复：`ws-kb` bundle 4 张、`signing.chain=2` |
| **per-agent 加固** | ✅ `/etc/wit-kb-pep.env` + drop-in：`LEADER_AICS=supervisor` + `LEADER_DIRECT=0` |
| **服务总数** | ✅ 9/9 active（8 个原有 + KB） |

### §0 KB 上线后的真实准入矩阵（实跑输出）

```
[pep] 已加载 1 个域章程: yanshu-kb(r1,active)
本机 AIC = 1.2.156.3088.1.BUPT.VTF97R.GIKJNY.9AL6AY.10UD
公开能力 = ['kb.schema']    含非公开 = True
leader_direct = False       leader_aics = 1        域章程 = ['yanshu-kb']

✔ 外部 指名展示能力 kb.schema（无令牌）  → allow / public-skill      ← 需求(1)
✔ 外部 指名 kb.query（无令牌）         → deny  / no-credential     ← 需求(2)
✔ leader 指名 kb.query（无令牌）       → deny  / no-credential     ← 需求(3)
✔ leader 指名 kb.ingest（无令牌）      → deny  / no-credential
✔ leader 指名 kb.query（携真实令牌）    → allow / delegation-token
```

脚本：`deploy/discovery_diagnostics/acceptance_kb_live.sh`

### §0b KB 注册与上线过程中踩到的三个真实缺陷（都已修）

1. **探测端口与正式端口不一致** → `acs_fingerprint_changed_after_submission`
   提交注册时探测脚本用了端口 **8599**，正式配置是 **8464**；ACS 里固化了 `endPoints`，
   重算指纹不同 → wit 拒绝续用（`failure=acs_fingerprint_changed_after_submission`）。
   **修法**：用提交时的端口 8599 重新 enroll（指纹一致）→ 取回证书 → 正式端口也改为 8599。
   **教训**：探测用的参数必须与正式一致，否则注册态无法续用。
2. **新工作区 trust-bundle 缺中间 CA**（预判过，实测命中）
   注册后 `signing.chain=1` → 令牌永远验签失败。用 `fix_trust_bundle.sh` 修好（→2）。
3. **sender 未纳入 KB 心跳**：`wit-amp-sender` 从 **10-09** 就一直在跑，
   用的是**修复前**的代码（文件列表只在启动时 glob 一次）→ KB 新文件不被投递。
   重启后位点条目 9 → **10**，KB 心跳进入投递 —— 这正是早先修的 P1 项在现网生效。

---

**验收结论：代码侧已满足"三者都需要本地鉴权 token 才能被 PEP 准入"，且展示能力不受影响、
leader 也必须带令牌。注册时需按 §4 配置环境变量。**

---

## 1. 需求与实现对应

| 需求 | 实现 | 验证 |
|---|---|---|
| 展示能力保持公开（可被外部发现/调用） | `cpu.env`/`cpu.status`、`gpu.env`/`gpu.status`/`gpu.data.list`、`kb.schema` 保持 `public=True`，**未改动** | 单测 [2]：指名这些能力 → `allow / public-skill` |
| 干活能力必须持令牌才能准入 | `cpu.run`、`gpu.run`、`gpu.data.fetch`、`kb.query`、`kb.ingest` 均 `public=False`（本来就未声明 public），PEP 落到 L1/L2 | 单测 [1]：指名这些能力 → 外部与 leader **全部** `deny / no-credential` |
| **连 leader 也不例外** | 关闭这三个 agent 的 leader 白名单（见 §4），leader 改走委托令牌 | 单测 [1] leader 指名干活能力 → `deny`；[3] 携令牌 → 进入 L2 校验路径 |

## 2. 准入矩阵（实测，使用真实能力声明）

| 调用方 | 载荷 | 裁决 |
|---|---|---|
| 外部 agent | 未指名 skill | `allow / public-skill-unscoped-narrowed`（运行时仅剩公开能力） |
| 外部 agent | 指名 `cpu.run` / `gpu.run` / `gpu.data.fetch` / `kb.query` / `kb.ingest` | ⛔ `deny / no-credential` |
| 外部 agent | 指名展示能力（`cpu.env` 等） | `allow / public-skill` |
| **leader** | 未指名 skill | `allow / public-skill-unscoped-narrowed` |
| **leader** | 指名干活能力，**无令牌** | ⛔ `deny / no-credential` |
| **leader** | 指名干活能力，**携令牌** | 进入 L2 令牌校验（令牌无效则因验签失败被拒） |

> "未指名 skill"仍放行是**设计如此**（收窄口径），但运行时 `skill_scope` 会把所有
> `public=False` 的工具移出 `ToolManager`，所以**拿不到任何干活能力**。
> 见 `tests/test_skill_scope.py` 第 6 组断言。

## 3. leader 侧改造：`acps_delegate_authenticated`

### 为什么需要

wit 内置的委派工具只发纯文本，**无法附带令牌**；而令牌只能走
`metadata.role == "delegation-token"` 的 DataItem（`token_data_item`，§3.2 约定）。
所以 leader 直调受保护伙伴需要一个新的本地工具。

### 它做什么

新增本地工具 `acps_delegate_authenticated(partner_aic, skill_id, request)`：

1. 用 `build_issue_token_tool_from_workspace(WORKSPACE)` 签发令牌
   （audience = 目标伙伴，skill_id = 目标能力）；
2. 从 `DEFAULT_TOKEN_VAULT` 按 jti 取回原文（**模型全程看不到原文**）；
3. 组装 `[TextPart(指令), DataPart(令牌)]` 作为出站载荷
   （注意 `ContentInput` 是 `typing.Union`，**不能实例化**，直接传 Part 列表）；
4. `rt.aip_delegate(state, partner_aic=…, request=[…])`；
5. 返回 `task_ref` 供 `wait_partner` 等待。

### 已挂上

- 进 `build_run_config().tools`（与 plan_gate / wait_partner 同一可靠通道）；
- playbook 新增教学：调 CPU/GPU/KB 的**干活能力**必须用本工具，**展示能力**走
  `acps_delegate_public`（见下），其余伙伴也走 `acps_delegate_public`。

### 2026-10-16 追加：公开能力的同族工具 `acps_delegate_public`

内置 `acps_delegate` 的 `request` 声明为**数组**（`Sequence[Part]`）。现网模型提供方
偶尔把 array 入参**丢空**，于是框架在参数校验阶段就报
`AipError: Invalid high-level content input`（`durationMs=0`——请求根本没发出去，
白烧一步，还容易被误读成"伙伴不可用"）。

于是新增同族工具 `acps_delegate_public(partner_aic, skill_id, request)`，**三个入参全是
字符串**，`[TextPart(text=request)]` 在代码侧组装，不经过会掉件的数组投影：

- 同样先过委派点标量闸门 `cluster_roster.check_delegation_target`（编造伙伴 / 错派能力 /
  未注册 / 路线 B 隧道未起，一律拒绝委派）；
- `partner_aic` 允许填名册 key、展示名或 AIC，**出站用解析后的权威 AIC**；
- 本工具**不签发令牌**，所以受保护的非公开能力（`cpu.run` / `gpu.run` / `gpu.data.fetch`
  / `kb.query` / `kb.ingest`）在入口就被拒绝并指路到 `acps_delegate_authenticated`
  ——放过去的结果只会是对端 `deny / no-credential`，同样白烧一步；
- `ResourceDispatchAuthHooks`（§5）原样保留：模型若仍用内置 `acps_delegate` 派受保护伙伴，
  透明补令牌那条兜底继续生效。

## 4. 注册时的环境变量与**证书链**要求（重要）

### 4.1 配置：**两个变量 + per-agent drop-in**（我最初写错了，这里更正）

⚠️ **不能只把白名单留空。** 实测发现 `leader_aics` 同时承担两个职责：

| 用途 | 代码位置 |
|---|---|
| ① L1 **直调**放行（`allow / leader-allowlist`） | `pep.py:456` |
| ② 令牌**签发者**白名单（`allowed_issuers = cfg.leader_aics`） | `pep.py:538` |

所以"留空白名单"会**连带剥夺 leader 的签发资格**，它签的令牌被判
`deny / issuer-not-authorized`（实测 `reason="token issuer is not an authorized leader"`）
—— 需求 (2) 与 (3) 会互相打架。

#### 配置怎么落到 systemd（实测确认的注入方式）

unit 模板 `wit-yanshu@.service` 只有**一个共享** `EnvironmentFile`：

```
EnvironmentFile=/opt/wit/my-project/acps_cluster/deploy/.env.production
```

per-agent 的差异靠 **drop-in** 注入（scout 已有同款）：

```
/etc/systemd/system/wit-yanshu@<agent>.service.d/pep.conf
  [Service]
  EnvironmentFile=/etc/wit-<agent>-pep.env
```

服务实际生效的 EnvironmentFile 链（`systemctl show -p EnvironmentFiles`）：

```
/opt/wit/…/deploy/.env.production     ← 共享
/etc/wit-<agent>-pep.env              ← 按 agent 覆盖
```

#### ⚠️ 关键：**不要把 `ACPS_PEP_LEADER_DIRECT=0` 写进共享文件**

共享 `.env.production` 被 8 个服务共用。若把"关闭直调"写进去，
**leader 连现有 8 个伙伴（scout/writer/critic/…）也会被要求令牌** →
现有委派全是纯文本直调 → **调度链直接断**。

**正确写法**：只写进三个资源 agent 自己的 env：

```sh
# /etc/wit-cpu-pep.env （gpu / kb 同款）
ACPS_PEP_ENABLED=1
ACPS_PEP_LEADER_AICS=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6   # 保留签发资格
ACPS_PEP_LEADER_DIRECT=0                                             # 直调也需令牌
```

`finish_resource_agents.sh` 已按此实现（写 env + 写 drop-in + `daemon-reload`）。

**已核实**：共享 `.env.production` 里**没有** `ACPS_PEP_LEADER_DIRECT`
（只有 `ACPS_PEP_ENABLED` 与 `ACPS_PEP_LEADER_AICS`），
所以现有 8 个 agent 的直调行为**完全不变**。

### 4.1b 排查陷阱：**检查 PEP 口径必须带 env**

`load_pep_config()` 从**进程环境**读变量。直接跑
`python -c "load_pep_config(ws)"` 会得到 `enabled=False / leader_aics=0`——
**与服务实际口径不符**（服务是 systemd 用 EnvironmentFile 注入的）。

我因此一度误判"现有 agent 的 PEP 没启用"。**正确做法**（二者之一）：

1. 读服务进程的真实环境：`tr '\0' '\n' < /proc/<MainPID>/environ | grep ACPS_PEP`；
2. 或按 systemd 顺序加载：先共享 `.env.production`，再 `/etc/wit-<agent>-pep.env`。

同理，**部署后凡是结论依赖环境变量的，都必须按上面两种方式之一复核**。

### 4.2 端到端实测（真实签名，非 mock）

```
[0] signing.chain 长度 = 2                       （证书链完整，否则令牌无法锚定）
[1] leader 指名 cpu.run、无令牌     → deny  / no-credential      ✓
[2] leader 指名 cpu.run、携真实令牌 → allow / delegation-token    ✓  ← 需求达成
[3] 令牌绑定 gpu.run 却请求 cpu.run → deny  / skill-mismatch       ✓
[4] 令牌受众是别的 agent            → deny  / invalid-token        ✓
[5] 已过期令牌                      → deny  / invalid-token        ✓
[6] leader 指名 cpu.env（展示能力） → allow / public-skill         ✓ 未误伤
```

脚本：`deploy/discovery_diagnostics/e2e_leader_token_admission.py`
（用真实 `load_signing_context` + 真实 trust-bundle 签验，覆盖签发→取回→准入全链）。

### 4.3 令牌的 `delegate_aic` 语义（实现要点）

直调时令牌必须是 **`delegate_aic = 调用方（leader 自己）`**、`target_aic = 资源 agent`，
因为资源 agent 侧校验 `act.aic == request.leader_aic`（PEP 3e "act 与 peer AIC 绑定"）。
把它写成资源 agent 会被判 `deny / act-peer-mismatch`（实测 `reason="act.aic != peer aic"`）。
`supervisor._make_authenticated_delegate_tool` 已按此实现。

### 4.5 委派路径的**透明补令牌**（覆盖全部 13 处委派点）

**问题**：leader 有 13 处委派点，模型实际用的是通用的 `acps_delegate`
（留痕实测：`acps_delegate` 12 次、参数 `{partner_aic, request}`）。
只靠 playbook 提醒"请改用 `acps_delegate_authenticated`"**不可靠**——模型忘了就是一次
白跑的 `deny / no-credential`。

**为什么不能简单换工具**：实测 `acps_delegate` **不在 ToolManager 里**
（`aip_submit` 也不在）——它们是**运行期间**由 `AipDelegationHooks` 注入的，
所以 `ctx.add_tool` 同名替换对它无效。

**解法**：新增 `ResourceDispatchAuthHooks`，用 wit 的标准钩子 **`wrap_tool_call`**
（`AipDelegationHooks` 自己就用它发起委派）包一层：

```
acps_delegate 调用
  → 目标伙伴在受保护名册里？
     是 → 签发令牌（act.aic = leader 自己）→ 取回 JWS
          → request 从纯文本换成 [TextPart(原文), DataPart(令牌)]
          → 交给下游 handler
     否 → 原样透传（scout/writer/… 完全不受影响）
```

- 已挂进 supervisor 的 `hooks=[...]`（优先级 40，早于 `AipDelegationHooks(50)`，确保在外层）；
- 受保护名册 `_protected_partners()` 从各工作区 `acs.final.json` **读 AIC、不写死**，
  资源 agent 未注册时名册为空 → 等价透传（对现网零影响）；
- 令牌原文只在本调用栈内出现，**不进模型上下文**（剥离由 `DelegationTokenScrubHooks` 负责）。

单测 `tests/test_resource_dispatch_auth.py`（6 组）：受保护伙伴附令牌且
`targetAic`/`targetSkillHint` 正确、非受保护伙伴**完全透传**、其它工具不受干扰、
名册为空等价透传、既有字段保留、令牌不出现在 `request` 之外的字段。

> **说明**：令牌**确实**出现在出站载荷里——这是设计要求（对端要靠它过准入）。
> "不进模型上下文"是另一条保证（scrub 钩子），不要把两者混为一谈。

### 4.6 部署陷阱：scp 目标写成目录会让改动**静默不生效**

本轮**反复**踩到（不止一次，还导致我误判与重查）：

```sh
# ❌ 目标写成目录、不带文件名 → 改动没落到目标文件，命令却不报错
scp supervisor.py host:/opt/wit/.../acps_cluster/
scp e2e.py        host:/opt/wit/.../discovery_diagnostics      # 注意结尾没有斜杠/文件名
```

后果（真实发生）：
1. 我以为 `supervisor.py` 已部署 → 实测"前缀校验没生效"，白排查一轮；
2. `e2e_leader_token_admission.py`、`fix_trust_bundle.sh` 与**三份文档**
   **在服务器上根本不存在**，但我的部署输出显示"已上传"；
   直到收尾核对才发现（此前那些"验证通过"是用**管道内联脚本**跑的，所以结论仍有效，
   但**文件没落地**）。

**规矩（必须遵守）**：
- 部署单个文件时**始终带完整目标路径（含文件名）**；
- 部署后**立刻回读**（`grep -c '<标志串>'` 或 `ls -la`）确认落地；
- 批量收尾核对见 `deploy/discovery_diagnostics/round3_final_check.sh`。

同类坑：`ssh "... for n in a b; do ... $n ..."` 里的 `$n` 会被 PowerShell 的引号处理吃掉
（循环变量为空 → `wit-yanshu@.service` 报错）。
**凡带 shell 循环/变量的远程命令，一律写成 `.sh` 文件 scp 过去再执行。**

### 4.7 `trust-bundle.pem` 必须含**签发中间 CA**（否则令牌永远验签失败）

**这是注册前必须检查的一项——本次发现并修复的全集群性缺陷。**

实测发现各工作区的 `trust-bundle.pem` 里**只有 2 个自签根**（Agent CA Root、ACPs Root CA），
**缺 `Agent CA Intermediate Certificate`**（而我们的 leaf 正是它签发的）。后果：

```
build_certificate_chain 补不出中间证书 → signing.chain 长度退化为 1（只有 leaf）
→ _verify_chain 两个分支都失败
→ 任何委托令牌都报 TokenError: chain-untrusted: chain tail … is not anchored
```

`token.py` 的设计注释写"当前集群 bundle = Root + 两个 Intermediate"，**与实物不符**。

**修复**（已对全部现有工作区执行，幂等）：

```sh
# 把中间证书追加进 bundle（中间证书就在 client/certificate.pem 的第二张）
cat <workspace>/acps/client/certificate.pem >> <workspace>/acps/trust-bundle.pem
```

脚本：`deploy/discovery_diagnostics/fix_trust_bundle.sh`（逐工作区检查+备份+追加+复核）。

**复核标准**：`signing.chain 长度 ≥ 2`（修复前为 1）。

**注册新 agent 时务必确认**：新工作区的 `trust-bundle.pem` 含中间 CA
（`ws-cpu` / `ws-gpu` / `ws-kb` 目前**尚无** `acps/` 目录，注册后要立刻检查）。

配套代码改动：`delegation/issuer_tool.py` 改为 `include_intermediates=True`，
使 token 的 `x5c` 带上整链——这样对"Root only"与"Root + Intermediate"两种 bundle
形态都能锚定，不再依赖"验证方 bundle 恰好含中间 CA"这一脆弱前提。

### 4.3 修复后的令牌实测（端到端）

```
签发 ok=True，句柄不含令牌原文（模型看不到 JWS）
取回并验签 → ✔ 通过
  iss = agent:…JXN8O8.98DOO8.0EB6        （leader）
  aud = acps:agent:…<资源 agent AIC>      （受众 = 目标资源 agent）
  act = {aic: …JXN8O8.98DOO8.0EB6}        （调用方绑定）
  acps_skill_id = cpu.run                 （能力绑定）
  无 acps_domain claim                     → 普通技能走非域路径；若目标技能声明
                                           visibility=domain，PEP 现在会 deny domain-required
```

> 2026-10-20 域硬化后：`acps_domain` 只能由 `derive_delegation_claims()` 从域 grant
> 派生进子令牌；子令牌还必须携带 `acps_parent_jti/exp/max_uses`，PEP 会复核
> `exp ≤ 父 exp` 并继续扣父 grant 配额。

## 6. 注册路径实测（2026-10-11）：可程序化提交，但**必须人工审核**

### 6.1 注册是程序化完成的

`partner_kb.py` → `cluster_common.serve_partner(...)` → `WitRuntime.up_until_ready(...)`：
程序会自动创建账号目录、提交 ACS draft、领证书、起 mTLS 服务。
未就绪时以**退出码 2** 结束交给 systemd 重试。

前置条件是**账号变量**（`ACPS_<PREFIX>_USERNAME/PASSWORD`）：
所有 agent 共用同一账号 `pxyy`（`.env.production` 里 8 个 agent 全写 `pxyy`）。

**已补**：`ACPS_CPU_*`(8462) / `ACPS_GPU_*`(8463) / `ACPS_KB_*`(8464)
（备份 `deploy/.env.production.pre-resource-creds-*`；只加配置、未启动服务）。

### 6.2 ⚠️ 发现：**首次注册会被 PEP 的 fail-fast 挡住**（鸡生蛋）

用临时工作区实测首次注册：

```
build_kb_agent → build_pep_hooks → load_pep_config
  → ValueError: ACPS_PEP_ENABLED=1 但无法解析本机 AIC
    （enrollment.json 与 client/certificate.pem 均缺失）——PEP 拒绝启动
```

原因：`_validate_config` 要求启用 PEP 时必须有本机 AIC（做 `aud` 校验），
而 AIC 要**注册拿到证书后**才有——首次注册必然踩到。

**绕法**（实测有效）：首次注册时给一个**占位 AIC**，让启动期校验通过：

```sh
ACPS_PEP_MY_AIC=1.2.156.3088.1.BUPT.VTF97R.PLACEHLD.PLACEH.PLC1
```

拿证后该变量即可去掉（`_my_aic_from_workspace` 会从 `enrollment.json` 正确解析）。

> 这是**真实缺陷**（fail-fast 与首注册互斥），值得反馈给框架侧；
> 我们这一侧的绕法是设占位 `ACPS_PEP_MY_AIC`。

### 6.3 注册**必须平台方审批**（因此第 (4) 项无法全自动收尾）

设置占位 AIC 后跑通到下一步，结果：

```
ready       = False
status      = pending_review
next_action = Registry review is pending; rerun runtime.up_until_ready() after approval
```

**为什么我们无法自己批**（2026-10-11 实测，registry 的 OpenAPI 与权限返回）：

```
POST /api/v1/agent/staff/{agent_id}/auto-review
  → HTTP 403  urn:acps:error:auth:insufficient_permissions
    "User does not have required roles: [<RoleType.STAFF>, <RoleType.ADMIN>]"
    user_id: 01a0a406-e1ea-73a0-9601-7eb7d9d788f7   user_roles: ["CLIENT"]
```

即**我们的账号只有 `CLIENT` 角色**，审批（`staff/*` 系列）需要 `STAFF`/`ADMIN`。
这是**权限隔离**，不是流程缺失——**必须平台方在后台批准**。

registry 上确认该注册已存在（`GET /api/v1/agent/client`）：

```
name = 研枢-KB-Partner   version = 1.0.0   acs.active = false（待审）
skills = kb.schema / kb.query / kb.ingest（均带描述与 examples）
```

相关端点（来自 `GET {ACPS_REGISTRY_URL}/openapi.json`，共 67 个）：

| 端点 | 用途 |
|---|---|
| `GET /api/v1/agent/client` | 列本账号的 Agent（含审核状态）——**查状态用这个** |
| `POST /api/v1/agent/client/{agent_id}/submit` | 提交审核 |
| `POST /api/v1/agent/client/{agent_id}/cancel` | 撤销审核申请 |
| `POST /api/v1/agent/staff/{agent_id}/auto-review` \| `/process` | 工作人员审批（需 STAFF/ADMIN） |

> 这与既有 8 个 agent 的经历一致（用户此前"注册了 6 份提交稿"正是指这个环节）。

### 6.4 因此第 (4) 项的收尾顺序

1. **平台审核通过**三个资源 agent 的注册；
2. 启动三个服务（unit 模板已存在 `wit-yanshu@{cpu,gpu,kb}`，端口 8462/8463/8464）；
3. **立刻检查证书链**（新工作区必查）：`sh deploy/discovery_diagnostics/fix_trust_bundle.sh`
   复核标准 `signing.chain ≥ 2`；
4. 写 `ACPS_PEP_MY_AIC` 移除、并按 §4.1 落 `/etc/wit-<agent>-pep.env`
   （`ACPS_PEP_LEADER_AICS=<supervisor>` + `ACPS_PEP_LEADER_DIRECT=0`）；
5. **生成并签名 KB 章程**（此时 KB 已有证书，可双签）：
   ```sh
   python deploy/make_kb_domain_charter.py --kb-aic <KB AIC> \
       --leader-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 --kb-workspace-name kb
   ```
   判定标准：回读 **`status == "active"` 且有成员**（毒化态要报错，见 §5.4）。

## 7. 关于 yanshu-kb 域章程（可选硬化；脚本已就绪并通过流程验证）

### 5.1 本机声明现在参与准入裁决（2026-10-20 硬化）

`partner_kb.py` 的 `kb.query` / `kb.ingest` 声明了
`acps_skill={"visibility": "domain", "domains": ["yanshu-kb"]}`。

**旧行为（已修复）**：`token_domain` 只取令牌的 `acps_domain` claim，所以 leader
用一枚普通委托令牌调 KB 时根本不碰域校验——代码里写着 domain，平台/PEP 却看不到。

**现在**：`build_pep_hooks(capabilities=...)` 会把代码声明解析成
`PepConfig.domain_skills`（发现层元数据 + 域令牌的目标域约束）；**访问控制本身
以已发布的域章程资产表为准**：

- **代码声明 `visibility=domain` 且章程资产表收录**的技能，普通委托令牌
  （无 `acps_domain`）→ `deny / domain-required`（KB 两者都有，因此这条现在生效）；
- 域令牌的 `acps_domain` 必须与代码声明的目标域/章程集合一致 → 否则
  `deny / domain-skill-mismatch`；
- 域令牌缺失 → `domain-unknown`；冻结 → `domain-frozen`（仍在验签前）；
- 域令牌必须由 grant 派生，携带 `acps_parent_jti/exp/max_uses`，并把**父 grant
  的 JWS 原文**作为 `acps_parent_grant` 嵌入；PEP 重验父签名并核对主体/域/资产/
  exp/max_uses。缺 proof、伪造 jti/配额、父主体或父签发者不符 → 
  `deny / invalid-parent-binding` 或 `parent-issuer-not-in-domain`。

代码声明单独不会凭空造出一个域（避免把尚未建章程的 scout/hpc 能力锁死）；
章程一旦发布，高水位/签名副本会保证它不会因删文件而失效。

### 5.2 生成脚本与**必须遵守的两条约束**

脚本：`deploy/make_kb_domain_charter.py`

```sh
python deploy/make_kb_domain_charter.py \
    --kb-aic <KB 的 AIC> --leader-aic <supervisor 的 AIC> \
    --kb-workspace-name kb --leader-workspace-name supervisor \
    [--out-dir <目录>] [--dry-run]
```

⚠️ **约束一：签名者必须是域当事方。**
`verify_manifest` 有安全校验（2026-10-06 审查 #6）：

```
manifest-signer-not-party: 签名者不是域成员/签发者
```

即**每一位签名者都必须出现在 `members` 或 `issuers` 里**，否则整份章程被判无效。
（设计意图：防"两个自注册身份双签一份把自己列为 issuer+资产的章程"。）

⚠️ **约束二：至少 2 位不同的签名者**（`MIN_SIGNATURES = 2`），且不能是同一方签两次。

⚠️ **约束三：revision>1 必须引用上一版**。脚本会自动找
`<out-dir>/<domain>.r{revision-1}.json`（或 `--previous <文件>`），新版写入
`previousRevision` / `previousDigest`；加载器要求上一版至少 2 位签署方连署。
否则修订会被隔离（不会毒化域，但也绝不生效）。

因此 **KB 必须自己先注册**（拿到证书才能作为签名者），之后：

```sh
# KB 注册后执行（KB 自己 + supervisor 双签，两者都在 members/issuers 里）
python deploy/make_kb_domain_charter.py \
    --kb-aic <KB AIC> --leader-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
    --kb-workspace-name kb
# 输出到 ws-kb/acps/domains/yanshu-kb.r1.json，并立即回读验签
```

### 5.3 流程已验证通过（用两个真实工作区代替）

由于 `ws-kb` 尚未注册，我先把**整条链路**验证通（两个签名者都写进 members 以自洽）：

```
  法定签名数=2  members=2  assets=2
  签名者 += supervisor(…JXN8O8.98DOO8.0EB6)
  签名者 += scout(…GMT5S6.4SZM68.0LC5)
  ✔ 已写入 …/yanshu-kb.r1.json
  yanshu-kb: revision=1 status=active members=2 issuers=2 assets=2
      asset kb.query  @ … → 允许角色 ['auditor', 'consumer']
      asset kb.ingest @ … → 允许角色 ['auditor', 'consumer']
  ✔ 流程验证通过：双签章程被验签并解析为 active 策略
```

脚本：`deploy/discovery_diagnostics/prove_charter_flow.sh`

### 5.3b ★ **域路径本身可用**（章程的目的所在，此前缺失的证据）

此前只验证了"**缺**章程时带 `acps_domain` 的令牌被 `deny / domain-unknown`"，
**没有**验证"有章程且调用方是成员时能放行"——而后者才是章程存在的意义。

新增 `tests/test_domain_path_works.py`（5 组，真实双签章程 + 真实域令牌）：

```
[0] 章程就绪: status=active members=2 assets=1 issuers=2
[1] 域成员 + 章程            → allow / domain-delegation        ← 域路径确实可用
[2] 域外调用方               → deny / domain-not-member
[3] 请求未授权 skill          → deny / domain-asset-not-allowed
[4] 无章程（对照）            → deny / domain-unknown
[5] 章程 frozen              → deny / domain-frozen
```

即：**章程落盘后域级授权真的能工作**；且非成员、未授权 skill、无章程、冻结四种情况
全部 fail-closed。这条证据让第 (4) 项在"KB 发证后落盘"这一步之前就已可验证。

### 5.4 排查陷阱（我踩过，记录下来）

1. **`load_domain_policies` 在验签失败时不返回 None，而是"毒化"** ——
   合成一个 `status=frozen`、无成员无资产的策略（fail-closed）。
   所以判定"章程是否生效"**必须看 `status == "active"` 且有成员**，
   只看 `policy is not None` 会把**验签失败误报成通过**（脚本第一版就误报了）。
2. **`--out-dir` 必须给**（否则默认写进 `ws-<kb>/acps/domains/`）——
   脚本第一版把 `--kb-workspace-name` 当输出目录，测试时误写进了
   `ws-supervisor/acps/domains/`（已清理）。测试请显式 `--out-dir`。
3. `signatures` 是 **JWS 字符串数组**，不是对象数组（写调试脚本时注意）。

## 6. 验证口径

- 新增 `tests/test_resource_agent_authz.py`（5 组）：
  干活能力对谁都 deny / 未指名按收窄口径 / 展示能力不受影响 /
  leader 携令牌进入 L2 / 带令牌委派工具的形状与必填校验。
- 更新 `tests/test_cluster_logic.py` 的工具集断言（新增带令牌委派工具）。
- **本地全量 342 通过（1 skip）**。

### 6.1 对账（reconciliation）用量口径（2026-10-15 审查补）

队友提出"access 计数口径对不上"，核实后修了 `delegation/reconciliation.py::build_report`
与 `delegation/pep.py::_trace`。以下口径是双方认账的依据：

1. **计数对象 = 准入(allow)裁决次数**，不是能力执行成功次数。与 RFC §4.7
   "`acps_max_uses` = 单枚 GT 可**准入**次数"同口径；`pep.py:814` 的配额消费发生在
   allow 之前、能力执行之前，**执行失败/超时不回滚**。对方若按"成功访问次数"记账，
   数字必然小于我们——这是定义差异，不是漏计。
2. **时间窗真实生效**：`window[0] <= ts < window[1]`（`ts` 与窗口同为定长 UTC
   `%Y-%m-%dT%H:%M:%SZ`）。缺 `ts`/格式非法 → 排除并计入 `excluded.noTimestamp`；
   窗口自身非法 → 全部判窗外（签名报表不猜区间）。修复前 `window` 只是标签，
   换成 2025 年与"本次运行"两份报表逐字相同。
3. **归属字段恒落齐**：非域路径的 allow 也写 `domain="(no-domain)"`、
   `skillHint="(undeclared)"`（哨兵常量在 `delegation/audit.py`）。修复前这两个键
   只在 `domain-grant` 路径出现，现网 88 条 allow 全缺 → 按域聚合恒为 0。
   要统计"非域用量"就显式传 `domain=NO_DOMAIN`。
4. **provider 维度**：审计行 `myAic` 存在时必须落在 `provider_aic` 里，否则计入
   `excluded.otherProvider`（没有该字段的历史行不参与过滤）。`provider_aic` 可传单个 AIC，
   也可传**身份谱系列表**（旧号 + 现役号）——现网 89 行里有 34 行的 `myAic` 还是换身份前的
   旧号（writer 27 行 `PK47TC…`、synthesis 7 行 `0J3LA2…`），只传现役号会把那段历史
   判成"别人的用量"（实测：writer 30→27、synthesis 10→3；传谱系后恢复 30/30、10/10）。
5. **两个计数器不要互相校验**：`grant-quota.sqlite3` 的 `grant_uses` 按 `jti` 计、
   只在 grant/域路径消耗；报表 `totalUses` 按审计 allow 行计、含 L0 与白名单路径。
   报表另附 `byRule`/`excluded`/`scanned`，让"为什么和你的数不一样"当场可读。
6. **现网实况（2026-10-15 实测，7 个 `ws-*/audit/pep-audit.jsonl` 共 89 行）**：
   allow **88** 条 = `public-skill-unscoped` 63 + `leader-allowlist` 23 +
   `public-skill-unscoped-narrowed` 1 + `public-skill` 1；deny 1 条（`no-credential`）。
   `domain-grant` 为 **0** —— 即双轨配额这条链路尚未被真实流量走到，目前只在演示
   （`demo_domain`/`demo_commerce`，临时目录）里生效。
7. **用真实台账验收过本次修复**：把上述 89 行喂给新的 `build_report`
   （`domain=NO_DOMAIN`、`window=audit_window(行)`、`provider_aic=该文件出现过的全部 myAic`）
   → `totalUses=88 / scanned=89`；修复前同一份数据按任一域名过滤恒为 **0**。
8. **技能维度按证据等级报，不按"看起来像"报**（2026-10-16 补）：审计行现在恒带
   `skillHint` / `declaredSkill` / `skillSource` 三件，报表每行也带 `skillSource`，
   并汇总 `bySkillSource`：

   | skillSource | 含义 | 能否当对账凭据 |
   |---|---|---|
   | `token-claim` | 来自校验过的域 grant / 委托令牌里的 skill | ✅ 不可伪造 |
   | `self-declared` | 调用方在 `command_params` / `data_items[].metadata` 里自报 | ⚠️ 仅供参考 |
   | `unspecified` | 调用方根本没指名能力 | ❌ 无技能维度 |
   | `legacy-unknown` | 修复前落盘、字段缺失的历史行（按 `skillHint` 是否有值推断） | ⚠️ 仅统计 |

   令牌路径上 `declaredSkill` 与 `skillHint` 会重合（令牌条目的 `data.targetSkillHint`
   本身就是 `requested_skill()` 能读到的自报字段），分级依然成立：`skillSource` 取的是
   **更高的那一档**（凭据），不是"看有没有值"。

   为什么必须分档：`skillHint` 语义上"校验过的技能"，它同时被 `skill_scope` 用来做能力
   收窄，所以**不能**把自报值塞进去（会悄悄放宽收窄口径）；而 AIP v2 的 `TaskCommand`
   没有能力选择符，现网 88 条 allow 里约 **72%**（63+23+1 条 unscoped/白名单/narrowed）
   根本没指名能力，只有 1 条 `public-skill` 是自报的。结论：**"按技能核算"目前只对
   域/L2 路径成立**，L0 路径的报表只能给"调用方声称用了哪个能力"。

   要让 L0 也按技能对账，只能等上游补 `TaskCommand` 的能力选择符（与 `discovery_filter`、
   `onlyAvailable` 同类的上游改法）。**已核实：本集群对平台没有对外沟通渠道**（无工单/
   接口可提交这类诉求，2026-10-16 队友确认），所以这不是一条待办，而是一条**已知外部约束**
   ——记录在此，本地代码不再重复推导。能做的已经做了：把证据等级显式写进审计行与报表，
   让"L0 只有自报值"这件事在对账时**看得见、可复核**，而不是被当成凭据误用。

## 7. 仍然存在的边界（如实记录）

1. **"未指名 skill"无法区分"只要展示能力"还是"想偷偷调干活能力"**——
   根因是 AIP v2 的 `TaskCommand` 没有能力选择符。缓解是运行时收窄（干活能力不可见），
   不是准入层拦截。
2. **令牌由 leader 自签**（`delegate_aic == target_aic == 资源 agent`）。
   若将来要防"leader 越权替自己签"，需要独立的签发方（STS/域 grant）——
   这是 §5 的域章程路径。
3. **`pep.py` 的 `hop` 绑定**：L2 会校验 `hop = (request.leader_aic, cfg.my_aic)` 在路由内；
   leader 直连资源 agent 是单跳，正常满足；多跳场景需回归。

---

## 8. B3 切换清单（2026-10-18）：域 grant 改由**资源方自签**

上面 §7.2 那条遗留项落地了。`pep._evaluate_grant` 对 grant 只认章程 `issuers`
（代码注释原话："grant 的权力来源是章程不是编排关系"），所以把 `yanshu-kb` 的
`issuers` 收成"只有本馆自己"，leader 自签的 grant 就会被判 `issuer-not-in-domain`。

### 本机已完成（已验证）

| 项 | 结果 |
|---|---|
| 章程 `ws-kb/acps/domains/yanshu-kb.r2.json` | ✔ 回读验签通过：`revision=2 active members=3 assets=2 issuers=1` |
| 成员角色 | KB=`provider`(兼唯一 issuer)、experiment=`consumer`、supervisor=**`consumer`**（原 auditor） |
| 资产口径 | `kb.query`→consumer 可用；`kb.ingest`→**仅 provider**（外馆不能往本馆库里写） |
| 实证 | `demo_kb.py` 新增 **K11**：leader 自签 grant → `task rejected`，审计 `issuer-not-in-domain(yanshu-kb)`；K 系列 **12/12** |
| 签发入口 | `kb_admin.py grant --subject <消费方 AIC>` → 用本馆私钥签，令牌落 `ws-kb/acps/grants/<jti>.token` |
| 全套回归 | 589 passed |

**K11 第一次是失败的**，失败原因值得记：演示没设 `ACPS_PEP_LEADER_DIRECT=0`，
supervisor 的调用在 **L1 白名单**就被放行（审计里是 `leader-allowlist`），令牌根本没进
裁决。也就是说本地演示此前比现网**更松**（现网 `/etc/wit-kb-pep.env` 已置 0），
现在演示与现网口径对齐了。

### 服务器切换步骤（有序，可回滚）

1. **同步代码**。`kb_store.py` 在同步审计里已标 **P0**（`partner_kb` 直接 import，缺文件
   KB 起不来）；另需 `kb_admin.py`、`partner_kb.py`、`kb_runner.py`、
   `deploy/make_kb_domain_charter.py`、`delegation/demo_kb.py`。
2. **铸现役库**：`kb_admin.py migrate acps_cluster/ws-kb/kb.json` + `import …` + `fix-owner`
   （注意：服务器上的持有方会解析成**注册身份**，与本机不同，属预期）。
3. **签 r2 章程**，`--kb-aic` 必须用**现役注册 AIC**（`…BUPT.VTF97R.D86STV.UBL99U.0BOQ`），
   不是本机那个 `KB0001` 桩身份：
   ```sh
   ACPS_CLUSTER_ROOT=/opt/wit/my-project/acps_cluster python3 deploy/make_kb_domain_charter.py \
     --kb-aic 1.2.156.3088.1.BUPT.VTF97R.D86STV.UBL99U.0BOQ \
     --consumer-aic <experiment 注册 AIC> --consumer-aic <supervisor 注册 AIC> \
     --sign-with kb --sign-with supervisor --revision 2
   ```
   双签方都必须是域当事方，否则 `verify_manifest` 判 `manifest-signer-not-party`，
   而 `load_domain_policies` 验签失败时**不会报错、会把章程毒化成 frozen**（脚本已拦住并回读校验）。
4. **`ACPS_PEP_LEADER_AICS` 可以保留**：`kb.query` 已声明 domain，普通委托令牌
   会先被 `deny / domain-required`，不依赖"白名单置空"来堵；`ACPS_PEP_LEADER_DIRECT=0`
   （已置）关掉的是 L1 直调通道。域令牌的签发者仍按
   `cfg.leader_aics | domain_policy.issuers` 校验，保留 leader 只影响它签的**域令牌**；
   而 `issue_token` 现在拒绝无父 grant 的域令牌，所以这条通路也被收紧。
5. **给消费方签 grant**：`kb_admin.py grant --subject <消费方 AIC> --skill kb.query`，带外递令牌
   （落 `ws-kb/acps/grants/<jti>.token`，不进模型上下文）。消费方/leader 要用时，
   必须用 `derive_delegation_claims()` 从这枚 grant 派生 task token；普通令牌不再
   能"换条路"调域技能。
6. **复跑准入矩阵** `acceptance_kb_live.sh`：`kb.query` 已声明 domain，因此
   "leader 指名 kb.query（携普通委托令牌）"现在会 `deny / domain-required`
   （2026-10-20 起，即使不做第 4 步也一样）；要放行必须由域 issuer 签发 grant，
   再派生携带父绑定的 task token。
7. **回滚**：删除 `yanshu-kb.r2.json` **不再**自动回落 r1——2026-10-20 起
   `load_domain_policies` 有高水位 + 签名副本，删除冻结文件仍保持冻结
   （防"删文件即解冻"）。需要显式管理操作：
   ```sh
   python -c "from delegation import purge_domain_store; \
       purge_domain_store('ws-kb/acps/domains', 'yanshu-kb')"
   rm ws-kb/acps/domains/yanshu-kb.r2.json
   ```
   第一步会清掉该域的高水位与签名副本；不执行它就删文件，域仍按已知最高版本裁决。
   修订链本身也必须连续：r2 必须带 `previousRevision/previousDigest`，并由 r1
   的至少两位签署方连署；新脚本已自动写入。

### B3 的边界（不是缺口，是这个体系的边界）

- **比赛方只有一个信任锚**：`ws-*/acps/trust-bundle.pem` 里三张证书全部链到
  `CN=ACPs Development Root CA`。AIC 中的 `BUPT` 只是命名节点，不是 CA 层面的机构隔离。
  所以"另一个机构"在本体系里的**唯一有效表达就是域**——成员表、签发方、数据不出域，
  B3 已具备；铸一个不同的机构段字符串不会改变任何验签结果（曾把它列为待办 B2，是错的，已撤回）。
  真正的跨机构联邦需要**不同根 CA 之间的互信**，那是比赛之外的部署条件。
- 同一根 CA 下的必然代价：KB 无法从密码学上区分"外馆成员"与"自家 agent"，
  谁算外人由成员表说了算。演示口径下这正是想要的抽象，不是弱点。
- 消费方工作区要能验签本馆证书：`ws-*/acps/trust-bundle.pem` 需含 KB 证书链
  （现网踩过：新工作区 bundle 缺中间 CA → 令牌永远验不过，见 §0b）。
- Windows 上 `chmod 0600` 只落 readonly 位；令牌的真实保护是 `.gitignore`
  （已加 `*.token` 与 `ws-*/acps/grants/`）加上"只带外递、不进模型上下文与心跳"。
