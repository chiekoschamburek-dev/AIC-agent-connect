"""发现门面（discovery facade）：把"平台检索用的元数据"集中登记、统一增强。

## 为什么需要这个模块

2026-10-08 线上实测（见 `docs/issue-wt-dingdang-cannot-discover-our-agents.md`）：
平台发现服务用**语义匹配**（embedding + pgvector）决定"一句话问智能体"时谁能进结果集。
实测我们的 agent 在**技能词**检索里常常排第一（`arXiv 论文检索` → Scout 第 1；`论文评审` → Critic 第 1），
但在**自然语言问法**下经常进不了结果（`帮我找一个能做文献综述的智能体` → 我们 0 个，
而"基于多智能体协作的一站式科研助理平台""科研智能体协作工作台"类对手稳定出现）。

差别不在能力，而在**门面文本**：对手把"能做什么 + 用户会怎么问"都写进了 ACS 的
`description` / `skills[].description` / `skills[].tags` / `skills[].examples`，
我们是一句话短描述 + 空 examples + 少量英文 tag。

## 这个模块做什么

- **只改描述类字段**（`description` / `tags` / `examples`）：`id`、参数 schema、准入语义一律不动，
  因此不会改变任何调用契约，也不会改变"公开/非公开"边界（`public` / `acps_skill` 仍由各伙伴自己声明）。
- **集中登记**：所有增强写在本文件的两张表里，改文案不用翻 14 个 partner 文件，
  并能用 `unregistered_ids()` 在单测里挡住"新增能力忘了登记门面"。
- **按需生效**：`apply_to_capabilities()` 只增强**已登记**的 id，未登记的保持原样（不会报错、不会污染）。

## 怎么用

```python
from acps_cluster.core.discovery_metadata import apply_to_capabilities, agent_discovery_description

capabilities = apply_to_capabilities(SCOUT_CAPABILITIES)
WitAgent(..., description=agent_discovery_description(SPEC), capabilities=capabilities, ...)
```

改完文案要**重新提交 ACS**（源码改动本身不进平台索引，平台索引读的是注册表里那份 ACS）：

```bash
cd my-project/acps_cluster
ACPS_PLATFORM=custom . /opt/wit/...  # 见 deploy/.env.production
python partner_scout.py draft        # 先看生成的 ACS 草案，确认 description/tags/examples 生效
# 确认后再走正常注册流程提交（PENDING 记录不可更新，注意状态）
```

复核工具：`python -m discovery_metadata` 打印登记表与"未登记能力"清单。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class CapabilityFacade:
    """一个能力的检索门面：技能描述 + 关键词 + 用户问法示例。"""

    description: str
    tags: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentFacade:
    """一个 agent 的检索门面：平台索引主要用这段文本匹配"一句话问法"。

    `search_surface` 用于"描述本身已经是长文"的 agent（如 supervisor）：不替换原文
    （那会丢掉对内/对评委都有价值的细节），而是把面向检索的问法语追加到原文之后。
    """

    description: str
    tags: tuple[str, ...] = ()
    search_surface: str = ""


# ── agent 级门面（写进 ACS 顶层 description；平台把它当该 agent 的主匹配文本）──
# 写法要求：先一句"我是谁/做什么领域"，再列"用户会怎么问"的典型说法（含中英文近义词）。
# 长度控制在 400 字以内，避免平台侧截断或降权。
AGENT_FACADES: dict[str, AgentFacade] = {
    "yanshu.workspace-partner": AgentFacade(
        description=("研枢集群的任务工作区管理员：为多智能体协作按任务开辟服务器端工作区目录，让伙伴之间以文件交换代替上下文传话——文件不进模型上下文，只有路径和摘要进上下文。能力六项：ws.create 按任务开辟工作区；ws.write 写入文本或 base64 二进制（原子写）；ws.read 读取文件（默认 ≤64KB，超限截断并如实标注 truncated）；ws.list 输出目录树（posix 相对路径、类型、字节，≤200 项）；ws.clone_repo 浅克隆公开 https 仓库（--depth 1，返回文件树与 README 摘要）；ws.gc 按 task_id 或按 mtime 天数批量回收。安全边界：task_id 白名单校验；相对路径 resolve 后必须落在任务目录内，绝对路径与目录逃逸显式拒绝；每任务配额 512MB，写前与克隆后复核；克隆仅接受 https 公开仓库、禁用户凭据、限时 120 秒。典型场景：任务涉及 GitHub 仓库或大数据集时，编排第一步先建工作区并克隆仓库，后续检索/实验/写作伙伴按路径取用，交付物也落在同一工作区。适合作为多智能体系统的共享工作面/文件总线使用。"),
    ),
    "yanshu.supervisor": AgentFacade(
        description=(
            "科研任务总控与多智能体调度（Leader）：把一句话研究需求拆成可执行计划，"
            "调度文献检索、论文精读、矛盾侦测、前沿探测、论文写作、审稿与引用核验、实验执行等专职智能体协作交付。"
            "适合：做文献综述 / 写一篇论文 / 审稿与改稿 / 选题与前沿调研 / 研究方案设计与实验验证 / 多步骤科研任务编排。"
            "典型问法：帮我完成一篇综述、帮我做一次科研调研、帮我写论文并审稿、帮我验证一个研究假设、"
            "帮我安排一整套研究流程、research assistant、literature review、paper writing、peer review、research orchestration。"
        ),
        tags=("科研协作", "任务编排", "leader", "supervisor", "research"),
        # supervisor 的原描述承载调度语义与代码闸门说明（对评委与对内都有价值），
        # 因此门面以"追加检索面"的方式生效，而不是替换原文。
        search_surface=(
            "【找智能体时看这里】我是这个科研智能体集群的总入口：一句话交给我一个研究任务，"
            "我会自动拆解并调度文献检索、论文精读、矛盾侦测、前沿探测、论文写作、审稿与引用核验、"
            "实验执行等专职智能体协作完成。适合：做文献综述 / 写论文 / 写综述 / 审稿改稿 / "
            "选题与前沿调研 / 研究方案设计与实验验证 / 需要「一个帮我做完整科研流程」的助手。"
            "典型问法：帮我做一份文献综述、帮我写一篇论文、帮我审稿并给修改意见、"
            "帮我做选题调研、帮我验证一个研究假设、帮我安排一次完整的科研任务、"
            "research assistant / literature review / paper writing / peer review / research orchestration。"
        ),
    ),
    "yanshu.scout-partner": AgentFacade(
        description=(
            "文献检索与资料收集（Scout）：跨 arXiv、OpenAlex、Semantic Scholar 三个学术源检索中英文论文，"
            "返回标题、作者、年份、摘要、DOI 与链接的去重清单，并跟踪高引与新兴工作。"
            "适合：文献检索 / 找论文 / 查资料 / 文献综述的资料收集 / 领域调研 / 前沿追踪 / 高引论文与引文数据获取。"
            "典型问法：帮我检索某个主题的文献、找近五年的相关论文、收集一个方向的代表性工作、"
            "查某篇论文的引用情况、做一次文献调研、literature search、paper search、survey materials。"
        ),
        tags=("文献检索", "论文检索", "学术搜索", "literature-search", "scout"),
    ),
    "yanshu.writer-partner": AgentFacade(
        description=(
            "学术论文写作（Writer）：按研究主题与编号资料清单起草中文论文，支持大纲与全文两档，"
            "并可依据审稿意见定向返工修改。"
            "适合：论文写作 / 写综述 / 写开题报告与大纲 / 起草初稿 / 按审稿意见改稿 / 学术润色与结构重排。"
            "典型问法：帮我写一篇论文、帮我写文献综述、给我一个论文大纲、"
            "根据审稿意见改稿、把资料整理成一篇综述、academic writing、paper drafting、revise the manuscript。"
        ),
        tags=("论文写作", "综述写作", "初稿", "academic-writing", "writer"),
    ),
    "yanshu.synthesis-partner": AgentFacade(
        description=(
            "论文精读与结构化萃取（Synthesis）：把论文拆成创新点、研究缺口、方法论、实验配置四要素，"
            "并给出关键词，供后续综述、矛盾侦测与引用核验使用。"
            "适合：论文精读 / 文献结构化整理 / 批量抽取论文要点 / 综述素材整理 / 读文献并做笔记。"
            "典型问法：帮我读这几篇论文并提炼要点、把文献整理成结构化表格、"
            "提取论文的创新点和研究方法、帮我做文献笔记、paper reading、literature extraction。"
        ),
        tags=("文献精读", "信息抽取", "paper-reading", "extraction", "synthesis"),
    ),
    "yanshu.critic-partner": AgentFacade(
        description=(
            "论文评审与引用核验（Critic）：以审稿人视角做科学性、逻辑性与合规性审查，"
            "输出 APPROVE / REVISE 裁决、评分与逐条问题清单（含位置、问题、建议与可复核原文证据）；"
            "引用核验独立于审稿判断，逐条判定 supported / misrepresented / fabricated / unverifiable。"
            "适合：论文审稿 / 同行评审 / 稿件质量检查 / 引用与事实核验 / 查引用是否造假 / 投稿前自检。"
            "典型问法：帮我审一篇论文、帮我检查这篇稿子的问题、帮我核对引用是否真实、"
            "这篇论文能不能投、给我一份审稿意见、peer review、citation verification、fact checking。"
        ),
        tags=("论文评审", "审稿", "引用核验", "peer-review", "citation-verification"),
    ),
    "yanshu.frontier-partner": AgentFacade(
        description=(
            "研究前沿与选题探测（Frontier）：按关键词扫描近 6-12 个月发表的工作，"
            "用引用速度（总引用/月龄）和主题聚集度识别正在爆发的新兴方向，输出方向清单与入场时机建议。"
            "适合：选题 / 找研究热点 / 前沿趋势分析 / 判断方向是否值得做 / 新兴领域扫描 / 论文选题调研。"
            "典型问法：帮我找几个值得做的研究方向、这个领域最近有什么热点、"
            "帮我做选题调研、哪个方向正在爆发、research trend analysis、topic selection、emerging topics。"
        ),
        tags=("选题", "研究热点", "前沿趋势", "topic-selection", "trend-analysis"),
    ),
    "yanshu.contradiction-partner": AgentFacade(
        description=(
            "文献矛盾侦测与假说生成（Contradiction）：跨论文比对四要素萃取，"
            "找出实质冲突并转化为可检验的研究假说，输出结构化矛盾对（双方论点、证据、来源、矛盾类型）。"
            "适合：找文献之间的矛盾 / 发现研究冲突与争议点 / 生成可验证的研究假设 / 综述的批判性分析。"
            "典型问法：这些论文里有没有互相矛盾的结论、帮我找研究空白和争议、"
            "根据文献冲突提出假设、帮我做批判性文献分析、contradiction detection、hypothesis generation。"
        ),
        tags=("矛盾侦测", "研究假设", "contradiction", "hypothesis", "critical-analysis"),
    ),
    "yanshu.experiment-partner": AgentFacade(
        description=(
            "实验设计与执行（Experiment）：把研究问题转写为可执行实验代码，在受控 GPU 沙盒中真实运行，"
            "返回结构化实验报告（结论摘要、指标、发现、局限与该假设是否被支持）。"
            "适合：跑实验 / 验证假设 / 复现论文结果 / 机器学习实验执行 / 需要真实运行结果而不是文字推测。"
            "典型问法：帮我做个实验验证这个想法、帮我跑一下这个模型、"
            "复现这篇论文的实验、验证这个假设是否成立、run experiments、hypothesis validation。"
        ),
        tags=("实验执行", "假设验证", "experiment", "gpu-sandbox", "reproducibility"),
    ),
    "yanshu.gpu-partner": AgentFacade(
        description=(
            "研枢集群的 GPU 实验执行节点：在断网加固的 Docker 沙盒（PyTorch CUDA 镜像，"
            "不透传外网，GPU device=0，内存 7G/CPU 4 核）中运行机器学习实验；宿主侧代下数据集"
            "（https 直链，sha256 校验，≤2GB，zip/tar 安全解压）并以只读卷挂进沙盒，"
            "实验产物随结果回传；另有已托管数据集目录、任务状态与环境查询三个只读通道。"
            "适合：论文最小复现 / 研究假设验证 / 小模型训练与评测 / 数据集代下与预处理 /"
            " 跑一段 PyTorch 代码拿真实指标。"
            "典型问法：帮我跑一个实验验证这个想法、用 GPU 跑段训练代码、复现论文里的实验设置、"
            "把这个数据集下载到沙盒、看看最近的实验状态、run an experiment、"
            "train a small model、reproduce a paper experiment、fetch a dataset。"
        ),
        tags=("GPU算力", "沙盒执行", "实验复现", "gpu-compute", "sandbox", "resource"),
    ),
    "yanshu.cpu-partner": AgentFacade(
        description=(
            "研枢集群的 CPU 任务执行节点：以断网加固沙盒形式提供纯 CPU 任务执行"
            "（不透传 GPU，内存 7G/CPU 4 核），产物随结果回传；配套任务状态与环境查询通道。"
            "适合：CPU 批量计算 / 数据清洗与转换 / 脚本执行 / 纯计算验证 /"
            " 不需要 GPU 的原型验证。"
            "典型问法：帮我跑一个 CPU 计算任务、处理一批数据、清洗这份 CSV、"
            "在沙盒里执行这段代码、cpu compute、batch job、data processing。"
        ),
        tags=("CPU算力", "批处理", "数据处理", "cpu-compute", "sandbox", "resource"),
    ),
    "yanshu.model-partner": AgentFacade(
        description=(
            "算法模块推理服务（Model）：以进程内推理方式对外提供模型预测能力，权重不出域，只暴露输入输出规格与 predict。"
            "适合：调用已训练模型做批量预测 / 需要模型但不交出权重 / 分类与打分服务。"
            "典型问法：帮我用这个模型做预测、批量打分、model inference、prediction service。"
        ),
        tags=("模型推理", "批量预测", "model-inference", "prediction", "resource"),
    ),
    "yanshu.eval-partner": AgentFacade(
        description=(
            "模型评测服务（Eval）：数据集不出的基准评测——提交预测或提交代码，只回分数与榜单名次，"
            "标签不下发、产物不回传、沙盒断网，防泄题。"
            "适合：给模型或算法打分 / 参加基准评测 / 对比方案效果 / 需要公平第三方评测。"
            "典型问法：帮我评测这个模型的效果、给我在基准上打个分、"
            "提交结果到榜单、benchmark evaluation、leaderboard submission。"
        ),
        tags=("模型评测", "基准测试", "benchmark", "evaluation", "leaderboard"),
    ),
    "yanshu.kb-partner": AgentFacade(
        # ⚠ 2026-10-11 实测：原描述让"查文献"这类问法**匹配不到 KB**
        # （被 scout 等文献检索 agent 抢走），因为原描述把 KB 定位成"库"而非"文献检索"。
        # 这里把用户真实会用的问法并进**同一段 description**。
        # 注意：不能用 search_surface —— 那个分支会丢弃本段、改用 partner_kb.py 里的短默认句
        # （实测 287 字全是短句+检索面，原 209 字描述被丢掉，反而更差）。
        # 改这段会改变 ACS 投影 → 需要重新注册（见 docs/kb-discovery-widening.md）。
        description=(
            "知识库与文献库查询（KB）：数据不动、计算动——主题式全文检索（trigram 分词，中英文皆可，"
            "按相关度排序、返回有界摘录）+ 受限查询下推 + 字段级脱敏"
            "（open 目录 / members 摘要 / curator 全文），支持条件过滤、翻页、聚合统计与受控写入。"
            "适合：查机构文献库 / 按主题找资料 / 结构化知识检索 / 统计与聚合查询 / 需要按权限分级看数据。"
            "典型问法：帮我查库里的文献和统计数字、在库里找讲某个主题的论文、按条件检索知识库、"
            "做一次聚合统计、knowledge base query、restricted data access。"
            "也用于：查文献、找文献、查论文、找论文、查资料、找资料、查数据、找数据、"
            "查一下库里有什么、文献库里有啥、检索资料、全文检索、主题检索、找相关文献、"
            "按条件查、字段查询、条件筛选、"
            "批量查、数据统计、统计一下、分组统计、求和、计数、分布统计、排名、"
            "对比不同机构、按角色看数据、权限分级、脱敏、哪些字段被隐藏、"
            "受控写入、把资料入库、录入数据、登记数据。"
        ),
        tags=("知识库", "文献库", "查文献", "找论文", "查资料", "查数据",
              "受限查询", "聚合统计", "knowledge-base", "data-query"),
    ),
    "yanshu.hpc-partner": AgentFacade(
        description=(
            "高性能计算网关（HPC）：以异步作业方式接入机构调度器（提交 → 排队 → 执行 → 取结果），"
            "存量 HPC 不改造即可互联。"
            "适合：提交长时间计算作业 / 用集群跑任务 / 排队式批处理 / 对接已有 Slurm。"
            "典型问法：帮我把作业提交到集群、查一下队列和作业状态、"
            "跑一个长任务、hpc job submission、slurm job、batch scheduling。"
        ),
        tags=("高性能计算", "作业调度", "hpc", "slurm", "batch"),
    ),
    "yanshu.citation-integrity": AgentFacade(
        description=(
            "引用完整性核验（Citation Integrity）：把草稿里的每条引用变成四档裁决与证据摘录"
            "（supported / misrepresented / fabricated / unverifiable），并说明方法论与来源分级。"
            "适合：核查参考文献真实性 / 查引用是否被曲解 / 投稿前引用自检 / 综述引用合规检查。"
            "典型问法：帮我核对这篇稿子的引用、这些参考文献是真的吗、"
            "引用有没有被曲解、citation integrity check、reference validation。"
        ),
        tags=("引用核验", "参考文献", "citation-integrity", "reference-check"),
    ),
}


# ── 能力级门面（写进 ACS 的 skills[]：description / tags / examples）──
CAPABILITY_FACADES: dict[str, CapabilityFacade] = {

        'gpu.env': CapabilityFacade(
            description=('查询 GPU 计算节点环境：显卡型号/显存/驱动、可用镜像白名单、沙盒资源限制。公开只读。'),
            tags=('gpu', 'compute', 'status', '环境查询', '显卡', 'GPU 环境'),
            examples=(
            '看看 GPU 节点有什么卡',
            'GPU 沙盒的资源限制是多少',
            '查一下显卡型号和驱动版本'
            ),
        ),
        'gpu.status': CapabilityFacade(
            description=('查询最近的 GPU 实验任务（状态/时长/退出码/产物清单，不含任务内容）。公开只读。'),
            tags=('gpu', 'compute', 'status', '任务状态'),
            examples=(
            '最近的 GPU 实验跑得怎么样',
            '查一下上一个实验任务的退出码'
            ),
        ),
        'gpu.data.list': CapabilityFacade(
            description=('列出本 GPU 节点已托管的数据集（名称/文件数/大小/容器内挂载路径）。公开只读。'),
            tags=('gpu', 'dataset', '数据集', '数据列表'),
            examples=(
            'GPU 节点上有哪些现成数据集',
            '看看已托管数据集的大小'
            ),
        ),
        'gpu.data.fetch': CapabilityFacade(
            description=('把一个数据集从 URL 代下到本节点（宿主侧下载，沙盒外执行），之后实验代码可用 /data/<name> 只读访问。仅 https；可选 sha256 强校验；上限 2GB；zip/tar 自动安全解压。数据集托管属高成本操作，凭委派令牌调用。'),
            tags=('gpu', 'dataset', '下载', '数据集代下', 'fetch'),
            examples=(
            '帮我把这个 URL 的数据集下载到沙盒',
            '从 OpenML 拉一个数据集进 GPU 节点'
            ),
        ),
        'gpu.run': CapabilityFacade(
            description=('在 GPU 沙盒容器（pytorch:cu128，断网，内存 7G/CPU 4 核/GPU device=0）中运行实验：files 提供 {文件名: 内容}（至少含 main.py），产物写入 output 目录随结果回传。高成本资源，凭委派令牌调用。'),
            tags=('gpu', 'experiment', '沙盒', '训练', '复现'),
            examples=(
            '在沙盒里跑一个最小实验验证收敛',
            '用 GPU 沙盒复现论文里的训练脚本',
            '跑一段 PyTorch 训练代码给我指标'
            ),
        ),
        'cpu.env': CapabilityFacade(
            description=('查询 CPU 计算节点环境：核数/内存/Python 版本、镜像白名单、沙盒资源限制。公开只读。'),
            tags=('cpu', 'compute', 'status', '环境查询'),
            examples=(
            'CPU 节点几核几 G',
            '沙盒里的 Python 是什么版本'
            ),
        ),
        'cpu.status': CapabilityFacade(
            description=('查询最近的 CPU 节点任务（状态/时长/退出码/产物清单，不含任务内容）。公开只读。'),
            tags=('cpu', 'compute', 'status', '任务状态'),
            examples=(
            '最',
            '近',
            '的',
            ' ',
            'C',
            'P',
            'U',
            ' ',
            '任',
            '务',
            '跑',
            '得',
            '怎',
            '么',
            '样'
            ),
        ),
        'cpu.run': CapabilityFacade(
            description=('在 CPU 沙盒容器（pytorch:cu128 镜像但不透传 GPU，断网，内存 7G/CPU 4 核）中运行任务：files 提供 {文件名: 内容}（至少含 main.py），产物写入 output 目录随结果回传。适合纯计算/数据处理/脚本类任务；高成本资源，凭委派令牌调用。'),
            tags=('cpu', 'compute', 'task', '脚本', '数据处理'),
            examples=(
            '在沙盒里跑这个数据清洗脚本',
            '验证一段纯计算代码的输出'
            ),
        ),
        "ws.create": CapabilityFacade(
            description=("按 task_id 开辟服务器端任务工作区目录，返回绝对路径与配额（默认 512MB）。已存在则直接复用并报当前用量。任务级隔离，互不干扰。"),
            tags=("workspace", "storage", "task"),
            examples=(
            "为任务 task-20261101 开一个工作区",
            "给这轮协作建个工作目录"
            ),
        ),
        "ws.write": CapabilityFacade(
            description=("向任务工作区写文件：content 为文本，或 encoding=base64 的二进制；原子写，写前复核每任务配额（默认 512MB），超额拒绝并如实报已用量。"),
            tags=("workspace", "storage", "file"),
            examples=(
            "把这份实验报告存进工作区",
            "将数据集清单写到 task-20261101 的 data/ 目录"
            ),
        ),
        "ws.read": CapabilityFacade(
            description=("读取任务工作区内文件，默认 ≤64KB，超限截断并如实标注 truncated；二进制自动转 base64 并标注编码。只读，不执行内容。"),
            tags=("workspace", "storage", "file"),
            examples=(
            "读一下工作区里 README 的内容",
            "把 task-20261101 的实验结果发我"
            ),
        ),
        "ws.list": CapabilityFacade(
            description=("输出任务工作区的目录树（posix 相对路径、类型、字节，≤200 项，可限深度），附工作区总用量。用于让伙伴在读写前先看清有什么。"),
            tags=("workspace", "storage", "list"),
            examples=(
            "这个任务的工作区里有什么文件",
            "列一下 data 目录下的大小"
            ),
        ),
        "ws.clone_repo": CapabilityFacade(
            description=("浅克隆公开 https 仓库（--depth 1，限 120 秒）到任务工作区 repo/<name>/，返回文件树与 README 头部摘要；克隆后复核配额，失败自动清理。不支持私有仓库与凭据。"),
            tags=("git", "github", "repo", "clone"),
            examples=(
            "把 github.com/MKC-ShenZhi/SimpleAutoResearch 克隆进这个任务的工作区",
            "帮我拉取这个开源仓库的代码用于复现实验"
            ),
        ),
        "ws.gc": CapabilityFacade(
            description=("按 task_id 精确删除，或 older_than_days 按 mtime 批量回收过期工作区，返回已删除清单。防止服务器被任务残留填满。"),
            tags=("workspace", "gc", "cleanup"),
            examples=(
            "把 task-20261001 的工作区回收掉",
            "清理七天前所有过期工作区"
            ),
        ),
    # scout
    "arxiv.search": CapabilityFacade(
        description=(
            "arXiv 预印本检索：按关键词或主题检索 arXiv，返回标题、作者、发布时间、摘要与 PDF 链接，"
            "适合找最新预印本与前沿工作。输入自然语言关键词即可。"
        ),
        tags=("文献检索", "论文检索", "arXiv", "预印本", "学术搜索", "literature-search", "paper-search"),
        examples=(
            "帮我检索近三年图神经网络在知识图谱补全方向的论文",
            "找一下 arXiv 上关于多智能体协作的最新预印本",
            "给我十篇关于检索增强生成的论文",
            "search arXiv for recent papers on agent interoperability",
        ),
    ),
    "openalex.search": CapabilityFacade(
        description=(
            "OpenAlex 全库检索：覆盖期刊、会议与预印本，返回标题、作者、年份、摘要、DOI、引用数与来源，"
            "适合做覆盖面广的文献调研与引文统计。"
        ),
        tags=("文献检索", "学术搜索", "引用数", "OpenAlex", "全库检索", "citation-count", "literature-survey"),
        examples=(
            "帮我检索这个主题被引最高的十篇论文",
            "查一下这个方向近五年的高引工作",
            "找出某篇论文的引用情况",
            "find the most cited papers on this topic",
        ),
    ),
    "semanticscholar.search": CapabilityFacade(
        description=(
            "Semantic Scholar 引用图谱检索：返回标题、作者、年份、摘要、引用数与链接，"
            "适合沿着引用关系追踪一条研究脉络。"
        ),
        tags=("文献检索", "引用图谱", "Semantic Scholar", "引文追踪", "citation-graph"),
        examples=(
            "沿着引用关系帮我追踪这条研究脉络",
            "帮我查这篇论文被谁引用了",
            "找这个工作的后续研究",
            "trace the citation graph of this paper",
        ),
    ),
    # writer
    "paper.write": CapabilityFacade(
        description=(
            "论文写作：mode=outline 输出带章节规划与论点标注的大纲，mode=full 输出含摘要、引言、相关工作、"
            "方法、实验设计、结论与参考文献的全文初稿；传 feedback 与上一轮稿可按审稿意见定向返工。"
        ),
        tags=("论文写作", "综述写作", "大纲", "初稿", "改稿", "academic-writing", "paper-drafting", "revision"),
        examples=(
            "帮我写一篇关于这个主题的文献综述",
            "先给我一份论文大纲，再展开成全文",
            "按审稿意见修改这份初稿",
            "draft a survey paper on this topic",
        ),
    ),
    # synthesis
    "paper.extract": CapabilityFacade(
        description=(
            "论文结构化萃取：输入论文文本（标题+摘要或正文片段），输出核心创新点、研究缺口、方法论与实验配置四要素，"
            "并给出关键词；文中未提及的字段如实标注，不编造。"
        ),
        tags=("文献精读", "信息抽取", "论文要点", "结构化", "paper-reading", "extraction", "summarization"),
        examples=(
            "帮我读这几篇论文并提炼创新点和研究方法",
            "把文献整理成结构化的四要素表格",
            "提取这篇论文的实验配置",
            "extract the key contributions and methods from these papers",
        ),
    ),
    # critic
    "paper.review": CapabilityFacade(
        description=(
            "论文评审：以审稿人视角做科学性、逻辑性与合规性审查，输出 APPROVE/REVISE 裁决、0-100 分"
            "与逐条问题清单（位置、问题、建议、可复核的原文证据）。存在 blocker/major 时裁决强制为 REVISE。"
        ),
        tags=("论文评审", "审稿", "同行评审", "质量检查", "peer-review", "manuscript-review", "quality-check"),
        examples=(
            "帮我审这篇论文，给出审稿意见",
            "这篇稿子有哪些问题，能不能投",
            "帮我做一次投稿前的质量自检",
            "review this manuscript and list the issues",
        ),
    ),
    "citation.verify": CapabilityFacade(
        description=(
            "引用核验：解析草稿中的编号引用，逐条取源比对论断与原文，输出四档裁决"
            "（supported / misrepresented / fabricated / unverifiable）与证据摘录，供审稿交叉对账。"
        ),
        tags=("引用核验", "事实核验", "参考文献", "citation-verification", "fact-check", "reference-check"),
        examples=(
            "帮我核对这篇稿子的引用是否真实",
            "查一下这些参考文献有没有被曲解",
            "核对引用与原文是否一致",
            "verify the citations in this draft",
        ),
    ),
    # frontier
    "frontier.detect": CapabilityFacade(
        description=(
            "研究前沿探测：按关键词扫描近 window_months 个月发表的工作，用引用速度（总引用/月龄）"
            "与主题聚集度识别正在爆发的新兴方向，输出方向清单与入场时机建议。"
        ),
        tags=("选题", "研究热点", "前沿趋势", "新兴方向", "topic-selection", "trend-analysis", "emerging-topics"),
        examples=(
            "帮我找几个值得做的新兴研究方向",
            "这个领域最近有什么热点",
            "哪个方向正在爆发，值不值得入场",
            "find emerging research directions in this area",
        ),
    ),
    "frontier.citation_profile": CapabilityFacade(
        description=(
            "引用画像：给定主题或工作，统计其引用速度、引用分布与主题聚集情况，"
            "用于判断一条研究线是升温还是降温。"
        ),
        tags=("引用分析", "趋势判断", "citation-profile", "trend"),
        examples=(
            "帮我看看这个方向在升温还是降温",
            "统计这个主题的引用速度",
            "profile the citation trend of this topic",
        ),
    ),
    # contradiction
    "contradiction.detect": CapabilityFacade(
        description=(
            "跨论文矛盾侦测与假说生成：输入多篇论文的四要素萃取，输出结构化矛盾对"
            "（双方论点、证据、来源、矛盾类型）与可检验的调和假说；引用不存在或同一来源的候选会被代码丢弃。"
        ),
        tags=("矛盾侦测", "研究冲突", "研究假设", "研究空白", "contradiction", "hypothesis", "research-gap"),
        examples=(
            "这些论文里有没有互相矛盾的结论",
            "帮我找这个方向的研究空白和争议点",
            "根据文献冲突帮我提出可验证的假设",
            "find contradictions across these papers",
        ),
    ),
    # experiment
    "exp.run": CapabilityFacade(
        description=(
            "实验执行：把研究问题转写为可执行实验代码，在受控 GPU 沙盒中真实运行，"
            "返回结构化实验报告——结论摘要、指标、发现、局限，以及该假设是否被支持。"
        ),
        tags=("实验执行", "假设验证", "复现实验", "机器学习实验", "experiment", "hypothesis-validation", "reproducibility"),
        examples=(
            "帮我设计并跑一个实验验证这个假设",
            "复现这篇论文的实验结果",
            "帮我训练并评估一个模型",
            "run an experiment to test this hypothesis",
        ),
    ),
    # scout 的非公开能力（不进公开橱窗，但属于域可见声明；写门面便于将来放宽可见性时直接生效）
    "arxiv.fetch": CapabilityFacade(
        description=(
            "按 arXiv ID 取回单篇论文的完整记录（标题、作者、摘要、PDF 链接），"
            "是引用核验的取源通道：拿一条引用去核对原文到底怎么写。"
        ),
        tags=("文献取源", "引用核验", "arXiv", "全文比对", "citation-source", "paper-fetch"),
        examples=(
            "帮我取这篇论文的原文记录",
            "核对这条引用对应的论文内容",
            "fetch the paper by arXiv id",
        ),
    ),
    "dataset.fetch": CapabilityFacade(
        description=(
            "数据集获取：按名称或 URL 取回实验用数据集并登记为可挂载数据，"
            "供算力沙盒内的任务直接读取，避免把数据正文贴进对话。"
        ),
        tags=("数据集", "数据获取", "实验数据", "dataset", "data-fetch"),
        examples=(
            "帮我准备这个实验需要的数据集",
            "把这份数据挂给沙盒任务用",
            "fetch and register this dataset for the experiment",
        ),
    ),
    "scholar.deep_fetch": CapabilityFacade(
        description=(
            "批量文献深取：一次取回多篇论文的完整摘要与元数据（单次上限 25 条），"
            "供综述与引用核验使用；属高成本能力，需被授权后调用。"
        ),
        tags=("批量取源", "文献深取", "综述素材", "bulk-fetch", "deep-fetch"),
        examples=(
            "帮我把这十篇论文的完整摘要一次性取回来",
            "批量取源用于综述写作",
            "bulk fetch the full records of these papers",
        ),
    ),
    "memory.search": CapabilityFacade(
        description=(
            "论文记忆检索：在 scout **已存下的论文库**里按主题/关键词直接查（不联网、毫秒级），"
            "返回标题、作者、年份、摘要、链接、引用数与见过的来源。"
            "用于「这个主题之前是不是查过」，避免重复联网检索。"
        ),
        tags=("记忆库", "论文缓存", "避免重复检索", "paper-memory", "cache", "recall"),
        examples=(
            "这个主题之前是不是已经查过了",
            "从已存的论文库里找一下扩散模型的综述",
            "check the paper memory for diffusion model surveys",
        ),
    ),
    "memory.stats": CapabilityFacade(
        description=(
            "论文记忆库统计：已存论文数、时效内与过期条数、来源分布、TTL 与库文件路径，"
            "以及最近若干次检索的出处（走记忆还是联网）。用于观察记忆层是否在省检索。"
        ),
        tags=("记忆统计", "缓存命中", "memory-stats", "observability"),
        examples=(
            "论文记忆库里现在存了多少篇",
            "看看最近有多少检索是直接走记忆的",
            "show paper memory stats",
        ),
    ),
    # gpu / cpu / eval / model / kb / hpc
    "gpu.run": CapabilityFacade(
        description=(
            "GPU 任务提交：在断网加固的容器沙盒中执行 GPU 任务（资源与超时受代码强制），"
            "返回状态与产物清单，适合训练、推理与批量实验。"
        ),
        tags=("GPU算力", "沙盒执行", "任务提交", "gpu-compute", "sandbox", "training"),
        examples=(
            "帮我在 GPU 上跑一段训练代码",
            "提交一个需要显卡的计算任务",
            "run this training script on a GPU sandbox",
        ),
    ),
    "cpu.run": CapabilityFacade(
        description=(
            "CPU 任务提交：在断网加固沙盒中执行纯 CPU 任务（资源与超时受代码强制），"
            "适合批处理、数据处理与无需显卡的计算。"
        ),
        tags=("CPU算力", "批处理", "任务提交", "cpu-compute", "sandbox", "batch-job"),
        examples=(
            "帮我跑一个 CPU 批量计算任务",
            "在沙盒里执行这段数据处理代码",
            "run this batch job on a CPU sandbox",
        ),
    ),
    "kb.query": CapabilityFacade(
        description=(
            "知识库与文献库查询：查文献、找论文、查资料、查数据；主题式全文检索（trigram 分词，"
            "中英文皆可，按相关度排序、返回有界摘录）+ 受限查询下推 + 字段级脱敏"
            "（open 目录 / members 摘要 / curator 全文），支持条件过滤、字段投影、offset 翻页、"
            "排序、聚合统计（统计、求和、计数、分组、分布、排名）与按编号取单条；可防整库导出。"
        ),
        tags=("知识库", "文献库", "查文献", "找论文", "查资料", "查数据", "检索资料", "全文检索",
              "主题检索", "受限查询", "聚合统计", "统计查询", "分页查询", "knowledge-base",
              "data-query", "literature-search", "paper-lookup", "full-text-search"),
        examples=(
            "在库里找讲稀疏注意力和长上下文的文献",
            "按主题检索知识库里的资料，给我摘要片段",
            "查一下库里有哪些相关论文",
            "统计库里某个字段的分布",
            "帮我统计一下各机构的论文数量并排名",
            "命中太多时按相关度翻下一页",
            "query the knowledge base with filters",
            "look up papers in the literature library",
        ),
    ),
    "hpc.submit": CapabilityFacade(
        description=(
            "HPC 作业提交：把脚本提交到机构调度器（真实 Slurm 或本地队列+沙盒），"
            "异步返回作业 id，可轮询状态与结果。"
        ),
        tags=("高性能计算", "作业调度", "集群", "hpc", "slurm", "batch-scheduling"),
        examples=(
            "帮我把这个作业提交到集群",
            "提交一个长时间运行的计算任务",
            "submit this job to the HPC cluster",
        ),
    ),
    "hpc.result": CapabilityFacade(
        description="HPC 作业结果：取终态作业的 stdout 截选与产物元信息（产物全文留在网关本机，不搬运）。",
        tags=("高性能计算", "作业结果", "hpc", "job-result"),
        examples=(
            "帮我看这个作业跑完的结果",
            "取一下刚才提交作业的输出",
            "fetch the result of that HPC job",
        ),
    ),
    # 平台入口能力：让"一句话问智能体"能问到这个集群的总入口
    "research.orchestrate": CapabilityFacade(
        description=(
            "科研任务总控：把一句话研究需求拆解为可执行计划，调度文献检索、论文精读、矛盾与空白侦测、"
            "前沿选题、论文写作、审稿与引用核验、实验执行等专职智能体协作，并返回带 AIC 溯源的结果。"
            "也提供只读的集群调度计划与伙伴在线状态查询。"
        ),
        tags=("科研协作", "任务编排", "文献综述", "论文写作", "审稿", "研究助手",
              "research-orchestration", "literature-review", "paper-writing", "peer-review"),
        examples=(
            "帮我做一份文献综述",
            "帮我写一篇论文并审稿",
            "帮我做选题调研和前沿分析",
            "帮我完成一次完整的科研任务",
            "help me conduct a literature review and draft a paper",
        ),
    ),
    # citation-integrity
    "citation.rules": CapabilityFacade(
        description=(
            "引用核验方法论：说明四档裁决定义、审稿与核验的分离原则、来源分级与判定口径，"
            "供他人对齐验收标准。"
        ),
        tags=("引用核验", "方法论", "规范", "citation-integrity", "methodology"),
        examples=(
            "引用核验的判定标准是什么",
            "supported 和 misrepresented 怎么区分",
            "what are the citation verdict definitions",
        ),
    ),
    # 公开只读的橱窗能力（信息卡类）
    "gpu.env": CapabilityFacade(
        description="GPU 节点环境卡：显卡型号与显存、驱动版本、可用镜像白名单与沙盒资源限制，接任务前先看它确认能跑什么。",
        tags=("GPU算力", "环境查询", "资源", "gpu-environment"),
        examples=("帮我看看 GPU 节点的环境和限制", "这个算力节点能跑什么镜像"),
    ),
    "cpu.env": CapabilityFacade(
        description="CPU 节点环境卡：核数、内存、Python 版本、可用镜像白名单与沙盒资源限制，接任务前先看它确认能跑什么。",
        tags=("CPU算力", "环境查询", "资源", "cpu-environment"),
        examples=("帮我看看 CPU 节点的环境和限制", "这个节点有多少核和内存"),
    ),
    "gpu.status": CapabilityFacade(
        description="GPU 节点任务状态：最近任务的状态、时长、退出码与产物清单（只有元数据，不含任务内容），用于查看队列与排队情况。",
        tags=("GPU算力", "任务状态", "gpu-status"),
        examples=("查一下 GPU 节点最近的队列和任务状态",),
    ),
    "cpu.status": CapabilityFacade(
        description="CPU 节点任务状态：最近任务的状态、时长、退出码与产物清单（只有元数据，不含任务内容），用于查看队列与排队情况。",
        tags=("CPU算力", "任务状态", "cpu-status"),
        examples=("查一下 CPU 节点最近的队列和任务状态",),
    ),
    "gpu.data.list": CapabilityFacade(
        description="GPU 节点数据集清单：已托管数据集的名称、文件数、大小与容器内挂载路径，用于确认任务能直接用哪些数据。",
        tags=("GPU算力", "数据集", "gpu-dataset"),
        examples=("节点上有哪些可用的数据集",),
    ),
    "gpu.data.fetch": CapabilityFacade(
        description="GPU 节点数据集获取：按名称取回托管数据集的元信息与挂载方式，让沙盒任务能直接读该数据，不必把数据正文上传。",
        tags=("GPU算力", "数据集", "gpu-dataset"),
        examples=("帮我取用节点上的那个数据集",),
    ),
    "eval.benchmark_info": CapabilityFacade(
        description="评测基准信息：可用基准、指标、样本数与提交规则（标签由评测方持有，不随特征下发）。",
        tags=("模型评测", "基准信息", "benchmark"),
        examples=("有哪些评测基准可以用", "这个基准的指标和提交规则是什么"),
    ),
    "eval.leaderboard": CapabilityFacade(
        description="评测榜单：Top-N 提交者、分数与提交时间（不含任何预测内容），用于对比方案效果与查看自己的名次。",
        tags=("模型评测", "榜单", "leaderboard"),
        examples=("看看评测榜单现在谁在前列",),
    ),
    "model.info": CapabilityFacade(
        description="模型卡：架构、版本、许可证、输入输出规格与基准表现（公开橱窗；权重不出域，只暴露推理接口）。",
        tags=("模型推理", "模型卡", "model-card"),
        examples=("这个模型是什么架构、输入输出是什么",),
    ),
    "kb.schema": CapabilityFacade(
        description=("知识库结构卡：字段与类型、档位（open 目录 / members 摘要 / curator 全文）、"
                     "四种查询模式（search/filter/compute/get）规则、按档位分的全文索引档与限额；"
                     "只有结构，没有内容。"),
        tags=("知识库", "结构卡", "检索规则", "kb-schema"),
        examples=("知识库里有哪些字段和查询规则", "全文检索能查哪些字段"),
    ),
    "kb.ingest": CapabilityFacade(
        description="知识库受控写入（策展）：按 schema 校验后入库并附溯源（提交者与时间），每次写入都是审计事件。",
        tags=("知识库", "受控写入", "策展", "kb-ingest"),
        examples=("帮我把这批记录写入知识库",),
    ),
    "hpc.info": CapabilityFacade(
        description="HPC 网关信息卡：后端类型（真实 Slurm / 本地队列+沙盒）、作业限额（墙钟、文件、大小、队列深度）与当前队列。",
        tags=("高性能计算", "网关信息", "hpc-info"),
        examples=("这个 HPC 网关的限额和队列情况如何",),
    ),
    "hpc.jobs": CapabilityFacade(
        description="HPC 作业列表（squeue 等价）：id、名称、状态、墙钟、耗时与退出码（只有元数据）。",
        tags=("高性能计算", "作业列表", "hpc-jobs", "squeue"),
        examples=("看看集群队列里现在有哪些作业",),
    ),
}


# ── 应用与自检 ────────────────────────────────────────────────────────────


def agent_discovery_description(agent_id: str, default: str = "") -> str:
    """取 agent 声明的 description。

    取值优先级：**注册文本覆盖表 > 门面登记表 > 代码原声明（default）**。

    - 覆盖表命中（该 agent 已注册）：**逐字返回注册版描述**——这是保证
      "代码投影 ≡ 平台注册版"、不触发 wit 指纹保护的关键；
      此时**不追加** `search_surface`（注册版就是最终文本，改了反而漂）。
    - 未注册但登记了门面：用门面描述；登记了 `search_surface` 的（如 supervisor）
      保留 default 原文并把检索面追加其后。
    - 都没命中：原样返回 default。
    """
    registered = registered_agent_description(agent_id)
    if registered:
        return registered
    facade = AGENT_FACADES.get(agent_id)
    if facade is None:
        return default
    if facade.search_surface:
        return f"{default}\n{facade.search_surface}" if default else facade.search_surface
    return facade.description


def _capability_of(item: Any) -> Any | None:
    """取出条目里的 Capability 对象。

    wit 的 `@capability(...)` 装饰器**原样返回原函数**，把 `Capability` 挂在
    `func.capability` 上（见 wit `kernel/capabilities.pyi`）。所以伙伴模块里的
    `SCOUT_CAPABILITIES` 是"函数 + .capability"的列表；两种形态都要支持。
    """
    inner = getattr(item, "capability", None)
    return inner if inner is not None else item


# ── 注册文本覆盖表 ────────────────────────────────────────────────────────────
# 背景：wit 有 ACS 指纹保护 —— agent 一旦注册完成，代码投影出来的 ACS 必须与注册版
# **逐字一致**，否则拒绝启动（`acs_fingerprint_changed_after_submission`）。
# 而本模块的门面会改写声明文本 → 在"绑定已注册工作区"的服务器上必然冲突。
#
# 覆盖表（`registered_acs_overlay.json`，由 `deploy/discovery_metadata` 的
# `build_registered_overlay.py` 从平台现网 ACS 生成）解决这件事：
# **已注册的 agent 一律返回注册文本**（门面不再改动它），于是
#     代码投影 ≡ 注册版 → 指纹不漂 → 守护规则永不触发。
# 未注册的 agent（本地开发、新 agent 首次注册前）仍享受门面的"用户问法"增益。
_OVERLAY_PATH = Path(__file__).resolve().parents[1] / "registered_acs_overlay.json"
_OVERLAY_CACHE: dict[str, Any] | None = None


def registered_overlay() -> dict[str, Any]:
    """读注册文本覆盖表；不存在或损坏时返回空表（门面照常生效）。"""
    global _OVERLAY_CACHE
    if _OVERLAY_CACHE is None:
        try:
            data = json.loads(_OVERLAY_PATH.read_text(encoding="utf-8"))
            _OVERLAY_CACHE = {
                "agents": data.get("agents") or {},
                "skills": data.get("skills") or {},
                "skillsByAgent": data.get("skillsByAgent") or {},
            }
        except Exception:  # noqa: BLE001  文件缺失/损坏都不应影响启动
            _OVERLAY_CACHE = {"agents": {}, "skills": {}, "skillsByAgent": {}}
    return _OVERLAY_CACHE


def registered_agent_description(agent_id: str) -> str | None:
    """该 agent 的注册版描述（未注册则 None）。"""
    rec = (registered_overlay()["agents"] or {}).get(agent_id)
    return (rec or {}).get("description") or None


def registered_skill(skill_id: str, agent_id: str = "") -> dict[str, Any] | None:
    """该 skill 的注册版字段（未注册则 None）。

    技能 id 在不同 agent 间可能重名、且注册文本不同（2026-10-16 实况：critic 与
    citation 都声明 `citation.verify`）——给 agent_id 时优先取"按 agent 命名空间"
    的注册文本；未给或未命中时退回扁平表（兼容旧表与无需消歧的调用方）。
    """
    ov = registered_overlay()
    if agent_id:
        rec = ((ov.get("skillsByAgent") or {}).get(agent_id) or {}).get(skill_id)
        if rec:
            return dict(rec)
    rec = (ov["skills"] or {}).get(skill_id)
    return dict(rec) if rec else None


def apply_to_capabilities(capabilities: Iterable[Any], agent_id: str = "") -> list[Any]:
    """把门面合并进 Capability（只动描述类字段）。

    取值优先级：**注册文本覆盖表（按 agent 命名空间 > 扁平表） > 门面登记表 > 代码原声明**。

    - 覆盖表命中（该 skill 已注册）：用注册版的 `description/tags/examples`，
      保证代码投影与平台注册版逐字一致（否则 wit 会拒绝启动）；传 agent_id 时
      先查按 agent 命名空间的注册文本（消除跨 agent 同名技能的重名冲突）；
    - 覆盖表未命中但门面已登记：用门面文本（供尚未注册的 agent / 新 agent 首次注册）；
    - 都没命中：原样返回。

    对被装饰的函数条目：用 `dataclasses.replace` 复制内层 Capability 并挂回**同一个函数对象**
    （函数不可复制——wit 靠它取 handler），因此列表里仍是可调用对象。
    """
    out: list[Any] = []
    for item in capabilities:
        cap = _capability_of(item)
        cid = getattr(cap, "id", "")
        # 代码级内部工具（如 partner 的 ws.* 客户端）不参与注册文本/门面覆盖：
        # 同名 id 会命中 workspace 的注册版，把 internal tool 的 acps_skill 填成
        # dict（truthy）→ 被 wit 投影进 ACS、改变指纹（2026-10-26 实测）。
        # 判据只能看 `metadata.internal_tool`：`acps_skill` 默认就是 False，普通
        # 能力也走这条覆盖通道，按它判会把全部门面增强关掉（10-26 全量测试暴露）。
        cap_meta = getattr(cap, "metadata", None)
        if isinstance(cap_meta, dict) and cap_meta.get("internal_tool"):
            out.append(item)
            continue
        overlay = registered_skill(cid, agent_id)
        if overlay is not None:
            fields: dict[str, Any] = {}
            if overlay.get("description"):
                fields["description"] = overlay["description"]
            if overlay.get("version"):
                # version 必须补上：wit 写 final ACS 时会带上 skill 的 version，
                # 若代码投影里没有它，同一份声明在 enroll 与 serve 两个阶段会算出
                # 不同指纹（实测 b877… vs 76cc…），从而误触发"ACS 已变"的保护。
                fields["version"] = overlay["version"]
            if overlay.get("tags"):
                fields["tags"] = list(overlay["tags"])
            if overlay.get("examples"):
                fields["examples"] = list(overlay["examples"])
            # 技能名与内容类型也要对齐注册版（2026-10-16 实测：citation 的注册名是
            # 中文自定义名、rules 的 inputModes 为 application/json，与代码投影不同，
            # 指纹校验会因此拒绝启动）。name 没有 dataclass 字段——走 acps_skill 字典
            # 覆写通道（与域可见性 visibility 同通道）；inputModes/outputModes 走字段。
            cur_sk = getattr(cap, "acps_skill", None)
            sk_over: dict[str, Any] = dict(cur_sk) if isinstance(cur_sk, dict) else {}
            if overlay.get("name") and overlay["name"] != sk_over.get("name"):
                sk_over["name"] = overlay["name"]
            if sk_over != (cur_sk if isinstance(cur_sk, dict) else None) and sk_over:
                fields["acps_skill"] = sk_over
            if overlay.get("inputModes") and list(overlay["inputModes"]) != list(getattr(cap, "input_modes", None) or ()):
                fields["input_modes"] = list(overlay["inputModes"])
            if overlay.get("outputModes") and list(overlay["outputModes"]) != list(getattr(cap, "output_modes", None) or ()):
                fields["output_modes"] = list(overlay["outputModes"])
            enriched = replace(cap, **fields) if fields else cap
        else:
            facade = CAPABILITY_FACADES.get(cid)
            if facade is None:
                out.append(item)
                continue
            tags = list(facade.tags) or list(getattr(cap, "tags", ()) or ())
            examples = list(facade.examples) or list(getattr(cap, "examples", ()) or ())
            enriched = replace(cap, description=facade.description, tags=tags, examples=examples)

        if enriched is cap:  # pragma: no cover - replace 不会返回同一对象
            out.append(item)
        elif getattr(item, "capability", None) is not None:
            try:
                item.capability = enriched  # type: ignore[attr-defined]
            except AttributeError:  # 函数属性只读的极端情况：退化为原样
                out.append(item)
                continue
            out.append(item)
        else:
            out.append(enriched)
    return out


def unregistered_ids(capabilities: Iterable[Any]) -> list[str]:
    """返回没有门面登记的能力 id（供单测守门，防止新增能力悄悄漏了门面）。"""
    return sorted(
        {
            getattr(_capability_of(item), "id", "")
            for item in capabilities
            if getattr(_capability_of(item), "id", "") not in CAPABILITY_FACADES
        }
    )


def facade_summary() -> str:
    lines = [f"agent 门面 {len(AGENT_FACADES)} 个；能力门面 {len(CAPABILITY_FACADES)} 个", ""]
    for aid, f in sorted(AGENT_FACADES.items()):
        lines.append(f"[agent] {aid}  ({len(f.description)} 字)  tags={list(f.tags)}")
    lines.append("")
    for cid, f in sorted(CAPABILITY_FACADES.items()):
        lines.append(
            f"[skill] {cid}  描述 {len(f.description)} 字  tags {len(f.tags)} 个  examples {len(f.examples)} 条"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--json":
        print(json.dumps(
            {
                "agents": {k: {"description": v.description, "tags": list(v.tags)} for k, v in AGENT_FACADES.items()},
                "capabilities": {
                    k: {"description": v.description, "tags": list(v.tags), "examples": list(v.examples)}
                    for k, v in CAPABILITY_FACADES.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    print(facade_summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
