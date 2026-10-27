# 批准后验收 Runbook（照抄执行）

> 前提：watcher（`register --watch --serve`）在跑，批准后自动完成 证书→激活→serve。
> 以下步骤在 **批准后** 按序执行；每步标注【截图】的留证。

## 0. 确认自动链路已完成（1 分钟）

```bash
cd ~/projects/aic/wenji-aip-agent
tail -30 var/register_watch.log        # 期待: active / serving
pgrep -af "app.acps.cli register"      # serve 模式下进程应存活
ss -ltn | grep 8443                    # 本机 wit 监听已起
```

【截图 ①】`register_watch.log` 中 active/serving 状态段（含 AIC）。

## 1. 公网健康检查（1 分钟）

```bash
# 服务器上：
curl -sk --cert .wit/wenji-aip-agent/acps/client/certificate.pem \
  --key .wit/wenji-aip-agent/acps/client/private-key.pem \
  --cacert .wit/wenji-aip-agent/acps/trust-bundle.pem \
  https://8.160.176.215:8601/health ; echo
# 期待 {"status":"serving","agentId":"wenji-aip-agent",...}（strict mTLS 需 client cert）
```

【截图 ②】终端全屏：命令 + 应答（注意这是运维探活端点，对外任务是 /rpc，别混）。

## 2. AMP 心跳进平台（2 分钟）

```bash
tail -5 var/amp_forwarder.log          # 期待 [ok] forwarded N record(s)
ls backend/.wit/wenji-aip-agent -R | grep amp | head
```

等 1~2 个心跳周期（15s/跳）后，从公网验证 discovery 可见性：

```bash
curl -s -X POST https://wt.ioa.pub/discovery/acps-adp-v2/discover \
  -H 'Content-Type: application/json' \
  -d '{"type":"explicit","query":"问数 数据库 文档问答","limit":20}' | python3 -m json.tool | grep -B2 -A6 "问迹"
```

【截图 ③】discovery 返回中「问迹·可解释问数问答智能体」条目（active=true，含 8601 端点）。
【截图 ④】`amp_heartbeat_*.jsonl` 内容若干行（aic 字段可见）。

## 3. AIP 自测（2 分钟）

```bash
cd backend
.venv/bin/python scripts/acps/aip_selftest.py --question "销量最高的前五首曲目是什么？"
```

期待输出：discover 命中本机 AIC → delegate → state 走到 awaiting-completion → complete → completed，
result 中含问迹的答案（The Tropper…）。

【截图 ⑤】selftest 全程终端输出（这就是"自己发现并调用自己"的完整任务状态机证据）。

## 4. 与队友智能体互联（10 分钟，视频核心段落）

```bash
# 发现队友（研枢 Supervisor 已 active）
.venv/bin/python scripts/acps/aip_selftest.py --no-delegate --query "科研 任务 调度 知识库"
# 挑一个队友 AIC 委派（把 <AIC> 换成上一步结果里的）
.venv/bin/python scripts/acps/aip_selftest.py --partner-aic <队友AIC> --no-discover --question "请介绍你能提供的能力"
```

同时请队友从他们的 Leader 侧调用「问迹」：
- 让队友执行他们的 discover（query：问数/数据库/文档问答）→ 命中问迹 → delegate
- 问迹侧留证：`tail -f var/register_watch.log`（任务受理日志）+ 前端 `http://localhost:5173` 的 Trace 视图

【截图 ⑥】队友侧 discovery 结果（含问迹）+ 委派命令与返回。
【截图 ⑦】问迹侧任务受理/执行日志。

## 5. 平台后台数据（5 分钟）

登录梧桐平台后台 → 智能体访问量/调用记录页面。

【截图 ⑧】后台访问量页面（**打码 organization 字段**）。
【截图 ⑨】AMP access 记录（若后台可见；本地对照 `amp_access_*.jsonl`，correlationId=taskId）。

## 6. 引擎演示素材（10 分钟，视频第一段）

前端 `http://localhost:5173` 依次操作并截图：

| 操作 | 期待 |
|---|---|
| "销量前十的曲目" | SQL 卡片 + 数据表 + Trace 时间线 |
| "员工年假有多少天" | 引用列表（文档/页码） |
| "2024 销售冠军是谁？总结他的方法论" | 跨源：先查库后搜文档，分区溯源答案 |
| 问"增长率"（不给时间范围） | 澄清按钮挂起 → 选择后续查 |

【截图 ⑩~⑬】各一张（Trace 面板展开）。

## 7. 时延统计（报告 5.2 节【待补】数据）

```bash
for i in 1 2 3 4 5; do curl -sk -o /dev/null -w "%{time_total}\n" https://8.160.176.215:8601/health; done
# 任务往返时延：从 amp_access_*.jsonl 取 durationMs 字段做 P50/P95
python3 - <<'EOF'
import json,glob,statistics
d=[json.loads(l)["durationMs"] for f in glob.glob("backend/.wit/wenji-aip-agent/**/amp_access_*.jsonl",recursive=True) for l in open(f) if l.strip() and "durationMs" in l]
if d: print(f"n={len(d)} P50={statistics.median(d):.0f}ms P95={sorted(d)[int(len(d)*0.95)-1]:.0f}ms max={max(d)}ms")
EOF
```

## 8. 收尾打包

- 报告：【待补】全部替换 → 导出 PDF ≤10M
- 视频：按 `plan_24h.md` 分镜表录 3~5 分钟（0:00 定位 / 0:30 引擎 / 1:30 平台注册【截图①③】/ 2:30 互联【截图⑥⑦】/ 3:30 后台数据【截图⑧⑨】/ 4:30 总结）
- PPT：按 `ppt_outline.md` 制作 → PDF
- 网盘：`团队编号-赛题名称-作品名称-XX` 命名，提交前跑脱敏校验
