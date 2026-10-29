# Runbook：新增一个 agent（注册 → 上线 → 可被平台发现/调用）

> 适用：往研枢集群里加一个新 partner/leader。
> 结论先行：**心跳上报那一环不需要任何配置改动**（镜像与发送器都是按 glob 自动发现新工作区的），
> 真正要做的只有"注册 + 身份对齐 + 门面文本"三件事。
> 自检工具：`python deploy/check_new_agent.py --ws ws-<key>`（跑完会给"该做什么"）。

---

## 0. 会/不会自动发生的

| 环节 | 是否自动 | 说明 |
|---|---|---|
| 心跳被镜像 | ✅ 自动 | `wit-amp-mirror` 每 15s 扫 `ws-*/logs/amp_heartbeat_*.jsonl`；改名前缀是 `ws-`、日志目录是 `logs/`、文件名是 `amp_heartbeat_*.jsonl` 就会被收 |
| 心跳进平台 | ✅ 自动 | `wit-amp-sender` 每 15s 扫镜像目录 `yanshu/amp_heartbeat_*.jsonl`；**新增文件不需要改代码/配置** |
| 平台注册 / 审核 / 发证 | ❌ 手动 | 见 §2 |
| 发现门面文本（description/tags/examples） | ❌ 手动 | 见 §3；不写就"能按 AIC 查到、但叮当一句话问不到" |
| 集群名册（leader 能否调度它） | ❌ 手动 | 见 §5 |

> 自动发现的两个前提（写错就不收）：**工作区名以 `ws-` 开头**、**心跳写在 `<ws>/logs/amp_heartbeat_<AIC>.jsonl`**。
> 二者都由 `cluster_common.serve_partner()` + `deploy/wit-yanshu@.service` 的约定保证，照抄现有 partner 就不会错。

---

## 1. 准备（写代码）

1. 复制一个最接近的现有 partner 作为骨架，例如 `cp partner_synthesis.py partner_mynew.py`；
2. 改四处：`WORKSPACE = Path(__file__).parent / "ws-mynew"`、`WitAgent(id=..., display_name=..., version=..., description=...)`、
   `@capability(id=...)`、`main()` 里的 `port_env` / `default_port` / `creds_prefix`；
   - **如果新 agent 的全部能力都是 domain/非公开**：发现层按 skill 行过滤，它会整体消失。
     给它在 capabilities 列表加一个 public `agent.info`（只放公开身份/职责，不列域能力）：
     ```python
     from agent_profile import make_agent_info
     AGENT_INFO = make_agent_info(
         agent_id="yanshu.mynew", display_name="研枢-新伙伴",
         summary="公开档案：只说明身份与职责。", public_skills=(),
     )
     CAPABILITIES = [*CAPABILITIES, AGENT_INFO]
     ```
     `validate_acs` 已能识别这个工厂调用。**存量已注册 agent 不要加**：新增 capability
     会改 ACS 投影，可能触发 `acs_fingerprint_changed_after_submission`；现有 agent 都已有
     public 非 domain 入口，不需要它。
3. 端口选一个没用过的（现有：8443 arXiv 退役 / 8444 supervisor / 8445 scout / 8446 synthesis /
   8447 writer / 8448 critic / 8449 contradiction / 8450 frontier / 8461 experiment / 8590 designer /
   8599 kb）；
   - **放行端口（漏了它=服务活着但端点不可达，见 `docs/incident-2026-10-14-kb-endpoint-firewalld.md`）**：
     `firewall-cmd --permanent --add-port=<端口>/tcp && firewall-cmd --reload`；放行后做
     "本机 hairpin（连公网 IP）+ 外部直连 + mTLS 200"三连测；
   - **重启该伙伴后先探活再投件**：重启到端点可连有真实窗口（`ExecStartPre` 随机 sleep 0–300s +
     启动过程），期间投件必撞 `ConnectError`；先探活通过（TCP/带证书 curl，或 `deploy/_status_probe.py`
     巡检确认服务与心跳）再投件；
4. `.env.production` 加两行凭据：`ACPS_MYNEW_USERNAME=...`、`ACPS_MYNEW_PASSWORD=...`（以及 `ACPS_MYNEW_PORT`）。

---

## 2. 注册与上线（两个路径）

### 路径 A（推荐）：让 wit 自己走注册流程

```bash
cd /opt/wit/my-project/acps_cluster
set -a; . deploy/.env.production; set +a
sudo -u wit /opt/wit/venv/bin/python partner_mynew.py      # 首次会停在 pending_review
```

- 平台若自动审核通过（我们的记录都是几分钟内自动过），进程会自动领证并开始服务；
- **同账号单会话**：平台对同一账号重复登录会踢掉旧会话，所以**不要同时手工跑两个注册**，
  且尽量错开在跑的 systemd 服务（它们的抖动窗口是 0–300s，就是这个原因）；
- 卡住时看：`journalctl -u wit-yanshu@mynew -n 50`，或前台直接跑看输出。

### 路径 B：在平台控制台先建记录（我们这次 scout/supervisor 走的就是这条）

平台先有了 `APPROVED` 的新记录（**新 AIC**、描述可能还是旧的），但本地没有对应身份。
这时要**把工作区身份切到那条记录**（不需要重新注册）：

