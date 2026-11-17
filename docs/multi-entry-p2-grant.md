# 多入口编排 P2（上）：域 grant 授权与问迹侧接入点

> 日期：2026-11-17｜状态：**grant 工具链已实现并离线验收；问迹代码未改（本文给最小改动点）。**
> 前置：P0 报告（为什么不能设 `acps_skill`）、P1 文档（入口能力与幂等根任务）。
> 目标：把 P1 的**按技能白名单**升级为**域 grant**——同一套"双向调用 = 双向授权"的故事，
> 且依然不动 ACS、不换 AIC。

## 1. 为什么是"非公开技能 + 域 grant"

P0 的硬结论：

- `public=True` 或 `visibility=domain` → 进 ACS → 平台要求重新注册 → **换 AIC**；
- `acps_skill` 才是 ACS 投影开关，`public=False` 只管 PEP 可见性；
- grant 的授权依据是 **grant 的 `acps_assets` + 章程 `asset_allows(role)`**；
  `cfg.domain_skills`（即 `visibility=domain` 声明）只是"已声明时的第二把锁"——
  我们**故意不声明**，所以入口技能不出现在 ACS，grant 仍然有效。

```
PEP 判定顺序（pep.py）：
  L0 公开技能 → L1 leader 直调 → L1' 整链白名单 → L1'' 按技能白名单
  → [域存储预检] → token 提取 → grant 画像（_evaluate_grant）
                                     ├─ 验签（x5c 链 + Ed25519）
                                     ├─ iss ∈ 章程 issuers
                                     ├─ sub == 调用方 AIC（mTLS 已验证）
                                     ├─ caller 是章程成员 → 角色
                                     ├─ grant 资产 ⊆ 本机 + 章程允许该角色
                                     └─ 使用侧配额（sqlite 原子）
```

## 2. 双签流程（supervisor 与问迹不在同一台机器）

```
supervisor 侧            prepare  →  draft.json（只带 supervisor 签名 1/2）
        │  带外交付（scp/IM 均可，正文不含密钥）
        ▼
问迹侧                   cosign   →  signed.json（2/2，含双方签名）
        │  带外交回
        ▼
supervisor 侧            install  →  ws-supervisor/acps/domains/yanshu-entry.r1.json
                         issue    →  grant-wenji.json（JWS，0600）
        │  带外交付
        ▼
问迹侧                   保存 0600 → 调 research.submit/status 时放进 dataItems
```

> 同一台机器（自证：supervisor ⇄ kb）可以用 `mint` 一步完成。
> 章程正文一旦签发不可改：加资产要出 r2 并让**上一版签署方连署**（`--previous`）。

## 3. 命令（真实路径）

### 3.0 一键自证（推荐先跑这个）

四步手工命令容易漏步、也容易把"签发成功"误当"授权生效"。同机自证用：

```bash
cd /opt/wit/yanshu-AIC

# ① 预检 + 双签章程 + 签发 grant + 核验（不跑模型、不花配额）
#    建议在 r1 就把问迹也列入 consumer（不需要它签名，双签仍由 supervisor + kb 完成），
#    这样问迹拿到 grant 后不需要再升 r2。
python -m acps_cluster.tools.p2_selftest \
  --supervisor-workspace acps_cluster/ws-supervisor \
  --peer-workspace acps_cluster/ws-kb \
  --extra-consumer 1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN

# ② 用这个 grant 真跑一次入口调用并轮询到终态（会创建一个后台作业）
python -m acps_cluster.tools.p2_selftest \
  --supervisor-workspace acps_cluster/ws-supervisor \
  --peer-workspace acps_cluster/ws-kb --submit
```

产物（默认 `artifacts/p2/selftest-<时间戳>/`）：

```
report.md / report.json      # 判定 + 每步证据
charter.signed.json          # 双签章程（可公开）
entry-grant.json             # grant（0600，含 JWS，勿提交/勿打印）
grant-verify.json            # grant ↔ 章程核验结果
submit.json                  # 入口调用的 probe 回执（taskId=T1, root_id, job_id）
status-01.json …             # 每次状态查询的回执
```

脚本是**幂等**的：已有兼容章程则复用（只换发 grant），不兼容则直接拒绝并提示用
`prepare/cosign/install` 出 r(n+1)，不会覆盖旧版本。

**回传给研发侧的最小证据**：`report.md`、`report.json`、`submit.json`，以及
`grep domain-grant acps_cluster/ws-supervisor/audit/pep-audit.jsonl | tail`。

常见失败：

