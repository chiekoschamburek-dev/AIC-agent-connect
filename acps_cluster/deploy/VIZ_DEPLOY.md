# 前端真实任务演示 · 部署与运行说明

> 本地已调通的三场景真实任务录制管线。**先经用户认可，再按本文件上服务器。**

## 组件关系

```
acps_cluster/delegation/viz_record.py     ← 真实任务运行器（起真伙伴 + 真 mTLS + 真 PEP + 真 LLM/GPU）
  ├─ 轨迹原文  acps_cluster/demo_run/viz/<sid>/   trace-events.jsonl · audit-*.jsonl · products/ · reconciliation.json
  └─ 场景 JSON cluster-viz/public/scenarios/{a-full,b-short,c-auth}.json + manifest.json
cluster-viz/src/App.tsx                   ← 启动时 fetch /scenarios/manifest.json，真实场景置顶（失败回退手写剧本）
```

## 本地运行（已验证）

```bash
cd my-project
python acps_cluster/delegation/viz_record.py          # 全部三场景（约 15-20 分钟，真实 LLM + CUDA）
python acps_cluster/delegation/viz_record.py b        # 只跑 B（最快，冒烟用）
VIZ_PACE=2 python acps_cluster/delegation/viz_record.py   # 放慢舞台节奏

cd ../cluster-viz
npm run dev            # http://localhost:5173 —— 「真实 · A/B/C」三个 tab 置顶
```

- B 短链路：真实 arXiv 检索（L0 审计）+ 真实 LLM 综述（~4 分钟）
- C 授权实战：无凭据/自签拒 → GT/DT 放行（真实 Docker CUDA）→ 配额烧尽拒 → 冻结双杀 → 解冻 → 双签对账（~4 分钟）
- A 全链路：检索 → 实验方案 → 域授权隧道训练（LLM 代码生成 + CUDA 沙盒）→ 审稿 → 终稿 → 对账（~10 分钟）

## 服务器部署（待用户认可后执行）

1. **依赖**：服务器需具备 Python 环境（wit/acps_sdk/venv）、`.env`（OPENAI_* 真实 key）、
   Docker + GPU 沙盒镜像、`ws-*` 工作区与本地 dev CA 证书（A 系列需要 GPU；若服务器无 GPU，
   只部署 B/C 两场景，C 的放行场景在服务器上会因沙盒不可用而 rejected——需改跑纯裁决断言）。
2. **代码同步**：`acps_cluster/delegation/viz_record.py` + `cluster-viz/`（含 public/scenarios）。
3. **构建前端**：`cd cluster-viz && npm ci && npm run build` → `dist/` 交给现有静态服务（nginx）。
4. **产物更新方式（两选一）**：
   a. 服务器上也跑 `viz_record.py`（真实栈在服务器复现一遍），JSON 落 public/scenarios；
   b. 本地跑完把 `public/scenarios/*.json` 直接同步上去（轨迹本身是数据，跨机可回放）。
   推荐先用 b（零风险），后续再切 a。
5. **回放节奏**：前端已带速度档（1×/2×/4×）与进度条；JSON 内时间轴已按 ≥1.2s/事件重排，
   不依赖录制时的真实耗时。

## 4.1 把“本次任务”链接放进叮当结果（可选）

目标：叮当调用 leader 后，结果消息里多出一条醒目的
「▶ 打开工作流可视化（自动播放）」链接；页面打开的是**本次任务**的轨迹，
不是预录的 A/B/C 演示。调用链仍是叮当 → AIP/mTLS → leader，链接只是可观测性入口。

> 叮当当前结果路径只渲染 Markdown，不支持通用全屏/iframe；因此是“点击打开新标签页”，
> 不是“调用后自动弹窗”。详见负责人评审结论。

### 4.1.1 supervisor 侧开关

在 leader 的环境（`.env.production` 或 systemd unit）设置：

```bash
ACPS_VIZ_BASE_URL="https://viz.example.com"          # 或 https://example.com/viz
ACPS_VIZ_SCENARIO_DIR="/srv/cluster-viz/scenarios"   # 静态站点真正托管的 scenarios 目录
```

- 不设 `ACPS_VIZ_BASE_URL`：完全关闭，不记录、不追加链接，行为与以前一致；
- `ACPS_VIZ_SCENARIO_DIR` 默认是仓库内 `cluster-viz/public/scenarios`；生产建议显式指向
  nginx 托管的目录（或做 rsync）。注意 `npm run build` 只会把 `public/scenarios` **拷贝一次**
  到 `dist/scenarios`；如果静态站点只服务 `dist/`，请把该变量指向 `dist/scenarios`，
  或在每次构建后再同步（否则新生成的 `run-*.json` 不会出现在站点里）；
- 目录不可写时只打一条 `[viz] Scenario 写入失败` 日志并**不追加链接**，不会产生 404 链接。

supervisor 在每次 run 的 `on_run_start` 创建记录器，记录：
指令 → 计划（若调用 plan_gate）→ 委派/技能/目标 → 委托令牌 → 等待/完成状态 → 产物 → 收尾。
只记录 leader 侧可证实的事实，不伪造伙伴侧 PEP/域事件；token 原文不写入。

