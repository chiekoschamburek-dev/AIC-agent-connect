# 代码审查 12 项问题的修复记录（2026-10-11）

> 输入：用户提交的 12 项审查（P0-1…P0-5 + P1 择要）。逐条核实结论与修复实现如下。
> 核实阶段全部为只读；修复阶段每项都配了单测，**本地全量单测 340 通过（1 skip）**。

## 一、核实结论（只读阶段）

| # | 审查结论 | 我的复核 |
|---|---|---|
| P0-1 | `_wait_rounds` 建在工厂函数内 → 恒为 1 → `give_up` 死代码 → 烧光 200 步 → 兜底因 status 不符直接 return | ✅ **成立**。`_wait_rounds` 在 `_make_wait_partner_tool` 内，而 `on_run_start` 每次 `build_run_config()` 重建工具 → 闭包每轮新建 |
| P0-2 | 自愈重注上限死代码 → 收件箱任务无限重跑 | ✅ **成立**。`attempts` 用 `glob(f"{stem}*.md")` 计数，但每次 `write_text` **覆盖**同名 → attempts 恒 ≤2 ≤ `_MAX_RESUME(2)` → 无条件 return |
| P0-3 | PEP 的 L0 放行基于调用方自报 skillId | ✅ **成立**。`requested_skill()` 只读 `command_params`/`data_items[].metadata`；`evaluate` 里 `elif skill in cfg.public_skills: return allow` 在 L1/L2 之前 |
| P0-4 | critic 的 `citation.verify` 无准入钩子 | ✅ **成立**且更精确：该能力 `public=False`，但 `aip_hooks` 只有 Guard+TokenScrub。另：同文件 `paper.review` 是 `public=True`（公开可调属设计） |
| P0-5 | 心跳投递失败仍推进位点 | ✅ **成立**。`producer.send()` 的 Future 丢弃、`flush()` 返回值不检查、随后无条件推进位点。运行态 `NRestarts=0`、已连续投递 → 属潜伏 |
| P1 | 文件列表只 glob 一次 / mirror 无 per-file try / 提前提交即永久丢终稿 / PEP 审计被抹 / 事件库二次方增长 / eval_runner 只捕 TimeoutError / 配置漂移 | ✅ **全部成立**。事件库归因也确认：**outbound 9853 条 / 479.8 MB（100.0%）**，inbound 仅 161 条 / 0.2 MB |

**两处更正**：
1. 审计被抹的原因不是"崩溃重启"——`NRestarts` 全部为 **0**，是**手工 `systemctl restart`** 触发的 `ExecStartPre`。
2. `env.cluster.append` 的路径是 `my-project/acps_cluster/deploy/env.cluster.append`（行号 :35 与审查一致）。

## 二、修复实现

### P0-1 耐心计数（生产故障链）

- `_wait_rounds` 提为**模块级** `_WAIT_ROUNDS` + `_reset_wait_rounds()`；
- 连带修 `on_run_end` 兜底：原先 `status not in ("completed","success","")` 直接 return，
  现在**撞顶（budget/limit/exhaust）也兜底交付**；提交失败则调 `aip_fail` 如实上报，
  不再"既不提交也不失败"。
- 单测 `tests/test_wait_partner_patience.py`：**每轮新建工具实例**（模拟重建），
  验证计数跨实例累计、第 3 轮正确 `give_up`、终态后清零。

### P0-4 六伙伴补 PEP 钩子

`contradiction / critic / frontier / synthesis / writer / supervisor` 补上
`build_pep_hooks`（对齐 `partner_citation.py` 写法），并补 `import os`（原先靠
`agents` 模块的隐式命名空间注入才没报错）。

> 过程中被回归测试 `test_全部集群成员都挂上了剥离钩子` 抓到 `NameError: os`
> ——**该测试正好覆盖"编译每个伙伴"**，等于提前拦住了"5 个伙伴启动即崩"。

### P0-3 按已准入 skill 收窄工具集

新增 `delegation/skill_scope.py`：

