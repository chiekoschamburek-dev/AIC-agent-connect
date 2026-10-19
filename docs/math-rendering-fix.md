# 数学公式渲染修复：交付前规范化定界符（2026-10-11）

## 问题

平台上生成的综述里，公式显示成被换行拆开的样子：

```
\[

p_\theta(x_{t-1}\mid x_t)

=

\mathcal{N}

\left(

\mu_\theta(x_t,t),\sigma_t^2 I

\right).

\]
```

## 定位：源文本没问题，是**定界符**与渲染器不匹配

对 supervisor / writer 的产出正文做了定界符统计（`inspect_math_rendering.py`，只读）：

| 定界符 | 出现次数（每篇） |
|---|---|
| `\[` / `\]`（display） | 40–115 对 |
| `\(` / `\)`（inline） | 14–24 对 |
| `$$...$$` | 仅个别篇里有 |
| **`\[...\]` 内部空行** | **0 处** |

**关键结论：源文本里没有空行**，用户看到的换行是**渲染器**造成的。

根因：模型用 LaTeX 定界符 `\[...\]` / `\(...\)`，
而**主流 Markdown 渲染器（marked / markdown-it / GitHub）默认只认 `$...$` 与 `$$...$$`**。
不认的定界符被当普通文字，块内换行被逐个保留 → 就成了上面那个样子。

## 修法

新增 `math_delimiters.py`：

- `\[ ... \]` → `$$ ... $$`（并去掉定界符内侧空白，避免 `$$` 与公式间留缝）
- `\( ... \)` → `$ ... $`（行内两侧去空白）
- **跳过围栏代码块与行内代码**：代码里出现 `\[` 可能是正则/转义，改了会破坏语义
- **幂等**：已规范化的文本再跑不变

## 接入点（两处）

| 位置 | 作用 |
|---|---|
| `supervisor._flush_buffered_submit()` | 提交前对**所有产物**规范化 → 覆盖终稿 + 评审报告等全部交付物 |
| `supervisor.on_run_end()` 兜底提交 | 非正常收尾时自动提交的内容同样规范化 |
| `partner_writer.write_paper()` | writer 自身返回时规范化 → **直接调用 writer 的场景**（叮当直调伙伴）也生效 |

## 验证

**纯函数单测** `tests/test_math_delimiters.py`（7 组）：
定界符全部转换 / 公式本体不变 / `$$` 与公式紧贴 / 幂等 / 围栏代码块不动 /
行内代码不动 / 空文本与原样返回。

**接入单测** `tests/test_math_normalize_wiring.py`（3 组）：
提交路径已规范化 / 兜底路径已规范化 / 对已规范化内容不二次改动。

**用真实产出正文端到端验证**（writer 最近一篇 6783 字符）：

| 指标 | 原文 | 规范化后 |
|---|---|---|
| 反斜杠 display 定界符 | 12 | **0** |
| 反斜杠 inline 定界符 | 19 | **0** |
| `$$` 数量 | 0 | **24** |
| 正文长度 | 6783 | 6721（去掉定界符内侧空白） |

**本地全量单测 342 通过（1 skip）**；服务器上两个单测文件均通过；
`supervisor` 与 `writer` 已重启并 `active`，8444/8447 监听正常。

## 测试过程中的两次自纠（记录，避免重犯）

1. 断言 `"$$\n" in out` 判断"定界符与公式间有换行"是**错的**——块级公式的
   闭 `$$` 后面本来就该换段。改为检查**邻接字符**（开 `$$` 后必须是公式首字符、
   闭 `$$` 前必须是公式末字符）。
2. 接入单测里用 `"\\[" in got` 判断"是否还有残留"也是错的——**代码块里那一行是刻意保留的**。
   改为先在**非代码区域**里查残留。

两次都是"断言写得太粗"，不是实现问题；已在测试注释里写明原因。

---

## 附：注册资源 agent 前发现的**证书链缺陷**（2026-10-11，同一轮工作）

为 CPU/GPU/KB 做"必须持令牌"改造时，端到端验证令牌**验签失败**：

```
TokenError: chain-untrusted: chain tail C=CN,O=北京邮电大学,CN=…JXN8O8.98DOO8.0EB6 is not anchored
```

**根因**：各工作区的 `trust-bundle.pem` 里**只有 2 个自签根**
（Agent CA Root Certificate、ACPs Root CA），
**缺 `Agent CA Intermediate Certificate`**——而我们的 leaf 正是它签发的。

后果链：

```
bundle 无中间 CA
→ build_certificate_chain 补不出中间证书（实测 chain 长度 = 1，只有 leaf）
→ _verify_chain「链尾为锚」分支失败（链尾是 leaf，不在锚集合）
→ 「用 bundle 锚验链尾签名」分支也失败（bundle 里没有 subject == leaf.issuer 的证书）
→ 任何委托令牌都无法验签
```

`delegation/token.py` 的设计注释写"当前集群 bundle = Root + 两个 Intermediate"，
**与实物不符**——这是文档与现实的偏差，直接导致所有委托令牌不可用。

**修复**（两类，都已落地）：

1. **数据侧**：把 `client/certificate.pem` 里的中间证书追加到各工作区
   `trust-bundle.pem`；脚本 `deploy/discovery_diagnostics/fix_trust_bundle.sh`
   （逐工作区检查+备份+追加+复核）。全部现有工作区已修复，`signing.chain` 长度 1 → 2。
2. **代码侧**：`delegation/issuer_tool.py` 改为 `include_intermediates=True`，
   使 token 的 `x5c` 带上整链——这样对"Root only"与"Root + Intermediate"两种 bundle
   形态都能锚定，不再依赖"验证方 bundle 恰好含中间 CA"这一脆弱前提。

**修复后实测**：签发 → 取回 → 验签**通过**，claims 正确
（`aud` 指向目标资源 agent、`acps_skill_id` 绑定能力、`act` 绑定调用方、
无 `acps_domain` 故不走域路径）。

**注册新 agent 时必须检查**：新工作区的 `trust-bundle.pem` 是否含中间 CA
（复核标准：`signing.chain 长度 ≥ 2`）。`ws-cpu`/`ws-gpu`/`ws-kb` 目前尚无 `acps/` 目录，
注册后要立刻用同一脚本检查。
