# ACPS Event Channel Guard

面向智能体互联的三维事件通信风险检测器
Three-Dimensional Agent Event Communication Guard for ACPs

## 项目简介

ACPS Event Channel Guard 是一个面向 ACPs（Agent Collaboration Protocols）智能体互联场景的只读旁路安全审计原型。

传统身份认证、PEP 授权、委托令牌和协作域主要回答：

- 谁在调用；
- 是否具有调用权限；
- 调用凭证是否有效；
- 是否超过资源配额。

本项目进一步回答：

> 一个智能体即使具有合法 AIC、合法权限和有效委托令牌，其通信行为是否仍然异常？

检测器从 Storage、Timing 和 Behavioral 三个维度分析任务事件，帮助 Supervisor 发现合法权限滥用、异常通信模式、状态机异常和运行故障，并生成可解释、可追溯的风险报告。

## 主要作用

### 1. 异常通信筛查

检测智能体任务中的异常载荷、规律时间间隔和异常行为序列，为后续人工复核提供线索。

### 2. 合法权限滥用识别

PEP 可以判断一次调用是否具有权限，但合法令牌并不意味着后续行为始终正常。本模块用于发现获得合法权限后的异常高频调用、规律通信和可疑载荷。

### 3. 跨任务行为分析

单个任务通常只有少量事件，很多时间模式无法在一次任务中显现。本模块按照通信双方聚合多次任务，在跨任务会话中识别长期规律。

### 4. 运行故障辅助诊断

识别亚秒级并发、重复载荷、状态倒退和异常事件切换，帮助区分：

- 可能的通信安全风险；
- 平台重试；
- 并发派发；
- 服务重启；
- 状态机错误。

### 5. 安全审计与溯源

报告包含通信方向、通信双方、会话数量、触发规则、风险分值和具体原因，可作为安全调查与治理决策的辅助证据。

### 6. Supervisor 安全观察能力

后续可作为 Supervisor 治理层的独立旁路组件，周期性读取任务数据库、生成风险告警，并在人工确认后与 PEP 处置流程联动。

## 检测维度

### Storage：载荷通道

当前检查：

- 异常大的任务载荷；
- 长 Base64 编码片段；
- 解码后的高熵数据；
- 大量重复载荷。

主要数据来源：

- aip_task_events.event_data；
- aip_task_events.payload。

### Timing：时间通道

当前检查：

- 过度规则的调用间隔；
- 稳定的长短交替间隔；
- 跨任务、同一通信双方的规律模式。

跨任务分组键：

    direction + local_aic + remote_aic

相邻任务间隔超过 3600 秒时开启新会话。

### Behavioral：行为通道

当前检查：

- 任务状态倒退；
- completed → working 等终态恢复；
- 少数事件类型之间的机械切换；
- 与正常任务流程不一致的状态序列。

## 核心创新

### 创新一：从权限可信扩展到通信行为可信

现有治理体系主要提供：

    身份可信 + 权限可信

本模块增加：

    身份可信 + 权限可信 + 通信行为可审计

它补充了传统认证与授权机制无法覆盖的场景：智能体身份和凭证合法，但通信行为仍可能异常。

### 创新二：将三维事件通道模型适配到 ACPs

本项目参考论文《Whispering Agents: An Event-Driven Covert Communication Protocol for the Internet of Agents》提出的 Covert Event Channel 威胁模型，并将其映射到真实 ACPs 数据结构：

| 通道 | ACPs 数据与特征 |
|---|---|
| Storage | event_data、payload、载荷长度、编码与信息熵 |
| Timing | created_at、跨任务调用间隔、会话时间序列 |
| Behavioral | event_type、status.state、状态跳转 |

论文中的 Storage、Timing、Behavioral 三维通道模型不是本项目原创。本项目的贡献是 ACPs 场景适配、检测规则实现、真实数据校准和治理集成设计。

参考论文：