- `non_public_ids(caps)` 从本机声明取 `public=False` 的能力 id；
- `resolve_scope()` 纯函数：准入集合 = `admitted`（L0 自报命中）∪ `token_skill`（L2 委托令牌里**签名校验过**的授权）；
- `SkillScopedToolsHooks` 在 run 开始时从 `ToolManager` **移除**不在准入集合里的非公开工具
  （`public=False` 只影响 ACS 可见性，原先不影响运行时工具集——这正是威胁所在）；
- 代理工具（`aip_*`/`acps_*`）永不移除。

装配：`build_pep_hooks(capabilities=...)` 新增可选参数（不传则不装配，向后兼容），
15 个调用点已全部传入本机能力声明。

单测 `tests/test_skill_scope.py`：6 例（识别/纯函数/真移除/无准入全移除/无非公开空操作/真实 ToolManager API）。

### P0-5 心跳位点只在投递成功后推进

`deploy/amp_heartbeat_sender.py`：`meta = producer.send(...)` → `meta.get(timeout=args.send_timeout)`，
**失败不推进位点**并打日志（前 5 次 + 每 100 次），连续零投递时显式告警；
新增 `--send-timeout`（默认 15s）。

单测 `tests/test_heartbeat_sender_offsets.py`（用假 producer，不需要 broker）：
失败不推进 → 恢复后**补投之前失败那条** → 非心跳行正常跳过。

### P1「提前提交即永久丢终稿」→ 缓冲式 aip_submit

按审查建议：`on_run_start` 用 `ctx.add_tool` **同名替换** `aip_submit` 为**缓冲版**
（模型以为提交了，实际只入缓冲），`wrap_run` 的 **finally** 里把全部缓冲**合并成一次**正式提交
（同一 id 取正文更长的那份）。

- 用 `wrap_run` 而非 `on_run_end`：前者在**任何**退出路径（含异常/超时）都执行；
- 已成功提交过则不再提交（协议只允许一次）。

单测 `tests/test_submit_buffer_merge.py`：5 例（合并为一次/终稿胜出/成功不重复/异常路径也交付/未提交不误交）。

### P1 其余

| 项 | 修法 |
|---|---|
| sender 文件列表只 glob 一次 | 移进 `while True` **每轮重扫**；`file.stat()` 加 `OSError` 保护 |
| mirror 无 per-file try | 每文件包 `try/except OSError`，竞态跳过并打日志，不再掀掉整个 mirror |
| PEP 审计被 experiment 重启抹掉 | 从 `.env.production` **注释掉全局** `ACPS_PEP_AUDIT_PATH`（各伙伴回落到 `load_pep_config` 的工作区默认路径 `<ws>/logs/pep-audit.jsonl`） |
| eval_runner 只捕 TimeoutError | 对齐 `gpu_runner`：加 `except BaseException` + `finally` 杀容器，并补 `import contextlib` |

### 附带清理

- `build_pep_hooks` 内部已返回 token-scrub 钩子，而 6 个调用点又显式加了一个
  → `aip_hooks` 里同一 id 出现两次。已去重并清理随之未使用的导入。

## 三、未做（需你决策）

1. **P0-2**（收件箱自愈死代码）：只影响 inbox 手动/演示路径，不影响叮当 AIP 路径。
   修法简单（给重试次数落独立文件，或用时间戳后缀而非覆盖同名），
   但会动 `partner_supervisor.py` 的自愈逻辑——**尚未动手，等你的意见。**
2. **P1 事件库清理**：已交付工具 `deploy/prune_task_events.py`（**默认 dry-run**）。
   实测预演（保留 2 天 + 每任务 50 条）：从 **10017 行 / 480 MB** 中可清 **8978 行**，
   库文件 542.6 → 约 60 MB。**未执行**——删除审计数据请你自己决定窗口与是否执行：
   ```bash
   cd /opt/wit/my-project/acps_cluster
   sudo -u wit /opt/wit/venv/bin/python deploy/prune_task_events.py \
        --db ws-supervisor/acps/aip-tasks.sqlite3 --keep-days 2 --keep-per-task 50 --apply
   ```
   根因在 wit 的 `to_task_result(include_history=True)`，我们只能运维清理；
   要根治需向上游反馈该默认值。
