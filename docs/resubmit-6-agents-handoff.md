# 提交稿交接：6 个 agent 的"保留平台说明书 + 补检索面"版本

> 生成日期：2026-10-09 · 依据：`_platform_snapshot/acs.live.*.json`（各 agent 的**平台现网 ACS**）
> 生成链：`make_acs.py`（从代码派生 skills）→ `build_merged_resubmits.py`（与平台文字合并）
> 校验：`validate_merged_drafts.py`（schema / 隐形字符 / 端点 / 描述保留 / 端点与载体一致）

## 1. 为什么不是直接提交门面文字（重要）

实测发现：**平台现网 ACS 里的 description 是本项目代码里没有的"能力说明书"**，
例如 critic 的 `citation.verify` 现网描述 176 字（含"经桥持令牌取源""输出 verdict 表""供 review_gate 交叉对账"），
而 `partner_critic.py` 里的对应描述只有 44 字。writer / frontier / contradiction / experiment 同理。

**直接提交门面文字 = 删掉这些说明书内容 = 降级。** 所以本批提交稿改用合并策略。

## 2. 合并策略（逐字段）

| 字段 | 取值 | 理由 |
|---|---|---|
| agent `description` | **平台现网原文** + 追加门面的"典型问法"段 | 说明书内容不丢；问法词面补上（检索增益来自词面） |
| skill `description` | **平台现网原文** | 同上（平台版更长更具体，且含硬约束） |
| skill `tags` | **并集**（平台在前，我们的补充在后） | 纯增益（我们补的是中英文用户词） |
| skill `examples` | **用我们的**（平台为空或较少） | 平台现网多为 0–3 条；我们补到 3–4 条 |
| `endPoints` / `certificate` / `provider` / `capabilities` / `securitySchemes` | **载体原样** | 与现网一致，避免任何副作用 |
| `version` | **抬一位**（避开平台 `(name, version)` 唯一约束） | 已登记版本见下表 |
| `aic` | 留空（由 registry 分配） | 与既往提交一致 |

## 3. 提交稿清单

| 文件 | agent | 版本 | 描述字数 | examples | tags 增益 |
|---|---|---|---|---|---|
| [acs.critic.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.critic.resubmit.json) | 研枢-Critic-Partner | 1.10.0 → **1.11.0** | 42 → **360** | 0 → **7** | 2→9 / 3→8 |
| [acs.writer.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.writer.resubmit.json) | 研枢-Writer-Partner | 1.0.0 → **1.1.0** | 327 → **545** | 3 → **4** | 2→10 |
| [acs.synthesis.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.synthesis.resubmit.json) | 研枢-Synthesis-Partner | 1.0.0 → **1.1.0** | 185 → **396** | 3 → **4** | 3→10 |
| [acs.frontier.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.frontier.resubmit.json) | 研枢-Frontier-Partner | 1.0.0 → **1.1.0** | 509 → **753** | 5 → **7** | 4→10 / 4→8 |
| [acs.contradiction.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.contradiction.resubmit.json) | 研枢-Contradiction-Partner | 1.0.0 → **1.1.0** | 361 → **594** | 3 → **4** | 3→8 |
| [acs.experiment.resubmit.json](D:\桌面\project\AIC智能体互联\my-project\acps_cluster\acs.experiment.resubmit.json) | 研枢-Experiment-Partner | 1.0.0 → **1.1.0** | 484 → **710** | 3 → **4** | 3→9 |

校验结果（`validate_merged_drafts.py`）：**6/6 schema PASS、无隐形字符、端点与载体逐字一致、平台描述逐字保留**。

## 4. 注册与切换步骤（与 scout/supervisor 今天走的完全一致）

```bash
# ① 平台侧注册（控制台 / 或按你上次的方式），期望拿到新 AIC 并 APPROVED
# ② 本地把工作区身份切到新 AIC（在工作区副本上先签证书）
cd /opt/wit/my-project/acps_cluster
sudo -u wit TARGET_AIC=<新AIC> SOURCE_WS=$PWD/ws-critic TEST_WS=$PWD/ws-critic-idswitch \
  PUBLIC_ENDPOINT=https://192.0.2.10:8448/rpc \
  /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step2_try_ca_issue.py

# ③ 应用身份 + 搬证书 + 重启
sudo -u wit /opt/wit/venv/bin/python deploy/discovery_diagnostics/switch_step4_apply_identity.py \
  --ws ws-critic --aic <新AIC> --creds-prefix CRITIC
cp -p ws-critic-idswitch/acps/client/certificate.pem ws-critic/acps/client/certificate.pem
cp -p ws-critic-idswitch/acps/server/certificate.pem ws-critic/acps/server/certificate.pem
cp -p ws-critic-idswitch/acps/trust-bundle.pem      ws-critic/acps/trust-bundle.pem
chown wit:wit ws-critic/acps/{client,server}/certificate.pem ws-critic/acps/trust-bundle.pem
systemctl restart wit-yanshu@critic

# ④ 自检
sudo -u wit /opt/wit/venv/bin/python deploy/check_new_agent.py --ws ws-critic
```

**每个 agent 的端口对照**：critic 8448 / writer 8447 / synthesis 8446 / frontier 8450 /
contradiction 8449 / experiment 8461。

## 5. 注意事项

1. **同账号单会话**：一次只注册一个，且尽量避开在跑服务的抖动窗口（0–300s）；不然会互踢。
2. **心跳会自动跟上**：换身份后新 AIC 的心跳文件由 `wit-amp-mirror` + `wit-amp-sender` 自动采集投递
   （glob 匹配），**无需任何配置改动**；1–3 分钟后平台即认为在线。
3. **旧记录会并存**：新建记录后旧记录仍在索引里（scout 现在就是新旧两条）。
   等这批稳定后，可请平台停用旧记录，或保留（检索上会多一条重复项）。
4. **已知瑕疵（上一轮评审第 ① 条）**：critic 的 `citation.verify` 与 citation-integrity agent 共用同一 skill id，
   登记表只能存一条 → 本稿中 critic 的 `citation.verify` **描述取平台原文（176 字，正确）**，
   但 tags/examples 来自 citation-integrity 那套措辞（`citation.verify` 那条 facade 被后者覆盖）。
   影响很小（示例仍与"引用核验"语义相关），但如果要彻底干净，需要把登记表键改成 `(agent_id, skill_id)`。
5. **不急**：比赛截止已推迟，建议**一次只推 1–2 个**（先 critic，观察 1–2 天排名变化再推其余），
   避免一次性动 6 个在跑服务。