| 现象 | 原因 | 处置 |
|---|---|---|
| `预检未通过：trust-*` | 两侧证书不同信任锚 | 统一 trust-bundle；先在问迹/研枢各跑 `openssl verify` 对照 |
| `pep-enabled` 失败 | `.env.production` 没开 PEP | 开 `ACPS_PEP_ENABLED=1` 后重启 supervisor（否则 grant 不参与裁决） |
| `已存在域 … 不兼容` | 章程成员/资产与本次参数不一致 | 走 r2：`prepare --revision 2 --previous <旧章程>` → `cosign` → `install` |
| `entry-submit` 失败 `no-credential` | grant 没附上/字段不对 | 看 `submit.json` 的 `receipt.source.grant=true` 与 `dataItems` 是否被对端剥离 |
| `root-completed` 失败 | 后台作业失败/超时 | 看 `ws-supervisor/jobs/<job_id>/log.txt`；先用 `p0_checks jobs --run` 确认底座 |

### 3.1 supervisor 侧：生成草案

```bash
cd /opt/wit/yanshu-AIC
python -m acps_cluster.tools.entry_grant prepare \
  --supervisor-workspace acps_cluster/ws-supervisor \
  --peer-aic 1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN \
  --out /tmp/yanshu-entry.draft.json
```

### 3.2 问迹侧：联签（需要问迹自己的 `acps/client` 证书与 trust-bundle）

```bash
cd /opt/wenji   # 问迹仓库根
python -m acps_cluster.tools.entry_grant cosign \
  --in /tmp/yanshu-entry.draft.json \
  --peer-workspace .wit/wenji-aip-agent \
  --out /tmp/yanshu-entry.signed.json
```

> 工具会先用问迹的 trust-bundle 校验 supervisor 的签名；两边证书必须同属一个信任锚
> （梧桐平台 CA）。若报 `chain-untrusted`，先解决信任链，不要绕过。

### 3.3 supervisor 侧：安装 + 签发 grant

```bash
python -m acps_cluster.tools.entry_grant install \
  --in /tmp/yanshu-entry.signed.json \
  --supervisor-workspace acps_cluster/ws-supervisor

python -m acps_cluster.tools.entry_grant issue \
  --charter /tmp/yanshu-entry.signed.json \
  --supervisor-workspace acps_cluster/ws-supervisor \
  --subject-aic 1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN \
  --provider-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
  --max-uses 500 --ttl-days 7 \
  --out artifacts/p2/grant-wenji.json

python -m acps_cluster.tools.entry_grant verify \
  --grant artifacts/p2/grant-wenji.json \
  --workspace acps_cluster/ws-supervisor
```

`max_uses` 是**调用次数**：每次 `research.submit` 与每次 `research.status` 各消耗 1 次。
状态查询按 5–10 分钟一次估算；要更长的窗口就调大 `--max-uses` 或定期换发 grant。

### 3.4 用 grant 发起调用（问迹机器）

```bash
python -m acps_cluster.bidirectional probe \
  --workspace .wit/wenji-aip-agent \
  --peer-aic 1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6 \
  --skill research.submit \
  --args '{"question":"分析这组实验数据并结合相关论文解释结果","request_id":"req-p2-001","user_scope":"demo"}' \
  --grant /etc/wenji/entry-grant.json
```

期望：`grant=是`、终态 `completed`，产物是受理回执（`root_id`/`job_id`）。
换错身份/过期/配额耗尽会得到 `deny`（见 §6）。

### 3.5 同机自证（supervisor ⇄ kb，不需要问迹）

```bash
python -m acps_cluster.tools.entry_grant mint \
  --supervisor-workspace acps_cluster/ws-supervisor \
  --peer-workspace acps_cluster/ws-kb \
  --extra-consumer 1.2.156.3088.1.BUPT.BFZ0FH.F2KZXJ.15ZZZG.19CN \
  --max-uses 200 --ttl-days 7 \
  --out artifacts/p2/grant-kb.json

python -m acps_cluster.bidirectional probe \
  --workspace acps_cluster/ws-kb \
  --peer-aic <SUPERVISOR_AIC> --skill research.submit \
  --args '{"question":"分析这组实验数据并结合相关论文解释结果","request_id":"req-kb-001"}' \
  --grant artifacts/p2/grant-kb.json
```

> 这条路径能**完全离线验证**从"章程双签 → grant → PEP 放行 → 入口能力"的全链路
> （单测 `test_entry_grant` 已覆盖），再上服务器只差真实证书。

## 4. 问迹侧最小改动点

### 4.1 改动清单

| 文件 | 改动 | 说明 |
|---|---|---|
| `backend/app/acps/entry_client.py` | **新增** ~90 行 | 组装 `TaskCommand`（`commandParams` + grant `dataItem`）、轮询、`complete`、取产物 JSON |
| `backend/app/acps/settings.py` | 加 5 个字段 | `entry_enabled / supervisor_aic / supervisor_endpoint / grant_file / user_scope` |
| `backend/app/main.py` | 加 2 个端点 | `POST /api/research/submit`、`GET /api/research/{root_id}` |
| `backend/tests/test_entry_client.py` | **新增** | 只测 payload 组装与错误映射（不联网） |
| 现有 AIP capability | **不改** | `wenji.ask/data_query/doc_qa/schema` 继续作为被调方执行 Supervisor 的子任务 |