3. **P1 配置漂移**：服务器 `/opt/wit/my-project/.env` 仍是 LongCat；
   本地 `env.cluster.append:35` 的 `ACPS_PEP_LEADER_AICS` 被注释且指向废弃 AIC。
   两者都**没有现行故障**（生效的是 `deploy/.env.production`），但属隐患，未改动。

## 四、验证口径与结果

- 本地全量：`python -m unittest discover -s tests -p 'test_*.py'` → **Ran 340, OK (skipped=1)**；
- 服务器侧同一命令有 28 failures + 40 errors，**全部为环境差异**
  （例：`test_commerce` 需要 `ws-gpu` 这个本机开发身份的私钥，服务器上不存在）；
- 部署后逐项核对运行代码：`_WAIT_ROUNDS` 模块级 ✔、`wrap_run` 存在 ✔、
  `build_pep_hooks` 接受 `capabilities` ✔、`skill-scope` 钩子已挂且**无重复** ✔、
  sender 确认投递 ✔、mirror 每文件容错 ✔、eval_runner 杀容器 ✔；
- 集群全量验收：**8/8 active、身份一致、心跳新鲜、镜像在位、平台在线、问题工作区 0**；
  心跳链路 `wit-amp-mirror` / `wit-amp-sender` 均 active；平台 `aliveCount=152`。

## 五、部署期间的两次自纠

1. **P0-4 补 PEP 钩子时用了 `os.getenv` 而 5 个伙伴没有 `import os`**——
   被回归测试 `test_全部集群成员都挂上了剥离钩子` 当场抓住（它编译每个伙伴），
   否则会是"5 个伙伴启动即崩"。已补导入。
2. **去重 scrub 钩子时正则只匹配了 `*pep_hooks` 之后的形式**，
   而多数文件里 scrub 排在 `*pep_hooks` **之前**，导致第一次上传后服务器上仍是重复。
   已在本地核对 `aip_hooks=` 整行后重新上传，服务器复核 `dup=False`。

## 六、生产事故与修复：`wrap_run` 在 finally 里 return（2026-10-11 12:27 / 12:44）

### 现象

叮当侧调用研枢-Supervisor 直接失败：

```
研枢-Supervisor 失败
Agent execution failed.
```

### 定位

任务表 `local_error` 给出精确原因：

```json
{"phase":"run_finalize","type":"AttributeError",
 "message":"'NoneType' object has no attribute 'status'","step":1}
```

留痕显示 run 只走了 1 步就 `run_end status=None`，且 `aip_submit_replaced_buffering` 正常触发。

### 根因（**我引入的**）

P1「缓冲式 aip_submit」的 `wrap_run` 写成：

```python
try:
    return await handler()
finally:
    ...            # 里面有多处 return（early-return 守卫）
    return
```

**`finally` 里的 `return` 会吞掉 `try` 的返回值** —— `_run_core` 拿到 `None`，
wit 在 run_finalize 读 `.status` 直接 `AttributeError`。这不是"提交失败"，
而是**整个 run 的结果被丢掉**，所以对端只看到一句 `Agent execution failed.`。

### 修复

把 flush 逻辑抽成独立方法 `_flush_buffered_submit()`（**绝不 raise**，自带兜底 try），
`finally` 里只 `await` 它、不做任何 `return`：

```python
try:
    return await handler()
finally:
    await self._flush_buffered_submit()
```

并在方法 docstring 里写明「finally 里绝不能 return」这条铁律，附事故现象。

### 为什么原测试没抓到（我的测试设计缺陷）

`test_submit_buffer_merge` 当时只断言**提交次数**与**提交内容**，
**从未检查 `wrap_run` 的返回值**——于是"返回值被吞"这个致命行为完全在测试盲区里。
已补两个用例：

