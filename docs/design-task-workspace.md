# 任务工作区与产物归档——设计文档

> 状态：v1 已部分上线（workspace partner 8470 serving）；**第③层"leader 本地镜像"已弃用**，
> 产物改由持令牌的 `ws.write` 写入（2026-10-26 定案，见
> [handoff-workspace-partner-access.md](handoff-workspace-partner-access.md)）· 2026-10-26
> 关系：不改 AIP/AAC 协议语义（产物仍按协议交付）；本设计是**项目内部的存储与归档层**，
> 与《[RFC_delegation_domain_draft](../../RFC_delegation_domain_draft.md)》的委托令牌（task_id 作用域 claim）配合使用。
> 参考实现：`acps_cluster/partner_workspace.py`（8470，已上线）、`tests/test_workspace_partner.py`（23 项）。

---

## 1. 背景与问题

### 1.1 产物的现状：有保留，但等于不可用

| 现状 | 实测 |
|---|---|
| 产物以 JSON 行内形式存于各 agent 的 `aip-tasks.sqlite3` | 全集群 718 任务 / 5866 事件，分散在各库 |
| `wit-task-prune` 定时裁剪 `aip_task_events` | keep-days=7 / 每任务 50 条，含 VACUUM |
| 跨 agent 无共享面 | writer 拿不到 scout 的原始检索结果，只有 leader 转述的摘要 |
| 大产物进不了上下文 | 实体产物经常只剩一段文字描述 |

**结论**：产物"有保留，但等于不可用"——不可浏览、不可共享、易被裁剪。

### 1.2 传播的现状：上下文转述

partner 之间没有共享文件系统，一切以 supervisor 为中心星型转述：

- 委派请求把上游产物**作为文本**拼进下一个委派（实测内部委派最大 ~26KB；
  平台侧叮当载荷 ≤1288 字符）；
- 产物回传同样受上下文窗口约束。

涉及 GitHub 仓库、数据集、大产物的任务因此走不通。

## 2. 目标与非目标

**目标**：产物全量归档可翻查（保留+审计）；伙伴间文件级协作；大文件脱离上下文；
任务级访问隔离。

**非目标**：不替代 AIP 协议交付（产物仍回给调用方）；不做跨机构共享（域外场景
归协作域 RFC 管辖）；不做实时检索引擎。

## 3. 总体设计（四层）

| 层 | 职责 | 状态 |
|---|---|---|
| ① 目录约定 | `ws-workspace/workspaces/<taskId>/`，任务级隔离，惰性创建 | ✅ 已上线 |
| ② workspace partner（8470） | 独占磁盘的存储 agent：六能力 + task 作用域校验 | ✅ 已上线 |
| ③ 产物写入 | 改为**持令牌的 ws.write**：伙伴写自己的产物，编排显式归档 leader 产物；supervisor 不再本地直写（2026-10-26 定案） | ✅ 改造完成 |
| ④ 保留期 | `ws.gc older_than_days` 定时回收（复用 wit-task-prune 的 systemd 模式） | 待做 |

**两层通道语义（关键决策）**：

- **归档 = 协议写入**（2026-10-26 改）：产物归档走 workspace partner 的 `ws.write`，
  调用方身份由令牌 `act.aic` 派生。原设计让 supervisor 直写磁盘（与写 ledger/jobs
  同级的基础设施职责），实测这条旁路让 workspace 服务与 DB 都看不到产物、
  actor 也永远是 supervisor——已删除，改由持令牌的伙伴/编排写入；
- **协作 = 协议**：伙伴间文件级读写走 workspace partner 的 ws.read/write（凭令牌、
  task 作用域收紧、PEP 审计留痕）。伙伴**不直连磁盘**，"可以访问"的含义是
  "可以经它读写"。

## 4. 详细设计

### 4.1 目录布局与命名

```text
ws-workspace/workspaces/<taskId>/
├── products/            # leader 镜像的产物归档（N.json + manifest.json）
│   └── manifest.json    # [{file, partner, capability, bytes, sha256, ts}]
├── repo/<name>/         # ws.clone_repo 克隆的仓库
└── data/…               # 伙伴经 ws.write 写入的协作文件
```