**不要改**：`app/acps/agent.py`（ACS/能力声明）、`capabilities.py`、`runtime.py`。
（加技能会改 ACS 指纹；P2 不需要改声明。）

### 4.2 客户端骨架

```python
# backend/app/acps/entry_client.py
from __future__ import annotations
import json, ssl
from pathlib import Path
from typing import Any

from acps_sdk.aip.aip_rpc_client import AipRpcClient
from acps_sdk.aip.aip_base_model import (
    StructuredDataItem, TaskCommand, TaskCommandType, TextDataItem,
)

TOKEN_KEY = "acpsDelegationToken"          # 与研枢 delegation.token 的线格式一致
TOKEN_ROLE = "delegation-token"
TERMINAL = {"completed", "failed", "rejected", "canceled", "cancelled"}


class EntryClient:
    def __init__(self, *, workspace: Path, aic: str,
                 supervisor_aic: str, supervisor_endpoint: str, grant_file: Path):
        self.workspace, self.aic = workspace, aic
        self.supervisor_aic, self.supervisor_endpoint = supervisor_aic, supervisor_endpoint
        self.grant_file = grant_file

    # ── 身份：复用问迹现有 acps/client 证书 ──
    def _ssl(self) -> ssl.SSLContext:
        acps = self.workspace / "acps"
        ctx = ssl.create_default_context(cafile=str(acps / "trust-bundle.pem"))
        ctx.load_cert_chain(str(acps / "client/certificate.pem"),
                            str(acps / "client/private-key.pem"))
        return ctx

    def _grant_item(self, skill: str) -> StructuredDataItem:
        token = json.loads(self.grant_file.read_text(encoding="utf-8"))["token"]
        return StructuredDataItem(
            data={TOKEN_KEY: token, "targetAic": self.supervisor_aic,
                  "targetSkillHint": skill},
            metadata={"role": TOKEN_ROLE},
        )

    async def _call(self, skill: str, args: dict, timeout_s: float = 60) -> dict:
        client = AipRpcClient(
            partner_url=self.supervisor_endpoint, leader_id=self.aic,
            ssl_context=self._ssl(), expected_partner_aic=self.supervisor_aic,
        )
        try:
            cmd = TaskCommand(
                id=f"cmd-{uuid.uuid4()}", sentAt=_now(), senderRole="leader",
                senderId=self.aic,
                dataItems=[TextDataItem(text=json.dumps(args, ensure_ascii=False)),
                           self._grant_item(skill)],
                sessionId=f"entry-{uuid.uuid4()}",
                command=TaskCommandType.Start,
                taskId=f"task-{uuid.uuid4()}",
                commandParams={"skillId": skill, "args": args},
            )
            task = (await client._send_request(cmd)).result   # SDK 未暴露 commandParams/dataItems
            while _state(task) not in TERMINAL | {"awaiting-completion"}:
                await asyncio.sleep(1.0)
                task = await client.get_task(task_id=task.taskId, session_id=cmd.sessionId)
            if _state(task) == "awaiting-completion":
                task = await client.complete_task(task_id=task.taskId, session_id=cmd.sessionId)
            text = next((item.text for p in (task.products or [])
                         for item in (p.dataItems or []) if getattr(item, "text", "")), "")
            return json.loads(text) if text.strip().startswith("{") else {"ok": False, "raw": text}
        finally:
            await client.close()

    async def submit(self, question: str, request_id: str, user_scope: str = "default") -> dict:
        return await self._call("research.submit", {
            "question": question, "request_id": request_id, "user_scope": user_scope})

    async def status(self, root_id: str) -> dict:
        return await self._call("research.status", {"root_id": root_id})
```

> `_send_request` 是 SDK 私有方法（**研枢的 probe 也用同一条路径**）：`AipRpcClient.start_task()`
> 不能带 `commandParams`/`dataItems`。SDK 升级时先跑一次 `test_entry_client` 的 payload 断言。

### 4.3 HTTP 端点骨架

```python
# backend/app/main.py（只加两个路由）
@app.post("/api/research/submit")
async def research_submit(payload: dict):
    request_id = payload.get("request_id") or uuid.uuid4().hex   # 重试必须复用
    result = await entry_client.submit(payload["question"], request_id,
                                       payload.get("user_scope", "demo"))
    return {"request_id": request_id, **result}

@app.get("/api/research/{root_id}")
async def research_status(root_id: str):
    return await entry_client.status(root_id)
```

### 4.4 配置（问迹 `.env`）