- **[6]** `wrap_run` 返回值**身份保持**（`got is sentinel`）且 `status=="completed"`；
- **[7]** 收尾提交抛异常时返回值仍身份保持。

**测试有效性已用反证确认**：把旧写法（`finally: return`）临时装回去，
该断言立即报 `NoneType`，证明这个测试真能拦住同类回归。

### 验证

| 项 | 结果 |
|---|---|
| 本地全量单测 | **Ran 340, OK (skipped=1)** |
| 真实调用（收件箱路径投递轻量任务） | **`status=completed`**，1 步，回复"…修复验证通过" |
| supervisor 日志 | 无 `AttributeError`/异常 |
| 服务 | `active`，8444 监听正常 |

> 另：12:27 与 12:44 两次失败的任务在 `aip_tasks` 里留为 `failed`（历史记录，属正常）。

## 七、第二起生产事故：缓冲版的**代理返回**被误判为"真实提交"（2026-10-11 05:42）

### 现象

任务跑完 52 步、`status=completed`，但对端只拿到 **945 字摘要**（"（本产物由研枢-Supervisor
在任务收尾时自动提交）已完成并提交。交付概览…"），**15187 字的综述正文没有交付**。

### 定位（留痕一目了然）

```
[05:42:41] → aip_submit（模型提交 3 个产物，15187 字符）
[05:42:41] ★ aip_submit_buffered            {"products": 3, "chars": 15187}   ← 缓冲成功
[05:42:41] ★ aip_submit_result              {"ok": true, "text_len": 0}       ← ★ 误判
[05:42:46] ★ aip_submit_skipped_after_success {"buffered_products": 3}        ← flush 被跳过
[05:42:46] ★ aip_submit_auto                  ← 兜底把 945 字摘要提交了
```

### 根因（**我引入的**）

缓冲版 `aip_submit` 的返回是 `{"ok": True, "state": "buffered", ...}`——这是**代理返回**，
不是真实提交。但 `on_tool_response` 只判断 `status == "success"` 就置 `state["ok"] = True`，
于是 `_flush_buffered_submit` 认为"已经提交过了"而**跳过**，
**15187 字终稿被静默丢弃**，而 `on_run_end` 的兜底摘要**抢先占掉了那唯一一次提交机会**。

### 修复（三处）

1. 缓冲版返回加**判别标记** `"buffered": True`（并在代码里写明为什么需要它）；
2. `on_tool_response` 识别该标记 → **不**置 `ok`、不累加 `text_len`；
3. `_flush_buffered_submit` 提交成功后**回写** `state["ok"]=True` 与 `state["text_len"]=n_chars`，
   让 `on_run_end` 的兜底知道任务已被占掉，不再重复提交。

### 测试与反证

新增 `tests/test_proxy_submit_not_mistaken.py`，模拟**完整链路**
（模型走缓冲 → 投递代理返回事件 → `wrap_run` flush → `on_run_end` 兜底）：

| 断言 | 结果 |
|---|---|
| 代理返回后、flush 前 `state["ok"]` 必须为 False | ✔（事故根因点） |
| 正式提交**恰好 1 次** | ✔ |
| 提交正文 **13200 字符**且含终稿、**不含摘要句** | ✔ |
| `on_run_end` 兜底不再重复提交 | ✔ |

**反证**：把判别标记临时去掉（`is_proxy = False`）→ 测试立刻报
「提交次数=0 / 提交正文 0 字符 / 终稿未进入提交内容」，兜底只交 28 字符——**与生产事故一致**。
证明该测试真能拦住同类回归。

### 教训

两起事故同源：**我给"包裹 run 的关键钩子"写测试时，只测了副作用（提交次数/内容），
没测"这条链路本身的契约"**——
第一次漏了 `wrap_run` 的返回值，第二次漏了"代理返回不等于真实提交"。
凡是**替换/包裹框架行为**的代码，测试必须覆盖框架**能观察到的那一面**（返回值、事件流）。

## 八、方案 B：L0 放宽「未指名 skill」的入场（2026-10-11 傍晚）