- [AAAI Proceedings](https://ojs.aaai.org/index.php/AAAI/article/view/40380)
- [arXiv:2508.02188](https://arxiv.org/abs/2508.02188)

### 创新三：面向稀疏事件的跨任务会话检测

真实基线分析发现：

- 共分析 140 个历史任务；
- 每个任务仅包含 3–5 个事件；
- 没有任何单任务达到原 Timing 规则所需的 6 个事件。

因此本项目没有简单降低阈值，而是设计：

    按通信双方聚合
            ↓
    按 1 小时空闲间隔切分会话
            ↓
    至少 6 个有效间隔才参与安全评分

这使 Timing 分析从 0 个可用单任务扩展到 3 个主要通信方和 7 个真实可分析会话。

### 创新四：安全风险与运行突发分离

真实任务中存在亚秒级调用。系统将小于 1 秒的间隔标记为：

    operational_burst

该观察项用于提示并发派发或平台重试，但不直接增加安全风险分，避免将正常运行行为误判为攻击。

### 创新五：保守且可解释的风险判断

系统输出四种状态：

- LOW；
- MEDIUM；
- HIGH；
- INSUFFICIENT_DATA。

每条发现同时包含：

- 检测维度；
- 触发规则；
- 具体异常；
- 单项风险分；
- 样本是否充足。

样本不足时不会强行判断安全或异常。

### 创新六：只读旁路与渐进式治理

当前模块始终以 SQLite mode=ro 打开任务数据库，不修改生产任务。推荐演进路径：

    旁路观察
        ↓
    风险告警
        ↓
    人工复核
        ↓
    Supervisor 展示
        ↓
    必要时再联动 PEP 限速或撤销授权

## 系统位置

推荐部署架构：

    ACPs / 梧桐任务事件
              ↓
      aip_tasks / aip_task_events
              ↓
       ACPS Event Channel Guard
              ↓
       可解释风险报告与告警
              ↓
       Supervisor / 人工复核
              ↓
       可选的 PEP 治理处置

该模块不应放入 Research Designer、问答智能体或资源 Partner 的业务逻辑中，而应归属于 Supervisor 或统一治理层。

## 当前验证指标

| 指标 | 当前结果 |
|---|---:|
| 自动化测试 | 12 项全部通过 |
| 真实历史任务 | 140 个 |
| 单任务事件数 | 3–5 个 |
| 主要通信方调用量 | 84、30、11 次 |
| 真实可分析会话 | 7 个 |
| 真实 Timing 安全告警 | 0 |
| 亚秒运行突发 | 正确识别，未计入安全风险 |
| 规则时间阳性对照 | 成功检出 |
| 长短交替阳性对照 | 成功检出 |
| 组合 Timing 阳性结果 | HIGH 45/100 |
| 单任务三维异常样例 | HIGH 73/100 |
| 对生产数据库写操作 | 0 |
| 对心跳或 Kafka 改动 | 0 |

这些结果说明当前历史样本中未观察到 Timing 安全告警，并不等同于经过完整标注数据集验证的 0% 误报率或 100% 检出率。

## 项目结构

    .
    ├── README.md
    ├── .gitignore
    ├── acps_cluster
    │   ├── covert_event_guard.py
    │   ├── examples
    │   │   ├── covert_guard_normal.jsonl.example
    │   │   └── covert_guard_suspicious.jsonl.example
    │   └── tests
    │       └── test_covert_event_guard.py
    └── docs
        └── covert-event-guard.md

## 环境要求

- Python 3.10 或更高版本；
- 仅使用 Python 标准库；
- 无需安装额外依赖；
- 可独立运行，也可合入 AIC-agent-connect。

建议使用 Python 的 -B 参数，避免生成 .pyc 文件。

## 快速验证

运行全部测试：

    python3 -B -m unittest discover \
      -s acps_cluster/tests \
      -p 'test_covert_event_guard.py' \
      -v

预期：

    Ran 12 tests
    OK

运行正常样例：

    python3 -B -m acps_cluster.covert_event_guard \
      --input acps_cluster/examples/covert_guard_normal.jsonl.example

预期：

    Risk: LOW (0/100)

运行异常样例：

    python3 -B -m acps_cluster.covert_event_guard \
      --input acps_cluster/examples/covert_guard_suspicious.jsonl.example

预期：

    Risk: HIGH (73/100)

## 检测真实任务

检测单个任务：

    python3 -B -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 \
      --task-ref task_xxxxxxxxxxxx

批量检测单任务事件：

    python3 -B -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 \
      --limit 1000 \
      --json

跨任务 Timing 检测：

    python3 -B -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 \
      --peer-timing \
      --limit 1000

跨任务 JSON 报告：

    python3 -B -m acps_cluster.covert_event_guard \
      --db /path/to/acps/aip-tasks.sqlite3 \
      --peer-timing \
      --limit 1000 \
      --json

## 风险规则

### 单任务风险

单任务综合风险：

- 0–29：LOW；
- 30–59：MEDIUM；
- 60–100：HIGH。

### Peer Timing 风险

- 少于 4 个有效间隔：INSUFFICIENT_DATA；
- 4–5 个有效间隔：只观察，不计分；
- 至少 6 个有效间隔：允许参与 Timing 评分；
- 时间间隔 CV ≤ 0.10：增加 20 分；
- 稳定长短交替：增加 25 分；
- 小于 1 秒：记录 operational_burst，不增加安全分。

Peer Timing 等级：

- 0–19：LOW；
- 20–39：MEDIUM；
- 40–100：HIGH。

## 安全边界

当前模块：

- 不实现隐蔽通信编码或解码；
- 不读取或输出 API Key、完整令牌和私钥；
- 不执行载荷中的代码；
- 不修改任务数据库；
- 不修改心跳或 Kafka 转发；
- 不自动阻断任务；
- 不自动撤销委托令牌；
- 不自动冻结协作域；
- 不把风险结果视为攻击定论。

检测结果仅用于异常筛查，生产处置必须结合任务语义和人工复核。

## 当前限制

- 当前采用规则型检测，尚未使用机器学习或 LLM 语义检测；
- 尚未建立大规模人工标注的阳性与阴性数据集；
- 尚未统计严格的 Precision、Recall、FPR 和 TPR；
- 当前不直接解析 amp.heartbeat 或 amp.access；
- 尚未作为长期运行服务接入生产 Supervisor；
- 当前阈值来自首轮真实任务基线，仍需持续校准。

## 后续计划

1. 合入 Supervisor 治理仓库，以定时只读服务方式运行；
2. 将风险报告写入独立安全审计文件；
3. 建立人工标注数据集，统计误报率和检出率；
4. 增加按事件类型与业务场景划分的动态基线；
5. 引入通信图和跨智能体链路分析；
6. 经长期验证后，再评估与 PEP 告警、限速和授权撤销流程联动。

## 项目状态

当前版本属于经过真实历史数据回放和构造阳性对照验证的安全原型，适合：

- 代码审查；
- 历史任务审计；
- Supervisor 旁路集成测试；
- 比赛答辩与演示；
- 后续安全研究。

尚未接入生产 Supervisor 主流程，也尚未经过大规模标注数据集评估。