### 4.1.2 cluster-viz 深链

前端支持两种入口：

```text
https://viz.example.com/?live=1                 # 固定实时地址：跟随最新 run，旧链接不会过期
https://viz.example.com/?run=<runId>&autoplay=1 # 本次任务回放
```

- `live=1`：每 1.5s 轮询 `<BASE_URL>scenarios/live.json`，自动加载最新 run；
  任务开始前显示“实时等待”，任务开始后自动出现，**不需要等终稿**；
  本轮 `done` 后继续轮询，下一次 supervisor 调用会自动切到新 run；
  时间轴按服务端 `started_at` 的挂钟推进，不依赖事件到达——长工具执行期间时钟/进度也会继续走；
- `run`：拉取 `<BASE_URL>scenarios/<runId>.json`，任务进行中每 2.5s 增量刷新；
- `autoplay=1` 自动播放（`autoplay=0` 可关）；
- 找不到时显示“未找到本次工作流”并回退内置场景，不会白屏。

leader 的 AIP 产物现在会同时给两个链接：

```markdown
[▶ 实时工作流总览（自动跟随最新任务）](.../?live=1)
[↺ 回放本次任务](.../?run=run-...&autoplay=1)
```

因此**旧回答里的实时链接一直有效**，会跟着最新一轮 supervisor 任务走；只有“回放本次”的
`run` 链接是固定到单次任务的。

若站点部署在子路径（如 `https://example.com/viz/`）：

1. `vite.config.ts` 设 `base: '/viz/'`；
2. 重新 `npm run build`；
3. `ACPS_VIZ_BASE_URL="https://example.com/viz"`（不要带尾斜杠也可，代码会处理）。

### 4.1.3 验证清单

```text
1. 设置 ACPS_VIZ_BASE_URL + ACPS_VIZ_SCENARIO_DIR，重启 supervisor；
2. 打开 ACPS_VIZ_BASE_URL/?live=1，页面应显示“实时等待”；
3. 从叮当调用一次 leader，页面应在终稿出现前就自动切到本轮 run；
4. 检查结果里的两个链接：实时总览（?live=1）与本次回放（?run=...）；
5. 在服务器上确认文件已生成：ls "$ACPS_VIZ_SCENARIO_DIR"/run-*.json 和 live.json；
6. 用手机/另一台机器打开同一链接，确认公网可达（页面是用户浏览器打开的）。
```

### 4.1.4 安全与清理

- Scenario 只含 AIC/技能/状态/产物名，不含 token、私钥、完整审计原因；
- runId 是随机 12 hex，不可枚举；如需更强隔离可加签短时 token（当前比赛演示不需要）；
- 文件会按任务累积；建议加 systemd timer 或 cron 清理 7 天前的 `run-*.json`；
  `live.json` 保留最新指针，不要跟着删；
- 不要把 `ACPS_VIZ_SCENARIO_DIR` 指到公开可写的目录。

### 4.1.5 2026-10-29 例子 A 组件补齐

- `domain` 事件新增可选 `charter`（成员/资产/签发人/双签/哈希/有效期）。内置
  `A · 论文复现全链路` 与 `C · 授权实战` 已带完整章程；点画布上的域卡或
  “查看章程 →” 打开右侧抽屉，抽屉里同时列出该域的 Grant / Task Token / PEP /
  对账事件。旧录制数据没有 `charter` 时只显示摘要并提示“未上传章程明细”。
- `discover` 结果新增“发现候选”栏（按 AIC 去重，Agent 上线后打“已上线”）。
- 边标签按 Grant/Token/PEP/资源/委派优先级做碰撞避让；画布高度改为自适应
  （1600×1000 下完成横幅不再被 880px 最小高度顶出视口）。

## 4.2 本地快速验证（不依赖叮当）

在 `my-project` 下（跑一次本地 supervisor 任务，生成 `run-*.json`）：

```bash
ACPS_VIZ_BASE_URL="http://localhost:5178" \
ACPS_VIZ_SCENARIO_DIR="../cluster-viz/public/scenarios" \
ACPS_SUPERVISOR_NO_SERVE=1 \
python acps_cluster/agents/supervisor.py "写一份图注意力网络的简短综述"
cd ../cluster-viz && npm run dev
```

取最新 runId：

```bash
ls -t ../cluster-viz/public/scenarios/run-*.json | head -1
```

打开 `http://localhost:5178/?run=<runId>&autoplay=1`。

## 已知边界（诚实口径）

- leader 侧为代码侧确定性编排（与 demo_autoresearch_e2e 同款）；伙伴侧全部真实 LLM/沙盒。
- GPU 任务由 gpu 侧 LLM 转写任务文本为 JobSpec，偶发单次失败（重跑即恢复）。
- 场景 JSON 的时间轴是重排的「叙事时间」，轨迹原文（trace-events.jsonl 实测墙钟 + 审计行）在 demo_run/viz/ 留档可查。
