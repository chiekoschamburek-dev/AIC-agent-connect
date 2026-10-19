# 研枢集群 · 最终状态报告（2026-10-09）

> 本文是"现在的真实状态"的唯一权威入口：8 个 agent 的身份、平台记录、心跳链路、
> 代码基线，以及本次同步挖出的框架约束。

## 1. 8 个 agent（全部 active、在线、可被叮当检索）

| # | agent | AIC | 版本 | 平台描述 / examples | 端口 |
|---|---|---|---|---|---|
| 1 | 研枢-Supervisor（leader） | `1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6` | 1.2.0 | 820 字 / 2 | 8444 |
| 2 | 研枢-Scout-Partner | `…VTF97R.GMT5S6.4SZM68.0LC5` | 1.11.0 | 269 字 / 15 | 8445 |
| 3 | 研枢-Synthesis-Partner | `…VTF97R.9HADYH.RLVWEP.07Y5` | 1.1.0 | 396 字 / 4 | 8446 |
| 4 | 研枢-Writer-Partner | `…VTF97R.1OGDWX.U9K14M.0CDI` | 1.1.0 | 545 字 / 4 | 8447 |
| 5 | 研枢-Critic-Partner | `…VTF97R.M2NJ2G.A1BIB2.195J` | 1.11.0 | 360 字 / 7 | 8448 |
| 6 | 研枢-Contradiction-Partner | `…VTF97R.Z7TJ57.ORGGRY.0ZFO` | 1.1.0 | 594 字 / 4 | 8449 |
| 7 | 研枢-Frontier-Partner | `…VTF97R.V1E6AY.3YME2N.01K2` | 1.1.0 | 753 字 / 7 | 8450 |
| 8 | 研枢-Experiment-Partner | `…VTF97R.WN4C3S.0C0YA7.0J4R` | 1.1.0 | 710 字 / 4 | 8461 |

端点均为 `https://192.0.2.10:<端口>/rpc`（mTLS，证书 CN/SAN = 上表 AIC）。
历史（已作废）AIC 见 `docs/agent-aic-roster.md` 第 3 节。

## 2. 心跳与在线链路

> **最终验收（2026-10-09 23:30）**：8/8 agent **可检索 + `alive=true`**；
> 语义查询命中：写论文 4 个（#2 Writer/#3 Synthesis/#5 Scout/#13 Critic）、
> 审稿 3 个（**#2 Critic**/#3 Writer/#9 Supervisor）、检索论文 3 个、新兴方向 2 个、
> 跑实验 2 个、精读 3 个、文献矛盾 3 个。
> 唯一未命中：单问「文献综述」返回结果整体偏少（平台侧排序，与本集群健康无关）。

```
agent 写 ws-*/logs/amp_heartbeat_<name>.jsonl
   → wit-amp-mirror   （15s 同步到 /var/log/acps-amp/yanshu/）
   → wit-amp-sender   （15s 投递到 Kafka amp.heartbeat）
   → 平台 monitor-server → alive-sync → discovery alive 视图
```

- 两个服务均 active、enabled；
- 实测心跳新鲜度 1–29 秒；平台 `aliveCount=146 / knownCount=277`；
- **停掉 `wit-amp-sender` 会让平台在 1–2 分钟内把全部 agent 判为离线** —— 比赛期间不要停。

## 3. 代码基线（服务器 = 本地，已同步）

> **模型 provider（2026-10-10 更换）**：服务器与本地均已切到
> **MiniMax**：`OPENAI_BASE_URL=https://api.minimax.cn/v1`、`OPENAI_MODEL=MiniMax-M3.1-Flash-Preview`。
> 换的原因：原 LongCat 账号**额度耗尽**（`HTTP 402 AppId:**EK3Y Usage limit reached`），
> 8 个 agent 一度完全无法调用模型；而校内网关 `myai.bupt.edu.cn` → CNAME `vn.bupt.edu.cn`
> → **`192.0.2.41`（校园内网）**，阿里云服务器既无内网路由也无 IPv6，**物理不可达**。
>
> 踩到的坑（写进纪律）：从本地复制的 key **开头带 UTF-8 BOM**，导致
> `login fail (1004)`；必须 `\xef\xbb\xbf` 剥掉。另外模型名里**不能有空格**
> （`MiniMax M3.1-Flash-Preview` ✘ → `MiniMax-M3.1-Flash-Preview` ✔），
> 且 base_url 要用 **`/v1`**（`/anthropic` 是 Claude Code 专用端点，走 OpenAI 协议的代码用不了）。