```bash
WENJI_ENTRY_ENABLED=1
WENJI_ENTRY_SUPERVISOR_AIC=1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6
WENJI_ENTRY_SUPERVISOR_ENDPOINT=https://8.160.176.215:8444/rpc
WENJI_ENTRY_GRANT_FILE=/etc/wenji/entry-grant.json     # 0600
WENJI_ENTRY_USER_SCOPE=demo
```

### 4.5 分流纪律（问迹侧）

- **入口提交只从新端点进入**；现有 AIP handler 不调用 `entry_client`，所以
  "Supervisor 派来的子任务又被提交成新需求"在结构上不会发生；
- 如果以后要自动分流，判据必须是**已验证的 `leader_aic`**：
  `== supervisor` → 执行子任务；`== 外部用户/入口` → 提交；不接受正文自报；
- 进度查询与子任务执行走不同路径：查询不占执行线程/连接池（P0 已确认问迹 handler
  是 `async + to_thread`，没有全局执行位锁）。

## 5. 与 P1 白名单的关系

- grant 到位后，把 P1 的
  `ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW=<问迹 AIC>=research.submit,research.status`
  **删掉**（或保留给联调），授权事实改由 grant 表达；
- 整链编排白名单 `ACPS_SUPERVISOR_INBOUND_ALLOW_AICS`（叮当）**保持不动**——那是
  "能不能触发完整编排"，与入口技能是两回事；
- P1 的按技能白名单保留为**应急通道**：grant 联签/换发卡住时，先靠它跑通业务。

## 6. 失败判读

| 现象（PEP 审计 `rule`/`reason`） | 含义 | 处置 |
|---|---|---|
| `deny / no-credential` | 没带 grant（或白名单未命中） | 确认 dataItem 里 `metadata.role=delegation-token` 且 token 非空 |
| `deny / invalid-token` | JWS 验签失败/过期/链不受信 | grant 是否本机签发、trust-bundle 是否含平台根 |
| `deny / issuer-not-in-domain` | 签发者不在章程 `issuers` | 章程由 supervisor 签，检查成员表 |
| `deny / grant-subject-mismatch` | 拿别人的 grant | grant `sub` 必须是调用方自己的 AIC |
| `deny / grant-not-member` | 调用方不在章程成员表 | 重新双签章程（r2） |
| `deny / skill-mismatch` | grant 资产里没有这个技能 | 只授权了 submit/status；要加技能需重签 grant |
| `deny / grant-role-not-allowed` | 章程不允许该角色用该资产 | 检查 `allowedMembers` |
| `deny / quota-exhausted` | `max_uses` 用完 | 换发 grant；或把轮询间隔拉长 |
| `deny / domain-store-missing` | 章程目录缺失 | 重新 install；注意"删文件≠解冻"（§7） |

## 7. 撤销与回滚（重要）

- **删章程文件不会回退**：加载器有高水位 + 签名副本（`.domain-state/`），删除/损坏会
  继续按已知最高版本裁决；要撤销域授权，请 install 一份 `status=frozen` 的 **r2**（双签），
  域内 grant 立即失效（策略缓存 ≤5s）；
- grant 本身无法单独吊销（无中心吊销表）：靠**短 TTL + 小 max_uses** 收敛；急停用域冻结；
- 换发 grant 不会让旧 grant 失效——旧 grant 到期/配额耗尽才自然死亡；
- 章程 r2 的连续性：必须带 `--previous` 且由上一版签署方连署（工具强制）。

## 8. 未完成

| 项 | 说明 |
|---|---|
| 问迹代码 | 本文只给改动点；实际改动在问迹仓库，需要跨团队排期 |
| grant 自动换发 | 当前手动 `issue`；高频场景需要定时任务 + 双份轮换 |
| 取消/报告/根预算 | P4 |
| 证据包按 `root_id` 关联 | P5（`root_store.platform_task_id` 已记录） |
| 双向业务链演示 | 等问迹侧接通 + Supervisor 回派（P3）；回派与按 root_id 取证的设计见 [multi-entry-p3-dispatch.md](multi-entry-p3-dispatch.md) |

## 9. 本地验收

```bash
python -m unittest acps_cluster.tests.test_entry_grant -v   # 11 项
python -m unittest acps_cluster.tests.test_p2_selftest -v    # 10 项
```

`test_entry_grant` 覆盖：双签/篡改拒绝/单签不可安装、grant 签发与核验、主体不符、
PEP 端到端（`domain-grant` 放行 / 主体不符拒绝 / 配额耗尽 / 未授权技能）、CLI 四步子命令与
同机 `mint`。全部用**临时 CA + 真实 Ed25519/x5c 验签**，不连平台。

`test_p2_selftest` 覆盖：产物 JSON 提取（含自动提交前缀）、预检缺件/齐全、状态轮询
（completed / unknown-root 提前结束）、CLI 默认不执行 `--submit`。