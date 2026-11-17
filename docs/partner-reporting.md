# 伙伴 → Supervisor 任务上报（不改 ACS）

## 为什么单独做

平台上的 ACS 已经锁定：任何新增技能、改 `visibility/domains`、改 `provider.name` 或
`defaultInputModes`，都会触发 `acs_fingerprint_changed_after_submission`，节点直接起不来。

"上报任务"是**集群互联机制**，不是伙伴的业务特色能力，不应该出现在 ACS/发现里。因此它
不写成 `@capability`，而是在每次 run 开始时由 hook 注入一个 **run-scoped 工具**：

```
伙伴 run 开始
  └─ ReportUpHooks.on_run_start(ctx)
       └─ ctx.add_tool(Tool(name="report_task_to_supervisor", ...))   # 只影响本次 run
模型调用 report_task_to_supervisor(question, request_id?, user_scope?)
  └─ runtime.aip_delegate(state, partner_aic=<supervisor>, request=[TextPart(JSON)])
       payload = {"skill":"research.submit","question":...,"request_id":...,"user_scope":...}
Supervisor 快速通道 research.submit
  └─ 创建根任务 + 后台作业，返回 {"ok":true,"root_id":...,"job_id":...}
```

- `compile()` / `to_acps_skill()` 完全看不到这个工具 → **ACS 投影不变**；
- Supervisor 的 `research.submit` 本来就是非公开、`acps_skill=False`，同样不改 ACS；
- 工具只在 `serve_partner` 启动的伙伴进程里注入，Supervisor 自己不会注入。

## 配置（都是环境变量/授权，不改 ACS）

| 变量 | 默认 | 作用 |
|---|---|---|
| `ACPS_REPORT_UP` | `1` | 关掉注入：`0/false/no/off` |
| `ACPS_SUPERVISOR_AIC` | 空 | 显式指定 supervisor AIC；空则读同集群 `ws-supervisor/acps/acs.final.json` |

Supervisor 侧准入二选一（任选，均为配置）：

```bash
# 方式 A：按调用方 AIC + 技能白名单（L1''，不签发 grant）
ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW=<伙伴AIC>=research.submit,<另一个伙伴AIC>=research.submit

# 方式 B：给伙伴签发 yanshu-entry 域 grant（provider=supervisor，资产 research.submit）
python -m acps_cluster.tools.entry_grant issue ...
```

PEP 关闭时（当前生产 `ACPS_PEP_ENABLED=0`）调用会直接放行，但**不会**产生 `domain-grant`
审计证据；要拿证据需开 PEP + 白名单/grant。

## 失败语义

工具不会抛异常给模型，统一返回：

| `error` | 含义 |
|---|---|
| `question-required` | 没给任务描述 |
| `supervisor-aic-unavailable` | 既没配 `ACPS_SUPERVISOR_AIC`，本地也读不到 supervisor ACS |
| `report-failed` | AIP 调用失败（对端拒绝/网络/会话等），`detail` 带原因 |

`request_id` 由调用方复用即可实现幂等；不传时自动生成 `report-<12hex>`。

## 验收

```bash
python -m unittest acps_cluster.tests.test_reporting -v   # 8 项
```

覆盖：载荷构造、缺 question、上报异常、无 supervisor AIC、env 覆盖、开关、run 内不重复注入、
以及"上报工具不出现在 ACS 技能投影里"。

## 部署边界

- **伙伴侧**：新代码启动时才会注入；旧伙伴进程没有这个工具。
- **Supervisor 侧**：必须是带 `research.submit/status` 入口能力的新代码；部署它不改
  supervisor 的 ACS（入口能力 `public=False` 且无 `acps_skill`），可以先单独切 supervisor。
- 若平台注册仍锁着，伙伴不要重启；先用 `ACPS_ACS_FROM_FINAL=1`（注册草稿=平台已批 ACS）
  或等平台侧清理 registrationId 后再切。
## 全量自动转发（ACPS_AUTO_FORWARD）

ACPS_AUTO_FORWARD=1（默认）时，伙伴不再等模型决定，而是对所有**用户入站任务**自动转发：

1. `AutoForwardRequestHooks.on_partner_request` 记录 task_id/session/leader/question；
2. `AutoForwardRunHooks.wrap_run` 直接调用 supervisor 的 `research.submit`，**不跑模型**；
3. 把受理回执（含 source_task_id、supervisor_aic、supervisor 返回）提交回原 AIP 任务；
4. `leader_aic == supervisor` 的任务跳过（supervisor 派下来的子任务照常执行，避免死循环）。

载荷额外带 `source_task_id` / `source_session` / `source_caller`，便于两条 taskId 对账。
关掉：`ACPS_AUTO_FORWARD=0`。该模式仍不新增 ACS 能力。