### 问题（叮当"完全无法调用 partner，连 L0 都不能用"）

审计给出直接证据：

```json
{"callerAic": "1.2.156.3088.1.0001.00001.EZTK03.JISTB6.0P3V",   // 叮当
 "myAic": "…VTF97R.GMT5S6.4SZM68.0LC5",                          // scout
 "decision": "deny", "rule": "no-credential", "code": -32008}
```
对应任务 `task_6dd5e0329df0` → `state=rejected`。

**根因**：L0 只有两条退路——① 未指名 skill 且 `unscoped_entry_open()`（= 本机**全部**能力公开）；
② 指名 skill 且在 `public_skills` 里。而"未指名 skill"是**平台侧常态**
（AIP v2 的 `TaskCommand` 里没有 skillId 字段，只能从 `command_params`/`data_items[].metadata` 猜）。
于是只要本机有一个非公开能力（scout 有 3 个），常态调用就被拒。对照：

- 叮当 → supervisor：`allow public-skill-unscoped`（唯一能力即公开）
- 叮当 → scout：`deny no-credential`（有非公开能力）

差别只在"本机有没有非公开能力"，与调用方是否滥用无关。

> **同时纠正一个误判**：期间一度出现"6 个伙伴离线"的结论，来源是
> `diag_alive_per_agent.py` 里**写死了过期 AIC**（synthesis 用 `YDWSUJ…`、writer 用 `WATDMX…`）。
> 用从 `acs.final.json` 读出的正确 AIC 重测：**8/8 全部收录且 `aliveMap=True`**。
> 已把该坑写进 `docs/agent-aic-roster.md`。

### 修法：入场放宽、能力面照旧收紧

- 新增 `PepConfig.unscoped_public_entry_open()`：只要求"确有公开能力"，未指名 skill 时放行，
  记 `rule=public-skill-unscoped-narrowed`（与既有 rule 区分，审计可辨）。
- **它排在 L1/L2 之后**，作为"没有凭据可用时的最后退路"，而不是优先于凭据的通道。
  这条顺序很关键——第一次实现时我把它塞进 L0，被两个测试当场抓住回归：
  `test_L1白名单leader直调`（白名单 leader 被记成公开入场，**审计规则失真**）、
  `test_裸令牌无skillId_…`（带凭据的调用绕过凭据链）。
- **能力面没有放宽**：`skill_scope.SkillScopedToolsHooks` 在"无准入 skill"时
  会把所有 `public=False` 的工具移出 `ToolManager`，模型根本看不见
  （单测第 6 组直接断言这一点）。

### 实测结果（用各伙伴真实能力声明跑 `evaluate`）

| 工作区 | 公开能力数 | 裁决 |
|---|---|---|
| scout | 3（arxiv/openalex/s2.search） | ✅ `allow / public-skill-unscoped-narrowed` |
| critic | 1（paper.review） | ✅ `allow / public-skill-unscoped-narrowed` |
| writer / synthesis / contradiction / frontier / supervisor | 全公开 | ✅ `allow / public-skill-unscoped` |
| **experiment** | **0** | ❌ `deny / no-credential`（本就无公开入口，fail-closed 保留） |

> **experiment 需要你确认**：它唯一能力 `exp.run` 在代码里**没有声明 `public=True`**，
> 所以既不在 ACS 对外公开，也不在 L0 放行。如果它应该被外部调用，
> 需要在 `partner_experiment.py` 里把 `exp.run` 声明为公开（这会影响 ACS 指纹，见部署规程）。

### 测试

- 新增 `tests/test_l0_unscoped_relaxed.py`（6 组：放宽生效 / 无公开能力仍拒 /
  指名公开 skill 不变 / 指名非公开 skill 仍拒 / 严格口径未变 / **放宽后非公开工具确实被移除**）。
- 更新 `tests/test_delegation.py` 里那条旧断言（`test_混合可见性时未指名skill按fail_closed`
  → `test_混合可见性时未指名skill放宽到收窄口径`），并补两条边界断言。
- **本地全量 342 通过（1 skip）**。
