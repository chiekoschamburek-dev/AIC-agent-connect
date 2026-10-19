# 8 个 agent 的最终身份清单（2026-10-09 16:50 验收）

> ⚠️ **2026-10-11 重要更正**：下表中 **writer / synthesis / experiment 三个 AIC 已变**。
> **唯一权威**以各工作区 `acps/acs.final.json` 里的 `aic` 为准（平台注册用的就是它）。
> 核对脚本：`deploy/discovery_diagnostics/acs_vs_heartbeat_aic.py`。
>
> 2026-10-11 更正后的当前值：
>
> | 工作区 | 当前 AIC | 下表旧值 |
> |---|---|---|
> | writer | `1.2.156.3088.1.BUPT.VTF97R.PK47TC.WSY8VG.089N` | ~~`…1OGDWX.U9K14M.0CDI`~~ |
> | synthesis | `1.2.156.3088.1.BUPT.VTF97R.0J3LA2.XB2BZ3.07U1` | ~~`…9HADYH.RLVWEP.07Y5`~~ |
> | experiment | `1.2.156.3088.1.BUPT.VTF97R.JKYLU6.I4DOBA.097G` | ~~`…WN4C3S.0C0YA7.0J4R`~~ |
>
> **教训**：`diag_alive_per_agent.py` 等诊断脚本里**写死了 AIC**，身份一变就给出错误结论
> （2026-10-11 曾据此误判"6 个伙伴离线"，实际用正确 AIC 核对是 **8/8 全部在线**）。
> 诊断脚本必须从 ACS 读 AIC，不要写死。

> 用途：答辩材料、提交表格、队友交接时**唯一权威**的 AIC 清单。
> 历史 AIC（已作废）见文末，避免新旧混用。

## 1. 当前有效身份（平台 active，服务在线）

| # | agent | AIC | 版本 | 平台描述 / examples | 端口 |
|---|---|---|---|---|---|
| 1 | 研枢-Supervisor（leader） | `1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6` | 1.2.0 | 820 字 / 2 | 8444 |
| 2 | 研枢-Scout-Partner | `1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5` | 1.11.0 | 269 字 / 15 | 8445 |
| 3 | 研枢-Synthesis-Partner | `1.2.156.3088.1.BUPT.VTF97R.9HADYH.RLVWEP.07Y5` | 1.1.0 | 396 字 / 4 | 8446 |
| 4 | 研枢-Writer-Partner | `1.2.156.3088.1.BUPT.VTF97R.1OGDWX.U9K14M.0CDI` | 1.1.0 | 545 字 / 4 | 8447 |
| 5 | 研枢-Critic-Partner | `1.2.156.3088.1.BUPT.VTF97R.M2NJ2G.A1BIB2.195J` | 1.11.0 | 360 字 / 7 | 8448 |
| 6 | 研枢-Contradiction-Partner | `1.2.156.3088.1.BUPT.VTF97R.Z7TJ57.ORGGRY.0ZFO` | 1.1.0 | 594 字 / 4 | 8449 |
| 7 | 研枢-Frontier-Partner | `1.2.156.3088.1.BUPT.VTF97R.V1E6AY.3YME2N.01K2` | 1.1.0 | 753 字 / 7 | 8450 |
| 8 | 研枢-Experiment-Partner | `1.2.156.3088.1.BUPT.VTF97R.WN4C3S.0C0YA7.0J4R` | 1.1.0 | 710 字 / 4 | 8461 |

> 版本以公开接口点验为准（`GET /registry-server/acps-atr-v2/acs/<AIC>`）：8 个都是
> scout 1.11.0 / supervisor 1.2.0 / critic 1.11.0 / writer 1.1.0 / synthesis 1.1.0 /
> experiment 1.1.0 / frontier 1.1.0 / contradiction 1.1.0。
> （发现接口的 `acsMap` 里偶尔会返回同一 agent 的旧缓存条目，版本号以公开接口为准。）

**外部端点**：`https://192.0.2.10:<端口>/rpc`（mTLS，证书 CN/SAN = 上表 AIC）。

## 2. 验收结果（`full_acceptance.py`，问题工作区 0 个）

| 检查项 | 结果 |
|---|---|
| 8 个 systemd 服务 | 全部 active |
| 证书 CN = `enrollment.json` 的 AIC | 8/8 一致 |
| 心跳新鲜度（≤300s）且内容 AIC 与身份一致 | 8/8 通过（实测 1–29s 前） |
| 心跳镜像目录 `/var/log/acps-amp/yanshu/` | 8/8 有文件且持续更新 |
| 平台按 AIC 查（首页在线查询口径） | 8/8 查得到（即"在线"） |
| 心跳链路服务 | `wit-amp-mirror` / `wit-amp-sender` 均 active |
| 平台 alive 总盘 | `aliveCount=146 / knownCount=273` |

## 3. 已作废的历史 AIC（勿再引用）

| agent | 历史 AIC | 状态 |
|---|---|---|
| Scout | `…C6HTRB.5TNX0M.17SZ` | 停用（403） |
| Supervisor | `…GGWW9N.KYWRB7.0IC8` | 停用（403） |
| Critic | `…71P6ML.DDF7EG.13B2` | 停用（403） |
| Writer | `…WATDMX.AQFZR5.0NQN` | 停用（403） |
| Synthesis | `…YDWSUJ.RBUQ6T.12KC` | 停用（403） |
| Experiment | `…5CJ349.JNZ5VU.0E5E` | 停用（403） |
| Frontier | `…ZAJ69L.IOEX2R.082D` | 停用（403） |
| Contradiction | `…0FQNAG.ZO888M.0FW5` | 停用（403） |
| arXiv 检索 Partner | `…Q1EZOG.SSJTOF.0T4Q` | 已退役（能力由 Scout 承接） |

> 平台在提交新版本时会自动停用该 agent 的旧记录，因此**每次重新注册都会换 AIC**，
> 引用身份的地方（提交表格、视频、PPT、对外文档）都要用 §1 的清单。
