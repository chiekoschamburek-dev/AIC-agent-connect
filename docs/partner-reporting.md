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
       metadata = {"skillId":"research.submit"}
       payload = {"skill":"research.submit","args":{"question":...,"request_id":...,"user_scope":...}}
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
| `ACPS_FORWARD_DELEGATE_TIMEOUT` | `10` | 转发调用（`aip_delegate`）的等待秒数；超时按未知态回执，不报失败 |
| `ACPS_FORWARD_WAIT_TIMEOUT` | `5` | 转发后等受理回执的秒数 |
| `ACPS_FORWARD_COMPLETE_TIMEOUT` | `2` | 关闭对端 AIP 任务的秒数 |
| `ACPS_FORWARD_MAX_QUESTION` | `20000` | 转发正文上限（与 Supervisor 的受理上限对齐，超出会被截断） |

启用 PEP 时，用技能白名单授权参与转发的伙伴。每个 AIC 对应一条规则，多条规则以分号分隔。

```bash
ACPS_SUPERVISOR_INBOUND_SKILL_ALLOW='<伙伴AIC>=research.submit;<另一个伙伴AIC>=research.submit'
```

原发起方调用 `research.status` 时，也需要该技能的准入授权。Supervisor 还会按根任务的入口 AIC
和原发起方 AIC 检查归属。需要域 grant 审计的联调使用 [P2 的 grant 验收流程](multi-entry-p2-grant.md)。

## 失败语义

工具不会抛异常给模型，统一返回：

| `error` | 含义 |
|---|---|
| `question-required` | 没给任务描述 |
| `supervisor-aic-unavailable` | 既没配 `ACPS_SUPERVISOR_AIC`，本地也读不到 supervisor ACS |
| `report-failed` | AIP 调用失败（对端拒绝/网络/会话等），`detail` 带原因 |
| `acceptance-unconfirmed` | 等待窗口内没收到根任务号和作业号：可能是超时（对端可能已受理），回执保留已知的 AIP 任务引用 |
| `dispatch-in-progress` | 同一根任务正在启动作业，且等待超过 `ACPS_DISPATCH_WAIT_SECONDS`，可稍后用原 `request_id` 重试 |

`request_id` 由调用方复用即可实现幂等；不传时自动生成 `report-<12hex>`。

并发重试（用户连点、平台重试）会在 Supervisor 侧等第一个派发完成后再作为重放返回
`root_id` + `job_id`，不再直接报 `dispatch-in-progress`。等待上限由
`ACPS_DISPATCH_WAIT_SECONDS`（默认 `10` 秒）控制，超时才如实返回“进行中”。
转发超时属于**未知态**（`acceptance-unconfirmed`），不是失败：请用同一 `request_id`
重试，或凭回执里的 `root_id` / `source_task_id` 调 `research.status`。

## 验收

```bash
python -m pytest -q acps_cluster/tests/test_reporting.py acps_cluster/tests/test_root_orchestration.py
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

默认情况下，伙伴自动转发来自已验证外部调用方的当前任务正文。

1. `AutoForwardRequestHooks.on_partner_request` 记录 task_id/session/leader/question；
2. `AutoForwardRunHooks.wrap_run` 直接调用 supervisor 的 `research.submit`，**不跑模型**；
3. WIT 把返回值写成原 AIP 任务的产物。收到根任务号和作业号后，回执才标为已受理。
4. Supervisor 派来的子任务和集群成员之间的调用继续由接收伙伴处理。

载荷带 `source_task_id`、`source_session` 和 `source_caller`，供两条 AIP 任务对账。
`ACPS_AUTO_FORWARD=0` 可关闭自动转发。自动转发通过现有 AIP 能力交付回执。
