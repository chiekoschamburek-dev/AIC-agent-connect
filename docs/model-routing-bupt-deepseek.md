# 模型切换：北邮网关 deepseek 主用，MiniMax 保底（2026-10-19）

## 1. 改了什么

| 项 | 之前 | 现在 |
|---|---|---|
| agent 用的模型 | MiniMax-M3.1-Flash-Preview | **北邮网关 deepseek-v4-flash**（主用） |
| 能力内直连（`llm_tools.chat`） | 同上 | 同上（两条路径统一） |
| 故障时 | 报错/重试后失败 | **自动回退 MiniMax**，任务照常完成 |
| 代码位置 | 分散在 `agents.py` 与 `llm_tools.py` | 收敛到 `acps_cluster/model_router.py` |

改动文件：

- 新增 `acps_cluster/model_router.py`（路由与回退的唯一实现）
- `agents.py::build_provider` → 调 `model_router.build_provider()`
- `acps_cluster/llm_tools.py::chat` → 按目标顺序尝试
- `my-project/.env`、`deploy/.env.production` → 补 `BUPT_*` 与开关
- `acps_cluster/tests/test_llm_tools_length_contract.py` → 跟随接口变化

## 2. 配置（全在 .env）

```ini
# 主用
ACPS_MODEL_PRIMARY=bupt
BUPT_BASE_URL=https://myai.bupt.edu.cn/llm-gw/v1
BUPT_API_KEY=sk-...
BUPT_MODEL=deepseek-v4-flash
BUPT_HTTP_PROXY=            # 仅当需要校园网出口时才填

# 保底（沿用原有变量）
OPENAI_MODEL=MiniMax-M3.1-Flash-Preview
OPENAI_API_KEY=sk-cp-...
OPENAI_BASE_URL=https://api.minimax.cn/v1

# 主用失败后熔断秒数
ACPS_MODEL_PRIMARY_COOLDOWN=60
```

临时整体走保底（网关维护时）：`ACPS_MODEL_PRIMARY=none`。

## 3. 回退语义（这是设计的重点）

**按调用回退，不按启动回退。**

- 启动时**不做**"探一次然后锁死"：网关抖动是分钟级的，启动探测的结论很快过期；
- 每次调用先走主用；失败且**属于可回退类型**时，当场切保底重试这一次；
- 连续失败后熔断 `ACPS_MODEL_PRIMARY_COOLDOWN` 秒（默认 60），
  这段时间直接走保底，避免每条调用都先等一次超时。

**回退**的异常：连接失败、超时、429、5xx、鉴权失败。
**不回退**的异常：模型正常返回但内容不合契约——那是业务问题，换模型解决不了，
原样抛给上层（例如 `llm_tools` 的 `strict_length` 截断判定）。

### 两条路径的顺序差异（有意为之）

| 路径 | 策略 | 原因 |
|---|---|---|
| agent provider | 一次失败即切保底 | 宁可换模型也别让 agent 卡住 |
| `llm_tools.chat` | **同一目标先重试 2 次**再换 | 单次失败常是连接坏了；同目标内重试既修抖动又保持结果一致性 |

## 4. 必须知道的坑：推理型模型的 thinking 会吃光 max_tokens

`deepseek-v4-flash` 是推理型模型，会返回 `reasoning_content`。
实测同一条请求（要求输出 JSON）：

| max_tokens | finish_reason | 正文 | thinking |
|---|---|---|---|
| 800 | `length` | **0 字符** | 1690 字符 |
| 2000 | `stop` | 61 字符 | 779 字符 |
| 8000 | `stop` | 63 字符 | 643 字符 |

**800 时正文完全为空**——下游只会看到"解析不出 JSON"，看不出是预算问题。

项目里 `partner_experiment.py` 早为 MiniMax 踩过同一个坑并设了 24000
（`ACPS_EXP_CODEGEN_MAX_TOKENS`）。**沿用该值，不要往下调**。
`llm_tools.chat(strict_length=True)` 会把这种情况显式报出来，错误信息里带
`finish_reason`、`max_tokens`、正文字符数、thinking 字符数与**模型名**。