- `taskId` 使用入站 AIP 任务的天然 `taskId`（feed-… / 叮当任务自带），
  **不需要模型起名**；白名单 `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`；
- **惰性创建**：首个产物/首次写入时自动 mkdir，探活类空任务不产生目录。

### 4.2 产物写入（第③层，2026-10-26 改为令牌通道）

- ~~触发点：supervisor 收到子任务产物时直写~~（已删除：旁路写入在 workspace DB 里
  没有任务记录，`actor` 也不真实）；
- 现在的写入方：
  - **partner**：任务带了 supervisor 签的 `ws.write` 令牌（bundle）时，用自己的
    `ws_write` 工具写 `products/<partner>/…` 或 `scratch/<partner>/…`；
  - **编排**：需要归档 leader 产物时，显式委派一次 `ws.write`（`act=supervisor`，
    actor=supervisor）；
- 双写原则不变：产物仍按协议回给调用方，工作区是归档，不替代交付。

### 4.3 协作通道与 task 作用域（已上线）

- 六能力：`ws.create / ws.write / ws.read / ws.list / ws.clone_repo / ws.gc`；
- 作用域：leader 委派 workspace 时从委派参数或请求文本提取 `task_id=<id>`
  写入令牌 claim；workspace 六能力校验 claim 与请求一致，不一致/缺失/无委派
  一律拒绝（`scope-mismatch` / `scope-missing` / `scope-required`）；
- 批量回收（`older_than_days`）在作用域模型下需逐任务委派——有意为之，
  防误删；运维批量回收由 leader 直接管方通道承担。

### 4.4 生命周期

- 默认保留期 30 天（`ws.gc older_than_days=30`，systemd timer 承载，模式同
  `wit-task-prune`）；重要任务可 pin（v1 手动：不删即可）；
- 删除是唯一破坏性操作且受作用域约束；manifest 与 PEP 审计保留追溯链。

## 5. 安全考量

| 威胁 | 对策 |
|---|---|
| 路径逃逸 | 相对路径 resolve 后必须落在任务目录内；绝对路径显式拒绝；symlink 逃逸被 resolve 拦截（有测试） |
| 跨任务读写/回收 | 令牌 task_id 作用域，不一致拒绝（fail-closed）；无委派直调同样拒绝 |
| 提示词注入诱导跨任务 | 同上——模型改不了令牌 claim；注入只能得到明确拒绝 |
| 配额攻击（写满盘） | 每任务 512MB 配额，写前与克隆后复核 |
| clone 风险 | 仅 https 公开仓库、禁凭据、`--depth 1` 限时、克隆后复核 |
| 敏感产物脱敏 | 写入发生在 PEP 裁剪之后（拿到令牌的一方所见即所写），不新增泄露面 |

## 6. 实现状态

| 项 | 状态 |
|---|---|
| workspace partner（8470，六能力 + task 作用域） | ✅ 已上线（路线 B 身份 serving） |
| 令牌 task_id claim（签发器 + supervisor 提取注入） | ✅ 已上线 |
| 产物写入（③） | ✅ 改为令牌通道（伙伴/编排 ws.write）；本地镜像已删除 |
| 研究伙伴读写工作区（token bundle + ws_list/ws_read/ws_write 客户端） | ✅ 本地测试通过，待服务器联调（见 handoff） |
| 检索面（按 taskId 列产物） | 待做 |
| 保留期 timer（④） | 待做 |
| 平台注册（AIC …ABKIJE…149W 已批） | 挂起：wit 2.1.0 预证书对 IP 端点不写 IP SAN（见
  [issue-wit-2.1.0-ip-endpoint-san.md](issue-wit-2.1.0-ip-endpoint-san.md)）；不影响集群内使用 |

测试：`tests/test_workspace_partner.py` 23 项（逃逸/配额/截断/克隆闸门/作用域/名册接线）。

## 7. 开放问题

1. ~~镜像的脱敏边界~~（本地直写已删除，不再适用）；若将来由编排自动归档 leader
   产物，仍需回答"是否按 partner 配置只归档元数据"；
2. 跨机构工作区（域外伙伴共享目录）与协作域 RFC 的关系：v2 议题；
3. 产物检索：manifest 足够或需要索引（当前不建）。