```bash
cd /opt/wit/my-project/acps_cluster

# 1) 用平台 CA 按目标 AIC 直接签发证书（写到 -idswitch 副本）
sudo -u wit TARGET_AIC=<新AIC> SOURCE_WS=$PWD/ws-mynew TEST_WS=$PWD/ws-mynew-idswitch \
  PUBLIC_ENDPOINT=https://192.0.2.10:<端口>/rpc \
  /opt/wit/venv/bin/python deploy/archive/discovery_diagnostics/switch_step2_try_ca_issue.py

# 2) 应用身份（写 enrollment.json 三处 AIC + 用平台权威 ACS 覆盖 acs.final.json）
sudo -u wit /opt/wit/venv/bin/python deploy/archive/discovery_diagnostics/switch_step4_apply_identity.py \
  --ws ws-mynew --aic <新AIC> --creds-prefix MYNEW

# 3) 搬证书材料到线上工作区（**这一步漏了就是"服务自称新 AIC、证书却是旧的"**）
cp -p ws-mynew-idswitch/acps/client/certificate.pem ws-mynew/acps/client/certificate.pem
cp -p ws-mynew-idswitch/acps/server/certificate.pem ws-mynew/acps/server/certificate.pem
cp -p ws-mynew-idswitch/acps/trust-bundle.pem      ws-mynew/acps/trust-bundle.pem
chown wit:wit ws-mynew/acps/{client,server}/certificate.pem ws-mynew/acps/trust-bundle.pem
```

### 3) 装上 systemd 并启动

```bash
install -m 644 deploy/wit-yanshu@.service /etc/systemd/system/   # 模板已存在则跳过
systemctl daemon-reload
systemctl enable --now wit-yanshu@mynew
journalctl -u wit-yanshu@mynew -n 20 --no-follow      # 期待：常驻已上线 AIC=<新AIC>
```

---

## 3. 发现门面文本（决定"叮当一句话问得到吗"）

在 `discovery_metadata.py` 里登记两处：

1. `AGENT_FACADES["<agent id>"]`：三段式描述 —— 我是谁/做什么 → **适合：**（场景词）→ **典型问法：**（用户会怎么问，中英文都写）；
2. `CAPABILITY_FACADES["<skill id>"]`：`description`（输入→输出）、`tags`（中英文近义词 ≥3）、`examples`（用户问法 3–4 条）。

然后在 `build_*_agent()` 里套用（现有 partner 都是这个写法）：

```python
from discovery_metadata import agent_discovery_description, apply_to_capabilities
...
description=agent_discovery_description("<agent id>", "<原描述>"),
capabilities=apply_to_capabilities(CAPABILITIES),
```

改完**必须重新提交 ACS** 才会进平台索引（源码改动不会自动生效）：

```bash
python deploy/make_acs.py <key> --like <现网 ACS 快照> --out acs.<key>.resubmit.json --version <抬一位>
python deploy/check_resubmit_acs.py <key>            # 与代码逐字段比对
python deploy/validate_acs.py acs.<key>.resubmit.json
```

> ⚠️ 平台侧硬约束：已 APPROVED 的记录**不能原地改**（`registry-server/app/agent/service.py:544`）。
> 要么请平台退回可编辑状态，要么新建记录（`(name, version)` 在活跃记录间必须唯一，所以抬版本号）。
> 这也意味着**新建记录会拿到新 AIC** → 回到 §2 路径 B 做身份切换。

---

## 4. 自检（一条命令）

```bash
sudo -u wit /opt/wit/venv/bin/python deploy/check_new_agent.py --ws ws-mynew
```

它对五件事给 ✔/✘ 与处置建议：

1. `enrollment.aic` / `acs.final.aic` / 证书 CN 三者是否一致；
2. 平台记录是否 active；
3. 心跳文件是否存在、是否在 300s 新鲜度窗口内、文件名 AIC 是否与 enrollment 一致；
4. 心跳是否已出现在镜像目录 `/var/log/acps-amp/yanshu/`；
5. 平台是否认为它在线（按 AIC 查 + `aliveMap`）。

时间预期：服务起来后 **镜像 ≤15s → 投递 ≤15s → 平台 alive 生效 1–3 分钟**。

---

## 5. 集群名册（让 leader 能调度它）

- `cluster_roster.py` 的 `ROSTER` 加成员条目（key/name/role/module/workspace/port_env/default_port/kind，
  非公开能力才需要 `declared_capabilities`）；
- 公开能力由运行时从 `ws-mynew/acps/acs.final.json` 的 `skills[].id` **自动派生**，不用手写；
- 名册改动后重启 supervisor：`systemctl restart wit-yanshu@supervisor`；
- 若新 agent 需要被 leader 用委托令牌直连（remedy-3），记得在 `ACPS_PEP_LEADER_AICS` 里包含 supervisor 的 AIC。

---

## 6. 常见坑（今天都踩过，逐条对应）

| 症状 | 根因 | 处置 |
|---|---|---|
| 服务日志显示 AIC=新值，但被指名调用失败 | 证书没搬（仍是旧 AIC 的证书） | 重做 §2 路径 B 第 3 步并重启 |
| 平台按 AIC 查不到，但语义检索能查到 | 平台不认为它在线 | 查 `wit-amp-sender` 是否 active；再 `check_new_agent.py` |
| 心跳文件在写，但镜像目录里没有它 | 工作区名不以 `ws-` 开头 / 日志不在 `<ws>/logs/` | 改名或改 `WORKSPACE` |
| 镜像里有文件，但平台仍不在线 | 心跳超过 300s 新鲜度窗口（服务其实没在跑） | 先让服务真正跑起来（`systemctl status`） |
| 叮当一句话问不到（但按技能词能查到） | 门面文本太弱 | 补 §3 并重新提交 ACS |
| 注册时报 `Agents in PENDING status cannot be updated` | 平台对 PENDING 记录拒绝更新 | 等审核；别反复 enroll（会进 blocked） |
| 注册把别的在跑服务踢下线 | 平台同账号单会话 | 错开时间；systemd 抖动窗口已为此设计 |
| 备份/新工作区属主不对 | 手工 cp 用 root | `chown -R wit:wit <ws>` |