## 5. 实测验证（2026-10-19，全部真跑）

| 验证 | 结果 |
|---|---|
| 网关可达（DNS + `/models`） | `myai.bupt.edu.cn` → `192.0.2.41`，200 |
| 真实 chat completion | 200，1.84s，`收到` |
| **函数调用（tools）** | `finish_reason=tool_calls`，参数正确 |
| 配置解析与目标顺序 | 主用在前、保底在后 ✓ |
| agent provider 包装 | `FallbackProvider`，走主用未误回退（`primary_ok=1, fallback_used=0`） |
| **真实 agent 循环** | 2 步完成，工具真被调用，全程主用 |
| **回退真实生效** | 主用指向必挂地址 → `APIConnectionError` → 自动切 MiniMax → 仍拿到正文 |
| `llm_tools` 长度契约单测 | 5/5 通过 |
| 其他回归（deadline/arg_shapes/math） | 11/11 通过 |

## 6. 服务器部署要点

### 6.1 网关限制：只认校园网源 IP

`myai.bupt.edu.cn` 解析到 **`192.0.2.41`（校园内网）**。阿里云服务器物理不可达。
两条路：

**A. 反向隧道（推荐，保精度）**
```bash
# 笔记本（在校园网内）
bash /opt/wit/my-project/acps_cluster/deploy/start_campus_egress.sh
# 服务器 .env.production
BUPT_HTTP_PROXY=http://127.0.0.1:18888
```
隧道只对 `myai.bupt.edu.cn:443` 白名单放行（`campus_llm_proxy.py`），
不会把服务器变成通用出口。

**B. 直接走保底（最省事）**
```bash
ACPS_MODEL_PRIMARY=none
```
全集群仍用 MiniMax，行为与改动前一致——**不会因为这次切换而不可用**。

### 6.2 部署步骤

```bash
# 1) 同步三个文件到服务器
#    acps_cluster/model_router.py
#    acps_cluster/llm_tools.py
#    agents.py
# 2) 在 .env.production 补 BUPT_*（见 §2）
# 3) 先只重启一个 agent 验证
systemctl restart wit-yanshu@writer
journalctl -u wit-yanshu@writer -n 30 | grep -E '\[model\]|不可用|切'
#    期望看到：[model] 路由: 北邮网关/deepseek-v4-flash
# 4) 确认后再全量
systemctl restart wit-yanshu@{scout,synthesis,critic,contradiction,frontier,experiment}
```

### 6.3 怎么知道现在到底用的哪个模型

```bash
# 启动日志（每个 agent 各打印一次）
journalctl -u wit-yanshu@writer -n 50 | grep '\[model\]'

# 运行期回退会打这行
journalctl -u wit-yanshu@writer | grep '切保底'
```

## 7. 回滚

```bash
# 立刻回到 MiniMax（不用改代码、不用重装）
echo 'ACPS_MODEL_PRIMARY=none' >> /opt/wit/my-project/acps_cluster/deploy/.env.production
systemctl restart wit-yanshu@{scout,synthesis,writer,critic,contradiction,frontier,experiment}
```

## 8. 已知边界

1. **流式只回退一次机会**：一旦主用开始吐字就不能中途换路（会产生半截重复内容），
   所以只在"第一个事件之前"回退。
2. **模型不一致**：偶发回退会让同一批任务的模型不同（大部分主用、少数保底）。
   对答辩演示无影响；若要严格一致，把 `ACPS_MODEL_PRIMARY_COOLDOWN` 调大。
3. **`max_tokens` 语义**：两家都以"含 thinking 的总预算"计。24k 对 deepseek 实测够用，
   但**更长输出的能力（如 full 论文）仍需按需上调**。
4. **网关无 SLA**：校内网关是共享资源，高峰期可能变慢。熔断+回退是应对手段，
   但如果持续走保底，说明网关当时确实不稳，不必怀疑配置。