| 层 | 内容 | 状态 |
|---|---|---|
| 框架 | `wit_framework 2.1.0` / `acps_sdk 2.2.0`（官方 wheel） | **与本地逐文件一致（98/98），我们没有改框架** |
| 运行时补丁 | `RunConfig` 工具超时、`discovery_filter`（平台已废弃 filter 字段） | 在 `cluster_common.py` 内，服务器已生效 |
| 应用层 | `delegation/`（PEP/委托令牌/协作域/配额/对账）、7 个 partner、supervisor、gates | **已全部部署** |
| 安全修复 | `aip_guard` 壳标记中和、`cluster_common` 对端身份派生 | **已上线** |
| 声明层 | `discovery_metadata.py` + `registered_acs_overlay.json`（注册文本覆盖表） | **已上线** |

## 4. 本次挖出的框架约束（最重要的一条）

**wit 的 ACS 指纹保护**：agent 注册完成后，代码投影出的 ACS 必须与注册版逐字一致，
否则拒绝启动（`acs_fingerprint_changed_after_submission`）。

两个坑：

1. **顺序坑**：带门面的代码必须在"提交新 ACS + 换身份"**之后**部署；
2. **version 坑**（隐蔽）：wit 在 enroll 与 serve 两阶段对 `skill.version` 的投影不同
   → 同一份代码算出两个指纹 → 无改动重启也会被拦。
   已通过"覆盖表里一并补 `version`"根治。

完整机制、工具链与标准部署流程见 **`docs/acs-fingerprint-guard-and-deploy-runbook.md`**。

## 5. 备份与回滚材料

| 材料 | 位置 |
|---|---|
| 同步前服务器代码 | `/opt/wit/sync-backup-20261009-155452/acps_cluster-code.tar.gz` |
| 被覆盖的 20 个文件 | `/opt/wit/sync-backup-20261009-155452/overwritten/` |
| scout / supervisor 昨日工作区 | `/opt/wit/id-switch-backup-20261009-124140/` |
| 换身份前状态 | `ws-*/acps/*.pre-idswitch-<时间戳>` |
| 指纹修复前状态 | `ws-*/acps/*.pre-guardfix-<时间戳>` |
| enrollment 重建前 | `ws-*/acps/*.pre-rebuild-<时间戳>` |

## 6. 已知的"多余的平台记录"（不影响使用，供知悉）

排查过程中 wit 曾自动重新注册出 4 个**新 AIC**（现已全部切回原 AIC，这些记录虽 `active` 但无人使用）：
scout `…I8C7ZM.CNE8J2.0PWV`、critic `…B0A8L0.1R65NK.0BYB`、
frontier `…BS9QTH.KPV1W1.0Y4B`、contradiction `…X5NA66.ZKFLRV.1BT2`。
它们不在平台 alive 索引里，因此不会被检索到。**若平台支持停用记录，可清理掉以保持索引干净**；
不清理也无副作用。

## 7. 日常运维命令

```bash
CL=/opt/wit/my-project/acps_cluster

# 全量验收（8 个 agent × 身份/心跳/镜像/在线/门面）
sudo -u wit /opt/wit/venv/bin/python $CL/deploy/discovery_diagnostics/full_acceptance.py

# 单个 agent 自检
sudo -u wit /opt/wit/venv/bin/python $CL/deploy/check_new_agent.py --ws ws-scout

# 重新注册后：重生成覆盖表 + 双验证（必须 0 不一致）
sudo -u wit /opt/wit/venv/bin/python $CL/deploy/discovery_diagnostics/build_registered_overlay.py
sudo -u wit /opt/wit/venv/bin/python $CL/deploy/discovery_diagnostics/verify_overlay_matches_registered.py
sudo -u wit /opt/wit/venv/bin/python $CL/deploy/discovery_diagnostics/verify_projection_stability.py

# 部署前闸门（临时工作区真跑 enroll，不碰生产）
sudo -u wit /opt/wit/venv/bin/python $CL/deploy/discovery_diagnostics/verify_guard_on_server.py

# 心跳链路
systemctl status wit-amp-mirror wit-amp-sender
```
