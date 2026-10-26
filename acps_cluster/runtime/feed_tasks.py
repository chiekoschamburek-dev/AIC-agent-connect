#!/usr/bin/env python3
"""研枢集群任务派发器：给各 agent 派**真实、不重复**的任务，累积平台调用量。

## 先说清楚它在数什么

平台排行榜口径是 `count = accessCount + messageCount`，而 access 只在
**真实经过 AIP 传输层**的交互上产生（见 `docs/incident-2026-10-19-ranking-zero.md`）。
所以本脚本**不走 `runtime.run()`、不本机直调能力**，而是：

    以调用方身份持有 mTLS 证书 → 向目标 agent 的 /rpc 发 AIP `start` 命令
      → 目标 agent 走它自己的准入（PEP）与执行 → 我们轮询 `get` 直到终态

这样每个任务会在**两侧**各留一条 access：我们是 caller、目标 agent 是 callee。

> ⚠️ 前置条件：目标 agent 必须已挂 `AmpAccessLoggingHooks`
> （`deploy/patch_amp_access.py`），否则调用照常成功但**不会计数**。

## 为什么不会重复

三层去重，任一层命中就跳过：

1. **主题池**：内置科研主题 × 能力形态的笛卡尔组合，每次取**没用过的**；
2. **账本**：`<state>/ledger.jsonl` 记 (task_id, agent, skill, topic, 指纹)，
   重启后继续接着往下取；指纹 = sha256(topic + agent + skill)，同一组合永不二次派发；
3. **目标侧残留**：派发前读目标 agent 的入站任务库，若已有同 taskId 则跳过。

## 覆盖哪些 agent

`TASKS` 表按"能力 → 任务形态"登记。**公开能力（PEP L0 免令牌）优先**，
这样不需要签发委托令牌也能跑通；受保护能力（gpu.run / kb.query 等）默认不派，
需要时用 `--include-protected` 打开（会自动走令牌通道）。

## 用法

    # 1) 只看会派什么、派给谁（不联网、不落账）
    python3 feed_tasks.py --dry-run --limit 5

    # 2) 真派 3 个任务（串行，等终态）
    python3 feed_tasks.py --limit 3

    # 3) 常驻：每 10 分钟最多派 2 个，跑 4 小时
    python3 feed_tasks.py --watch --interval 600 --per-round 2 --hours 4

    # 4) 查看累计战果
    python3 feed_tasks.py --stats

## 参数要点

- `--caller <key>`：用哪个工作区的证书当调用方（默认 supervisor）。
  必须与目标 agent 的准入策略相容——公开能力对任何已认证身份放行。
- `--targets scout,writer,critic`：只派给这些。
- `--rate`：两次派发之间的最小间隔（秒），默认 20，避免把对端打爆。
- `--wait`：单任务等待终态的上限（秒），默认 300。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
PROJECT = CLUSTER.parent
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── 任务形态登记 ──────────────────────────────────────────────────────────────
# 每个条目 = 一种"能力形态"。topic 池会被展开成"主题 × 形态"的组合。
# public=True 的能力走 PEP L0（免令牌）；受保护的默认不派。

SPECS: list[dict] = [
    {
        # 派给 supervisor 本身：它会**真实出站委派**给下面几个伙伴。
        # 这是唯一能产生"caller 侧 access 记录"（aic=调用方）的路径——
        # 脚本直连只会产生被调方记录（2026-10-19 榜单排查结论）。
        "agent": "supervisor",
        "skill": "research.orchestrate",
        "public": True,
        "template": (
            "请就「{topic}」组织一次**深度调研**，共 4 步，**严格按顺序执行**：\n"
            "1) **先**委派 designer（队友的研究方案设计节点，名册内成员）调用它的 "
            "research.assess_feasibility，对本主题做一次可行性评估"
            "（很快，20 秒内返回；**必须最先执行，不要跳过**）；\n"
            "2) 委派 scout 检索该主题的代表性工作（8 篇以内即可）；\n"
            "3) 把检索结果交给 synthesis 做精读抽取（方法、数据、结论、局限四要素）；\n"
            "4) 把抽取结果交给 writer，产出一份大纲（mode=outline）。\n"
            "最后用 aip_submit 交付一段 300 字以内的汇总，写清四步各自的完成情况与可行性结论。\n"
            "硬性约束：designer 若不可用、超时或拒绝，**如实说明并继续第 2-4 步**，"
            "不得伪造它的评估结论；不要展开额外步骤，也不要自己替伙伴写内容。"
        ),
    },
    {
        # ── 重型任务：AutoResearch 论文复现（默认关闭，需 --include-heavy）──
        # 为什么默认关：一次复现要真跑 GPU 沙盒 + 多轮迭代，耗时是普通任务的数倍，
        # 且失败率更高（数据/超时/verdict 判定）。混进默认轮转会把队列拖满。
        "agent": "supervisor",
        "skill": "research.orchestrate",
        "public": True,
        "heavy": True,
        # 与"常规协作"同 agent+skill，必须靠 id 区分指纹（见 fingerprint() 说明）
        "id": "autoresearch",
        "template": (
            "论文复现任务：在「{topic}」方向选一篇有公开实验设置的代表性论文，"
            "用自研（auto_research）模式做一次**最小规模复现**，严格按下面 5 步：\n"
            "1) 先调 auto_research 激活自研模式；\n"
            "2) 用 scout 或 frontier 确定 **1 个**可证伪假设（不要贪多，不要并列多个）；\n"
            "3) 委派 experiment（exp.run）在 GPU 沙盒跑一次小规模实验"
            "（数据可自造，几十行即可；timeout_seconds 控制在 300 以内）；\n"
            "4) 拿到指标后用 auto_research(action='iterate') 登记 verdict"
            "（supported / refuted / inconclusive **必须如实**，拿不到指标就记 inconclusive，"
            "绝不允许编造分数）；\n"
            "5) 用 auto_research(action='summarize') 收尾，并 aip_submit 交付 400 字以内小结，"
            "写清：复现了什么、指标是多少、与原报告哪里不一致。\n"
            "硬性约束：**只跑 1 轮迭代**；任何一步失败就如实说明卡在哪，不要重试超过 1 次。"
        ),
    },
    {
        # ── 跨节点：队友的 research designer ──
        # 为什么由派发器**直接**派，而不是让 supervisor 计划里加一步：
        # supervisor 经常把长任务改道成后台作业，收尾时第 3 步还没执行就退了；
        # 实测 30+ 分钟一次都没调到 designer。直接派是确定性的。
        "agent": "designer",
        "skill": "research.assess_feasibility",
        "public": True,
        "id": "designer-check",
        "template": (
            "请对下面这个研究设想做一次简要可行性评估，3-5 条要点即可：\n"
            "主题：{topic}\n"
            "设想：由多智能体协作完成该主题的系统性综述，并对方法学分歧做交叉验证。\n"
            "请给出：研究类型判断、主要可行性风险、以及一条最关键的改进建议。\n"
            "只做评估，不需要展开完整实验设计。"
        ),
    },
    {
        "agent": "scout",
        "skill": "arxiv.search",
        "public": True,
        "template": (
            "检索主题：{topic}。\n"
            "请用 arxiv_search 从 2-3 个不同关键词角度检索，返回最相关的 8-10 篇论文，"
            "每篇给出 arXiv ID、标题、作者、年份与 2-3 句与本主题相关性的要点。不要编造论文。"
        ),
    },
    {
        "agent": "scout",
        "skill": "semanticscholar.search",
        "public": True,
        "template": (
            "换用 Semantic Scholar 视角复核主题：{topic}。\n"
            "请检索并列出 5-8 篇高被引或近两年发表的工作，标注 venue 与年份，"
            "并用一两句话说明它们各自解决了什么问题。"
        ),
    },
    {
        "agent": "scout",
        "skill": "openalex.search",
        "public": True,
        "template": (
            "用 OpenAlex 做一次引文面核查，主题：{topic}。\n"
            "返回该主题下引用量最高的 5 篇与最新 3 篇，标注年份、被引次数与开放获取状态。"
        ),
    },
    {
        "agent": "synthesis",
        "skill": "paper.extract",
        "public": True,
        "template": (
            "精读并抽取四要素（创新点 / 研究缺口 / 方法论 / 实验配置）。\n"
            "待读论文主题：{topic}。若没有指定单篇论文，请就该主题下最典型的一类方法做抽取，"
            "并明确标注你引用的是哪一篇（给 arXiv ID 或标题）。"
        ),
    },
    {
        "agent": "writer",
        "skill": "paper.write",
        "public": True,
        "template": (
            "写作任务：围绕「{topic}」起草一份论文大纲（mode=outline）。\n"
            "要求：题目、摘要要点、各章节标题与每节 2-3 句规划；"
            "每个论点标注需要哪类证据支撑；资料不足之处明确标 [需补充证据]，不要编造文献。"
        ),
    },
    {
        "agent": "critic",
        "skill": "paper.review",
        "public": True,
        "template": (
            "审稿任务：对下面这段关于「{topic}」的研究设想做严格评审。\n"
            "研究设想：拟用多智能体协作完成该主题的系统性文献综述，"
            "并对其中的方法学分歧做交叉验证。\n"
            "请按 VERDICT: APPROVE / REVISE 两档给出裁决，指出阻塞问题与改进建议；"
            "重点检查：论证是否有据、是否存在以偏概全、证据链是否闭合。"
        ),
    },
    {
        "agent": "contradiction",
        "skill": "contradiction.detect",
        "public": True,
        "template": (
            "矛盾侦测：主题「{topic}」下的文献常出现结论冲突。\n"
            "请指出该类主题中最典型的 2-3 组相互矛盾的主张（各自给出可能来源），"
            "并为每组提出一个可检验的调和假说（说明需要什么实验/数据来判定）。"
        ),
    },
    {
        "agent": "frontier",
        "skill": "frontier.detect",
        "public": True,
        "template": (
            "速度前沿探测：请识别「{topic}」附近近 6-12 个月引用增速最快的新兴方向。\n"
            "给出方向名称、代表工作、增速判断依据，并说明它为什么值得作为选题。"
        ),
    },
    {
        "agent": "experiment",
        "skill": "exp.run",
        "public": False,          # 受保护：需要委托令牌
        "template": (
            "实验设计：围绕「{topic}」提出一个可在小规模数据上完成验证的最小实验。\n"
            "给出：可证伪假设、数据规模与构造方式、评价指标、预期结果与失败判据。"
            "只做设计，不必真的执行训练。"
        ),
    },
]

# 主题池：覆盖不同子领域，保证"不同 agent 拿到不同主题"时也不重样
TOPICS: list[str] = [
    "图神经网络在知识图谱补全中的长尾泛化",
    "检索增强生成中的幻觉抑制与事实性校验",
    "多智能体协作中的通信开销与一致性问题",
    "大模型长上下文的稀疏注意力与记忆压缩",
    "扩散模型在科学数据生成中的可控性",
    "联邦学习中的非独立同分布与隐私预算权衡",
    "代码大模型的执行反馈与自我修复能力",
    "时序基础模型的分布偏移鲁棒性",
    "蛋白质结构预测中的等变表示学习",
    "思维链推理的长度与正确率之间的权衡",
    "多模态对齐中的跨模态语义鸿沟",
    "强化学习从人类反馈中的奖励过优化",
    "嵌入检索的增量索引与遗忘机制",
    "表格数据的自监督预训练目标设计",
    "智能体工具调用的规划分解与错误恢复",
    "语音大模型的低资源语言迁移",
    "图对比学习中的负样本构造策略",
    "科学文献的自动综述与引用可信度评估",
    "对抗样本迁移性在跨模态场景下的变化",
    "模型编辑中的知识定位与副作用控制",
    "小样本分类中的提示与微调协同",
    "推荐系统中的因果去偏与长尾覆盖",
    "自动驾驶感知中的长尾场景合成",
    "医学影像分割的半监督标签效率",
    # ── 为"单轮 30 个"扩充的池子（2026-10-19）──
    # 默认只派 supervisor，可用组合数 = 主题数；若主题数 < 30，
    # 一轮根本凑不满（实测 24 个池子只能出 21 个）。这里扩到 40 留余量。
    "大模型推理的测试时计算分配",
    "检索器的可微训练与端到端联合优化",
    "多模态大模型的视觉幻觉评测",
    "图神经网络的过平滑与深度退化",
    "长文档问答中的证据定位与引用",
    "模型蒸馏中的能力遗忘与容量瓶颈",
    "数据选择与主动学习在低标注场景的收益",
    "生成模型的版权溯源与水印鲁棒性",
    "科学假设生成的自动化评估方法",
    "跨语言迁移中的词表与分词偏差",
    "时序异常检测中的标签稀缺问题",
    "端侧推理的量化误差与精度补偿",
    "多智能体辩论中的观点收敛机制",
    "知识编辑的批量一致性与连锁影响",
    "长尾类别的重采样与损失重加权",
    "大模型自我评测的偏差与校准",
    # ── 第二轮扩充（2026-10-19）：账本已用掉 50+ 个组合，40 个主题不够两轮 ──
    "多模态检索中的跨模态对齐与召回",
    "代码生成中的单元测试自动补全",
    "图神经网络的分布式训练与通信压缩",
    "大模型推理的投机解码与接受率",
    "对话系统中的长期记忆检索与遗忘",
    "科学表格抽取中的模式推断",
    "强化学习探索中的内在奖励设计",
    "模型水印在微调后的存留性",
    "检索增强生成的多跳推理链构建",
    "跨模态生成中的文本-图像一致性度量",
    "时序预测中的多尺度分解与残差建模",
    "少样本文本分类的原型网络改进",
    "知识图谱嵌入的时序演化建模",
    "大模型安全对齐中的拒答边界",
    "语音识别的领域自适应与口音鲁棒性",
    "推荐系统的序列建模与冷启动",
    "图神经网络的异质图消息传递",
    "文档级关系抽取的跨句推理",
    "模型压缩中的结构化剪枝准则",
    "多智能体强化学习的信用分配",
]

DEFAULT_STATE = CLUSTER / ".feeder"

# 默认派发目标：**只有 supervisor 能产生被平台计入的 caller 侧 access 记录**
# （2026-10-19 实测：直连伙伴只留被调方记录，榜单恒 0；派给 supervisor 后
#   榜单立刻出现 supervisor 9 次 / scout 9 次）。详见 docs/task-feeder.md。
DEFAULT_TARGETS = ("supervisor",)

# --ca 指定的信任锚（全局，由 main 设置）。None = 自动选择。
trust_override: str | None = None
# 调用方名册 key（全局，由 main 设置）：用于优先选调用方自己的 trust-bundle。
caller_key_global: str | None = None


# ── 身份与端点 ────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def workspace_of(key: str) -> Path:
    return CLUSTER / f"ws-{key}"


# 平台注册身份覆盖表：{key: {"aic": ..., "endpoint": ...}}
# **为什么需要**：本地 ws-*/acps/ 里的身份是 dev CA 铸造的（aic 形如
# `1.2.156.3088.1.1.XXXX`、端点 localhost），与线上正在服务的平台身份
# （`1.2.156.3088.1.BUPT.VTF97R.*`、端点 192.0.2.10）**不是同一个**。
# 在服务器上两者一致（就是那份 ACS），所以覆盖表是可选的；
# 从别处（本机/另一台机器）派发时才必须靠它指到线上身份。
OVERLAY_NAME = "platform_identities.json"


def load_overlay() -> dict:
    """读平台身份覆盖表（可放在 --state 目录或集群目录）。"""
    for path in (Path(os.environ.get("ACPS_FEEDER_STATE", "")) / OVERLAY_NAME
                 if os.environ.get("ACPS_FEEDER_STATE") else None,
                 CLUSTER / OVERLAY_NAME,
                 CLUSTER / "deploy" / OVERLAY_NAME):
        if path and path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
    return {}


def _overlay_entry(key: str) -> dict:
    data = load_overlay()
    entry = data.get(key) or data.get(f"ws-{key}") or {}
    return entry if isinstance(entry, dict) else {}


def aic_of(key: str) -> str:
    override = str(_overlay_entry(key).get("aic") or "").strip()
    if override:
        return override
    data = _read_json(workspace_of(key) / "acps" / "enrollment.json")
    aic = str(((data.get("identity") or {}).get("ref") or {}).get("aic") or "")
    if aic:
        return aic
    # 队友节点（designer）是"仅作标识"的外部成员，服务器上只有寻址桩，
    # 没有 enrollment.json 的 identity；AIC 直接从它的 ACS 里读。
    acs = _read_json(workspace_of(key) / "acps" / "acs.final.json")
    return str(acs.get("aic") or "")


def endpoint_of(key: str) -> str:
    override = str(_overlay_entry(key).get("endpoint") or "").strip()
    if override:
        return override
    acs = _read_json(workspace_of(key) / "acps" / "acs.final.json")
    for ep in acs.get("endPoints") or []:
        if str((ep or {}).get("transport") or "").upper() == "JSONRPC":
            url = str((ep or {}).get("url") or "")
            if url:
                return url
    # 回退：按 env 端口拼（服务器上 .env.production 里有全部端口）
    port_env = f"ACPS_{key.upper()}_PORT"
    port = os.getenv(port_env)
    host = os.getenv("ACPS_PUBLIC_HOST", "")
    if port and host:
        return f"https://{host}:{port}/rpc"
    return ""


# ── 去重账本 ──────────────────────────────────────────────────────────────────

def fingerprint(spec: dict, topic: str) -> str:
    """组合指纹：(agent, skill, 形态变体, topic) 的 sha256 前 16 位。

    **必须带形态变体**：同一个 agent+skill 可以有多种任务形态
    （例如 supervisor/research.orchestrate 既有"常规协作"又有
    "AutoResearch 论文复现"），若只按 agent|skill|topic 取指纹，两者会撞车，
    重型形态永远选不中（2026-10-19 实测）。

    兼容性：只有声明了 `id` 的形态才把 id 混进指纹，普通形态沿用旧公式，
    已有账本不受影响。
    """
    variant = str(spec.get("id") or "")
    raw = f"{spec['agent']}|{spec['skill']}|{variant}|{topic}" if variant \
        else f"{spec['agent']}|{spec['skill']}|{topic}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class Ledger:
    """追加式账本：一行一次派发。重启后据此跳过已用过的组合。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self.entries.append(json.loads(line))
                except ValueError:
                    continue

    def used(self) -> set[str]:
        return {str(e.get("fingerprint")) for e in self.entries if e.get("fingerprint")}

    def add(self, entry: dict) -> None:
        """追加一条。写不进去时**不抛异常**——账本故障不该让已发出的调用丢失记录，
        也不该让整轮派发崩掉；改为打到 stderr 并继续（下次启动会重放缺失组合，
        代价只是可能重复派一次，远小于"派了却没记账"）。"""
        self.entries.append(entry)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"  ⚠ 账本写入失败（本次调用已发出但未留痕）：{exc}", file=sys.stderr, flush=True)

    def stats(self) -> dict:
        by_agent: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for e in self.entries:
            by_agent[str(e.get("agent"))] = by_agent.get(str(e.get("agent")), 0) + 1
            state = str(e.get("final_state") or e.get("outcome") or "?")
            by_state[state] = by_state.get(state, 0) + 1
        return {"total": len(self.entries), "by_agent": by_agent, "by_state": by_state}


def next_tasks(specs: list[dict], topics: list[str], used: set[str],
               limit: int, rng: random.Random, *, order: str = "random",
               heavy_quota: int | None = None,
               designer_quota: int = 0) -> list[dict]:
    """取未用过的 (agent, skill, topic) 组合。

    `order`：
    - `"random"`（默认）：把**全部**未用组合打乱后取前 N 个。
    - `"balanced"`：按 agent 轮转。

    `heavy_quota`：每轮重型任务（AutoResearch 复现）的精确配额；None=不限。
    `designer_quota`：每轮派给队友 designer 的精确配额（默认 1）。
    """
    candidates: list[dict] = []
    for spec in specs:
        for topic in topics:
            fp = fingerprint(spec, topic)
            if fp in used:
                continue
            candidates.append({"spec": spec, "topic": topic, "fingerprint": fp})

    if order != "balanced":
        rng.shuffle(candidates)          # 全随机：顺序不固定
        quotas: dict[str, int] = {}
        if heavy_quota is not None:
            quotas["autoresearch"] = heavy_quota
        if designer_quota > 0:
            quotas["designer-check"] = designer_quota
        if not quotas:
            return candidates[:limit]
        picked: list[dict] = []
        used_topics: set[str] = set()
        remaining = list(candidates)
        for spec_id, want in quotas.items():
            group = [c for c in remaining
                     if (c["spec"].get("id") or "") == spec_id
                     and c["topic"] not in used_topics]
            take = group[: max(0, min(want, len(group), limit - len(picked)))]
            picked.extend(take)
            used_topics |= {c["topic"] for c in take}
            gone = {id(c) for c in take}
            remaining = [c for c in remaining if id(c) not in gone]
        # 剩余名额给**非配额**形态；否则配额形态会被"剩余填充"再塞回来，
        # 配额就失效了（实测：配额 1 却出了 12 个）。
        rest = [c for c in remaining
                if (c["spec"].get("id") or "") not in quotas
                and c["topic"] not in used_topics]
        # 轮内主题不重复（同主题可能挂在多个形态下）
        seen_topic: set[str] = set()
        uniq: list[dict] = []
        for c in rest:
            if c["topic"] in seen_topic:
                continue
            seen_topic.add(c["topic"])
            uniq.append(c)
        picked.extend(uniq[: max(0, limit - len(picked))])
        rng.shuffle(picked)              # 混合后仍保持随机顺序
        return picked

    # ── 平衡模式：按 agent 轮转 ──
    pools: dict[str, list[dict]] = {}
    for cand in candidates:
        pools.setdefault(cand["spec"]["agent"], []).append(cand)
    for items in pools.values():
        rng.shuffle(items)

    out: list[dict] = []
    taken: dict[str, int] = {agent: 0 for agent in pools}
    while len(out) < limit and any(pools[a] for a in pools):
        available = [a for a in pools if pools[a]]
        low = min(taken[a] for a in available)
        pick_from = [a for a in available if taken[a] == low]
        agent = rng.choice(pick_from)
        out.append(pools[agent].pop(0))
        taken[agent] += 1
    return out


# ── AIP 调用 ──────────────────────────────────────────────────────────────────

def trust_bundle_for(key: str) -> Path | None:
    """给出可用于校验目标端点的信任锚文件。

    **优先级（2026-10-19 实测修正）**：

    1. `--ca` 显式指定；
    2. **调用方工作区自带的 trust-bundle** —— 服务器上这份是平台完整链
       （`Agent CA Root` + `ACPs Root CA`），实测 `Verify return code: 0`；
    3. 目标工作区的 trust-bundle（同机构部署时等价）；
    4. 最后才自动从端点抓链。

    为什么把"自带 bundle"提到"自动抓链"之前：抓链只能拿到服务端下发的
    **中间 CA**（根 CA 平台不公开），用中间 CA 当锚在部分 OpenSSL 版本上会报
    `unable to get issuer certificate`——本地 dev 环境曾被这条坑过一次。
    服务器上有更权威的材料时，没有理由去猜。
    """
    if trust_override:
        return Path(trust_override)

    # 调用方与目标各自工作区的 bundle 都是**平台完整链**，优先于抓链（见 docstring）
    caller = caller_key_global or "supervisor"
    for candidate in (
        workspace_of(caller) / "acps" / "trust-bundle.pem",
        workspace_of(key) / "acps" / "trust-bundle.pem",
    ):
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    endpoint = endpoint_of(key)
    if not endpoint:
        return None
    host, port = _host_port(endpoint)
    if not host:
        return None

    cache = _ca_cache_dir() / f"{host}_{port}.pem"
    if cache.is_file() and cache.stat().st_size > 0:
        return cache

    try:
        pem = _fetch_chain(host, port)
    except Exception:  # noqa: BLE001  抓不到不是致命错误，交给调用方决定
        return None
    if not pem:
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(pem, encoding="utf-8")
    except OSError:
        # 目录不可写就用临时文件（沙箱环境常见）
        import tempfile

        fd, name = tempfile.mkstemp(suffix=".pem")
        os.close(fd)
        Path(name).write_text(pem, encoding="utf-8")
        return Path(name)
    return cache


def _ca_cache_dir() -> Path:
    base = os.getenv("ACPS_FEEDER_CA_DIR", "").strip()
    return Path(base) if base else Path(os.getenv("TMPDIR") or os.getenv("TEMP") or "/tmp") / "acps-feeder-ca"


def _host_port(endpoint: str) -> tuple[str, int]:
    from urllib.parse import urlparse  # noqa: PLC0415

    parsed = urlparse(endpoint)
    return (parsed.hostname or ""), int(parsed.port or 443)


def _fetch_chain(host: str, port: int) -> str:
    """连一次目标端点，把服务端下发的**非叶子**证书拼成信任锚。

    用中间 CA 当锚是刻意的：平台只公开中间证书，不公开根。
    安全上等价性说明见 docs/task-feeder.md —— 我们只连**指定 IP:端口**，
    且随后仍由 SDK 做 peer AIC 校验（证书 CN 必须等于目标 AIC）。
    """
    import socket
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            leaf = tls.getpeercert(binary_form=True)
            del leaf
    # ssl 模块只暴露 leaf；中间 CA 用 openssl 取（项目服务器上已有）
    import re
    import subprocess

    out = subprocess.run(
        ["openssl", "s_client", "-connect", f"{host}:{port}", "-showcerts",
         "-servername", host],
        input="", capture_output=True, text=True, timeout=20,
    )
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                        out.stdout, re.S)
    if len(blocks) < 2:
        return ""
    return "\n".join(blocks[1:]) + "\n"


def _ssl_for(key: str):
    """构造目标端点用的 SSL context（带 peer AIC 校验由 SDK 负责）。"""
    import ssl

    bundle = trust_bundle_for(key)
    if bundle is None:
        return None
    ctx = ssl.create_default_context(cafile=str(bundle))
    ctx.check_hostname = False      # 用 IP 直连；身份由 SDK 的 CN==AIC 校验兜底
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def build_command(*, sender_aic: str, text: str, skill: str,
                  session_id: str, task_id: str) -> dict:
    """AIP v2 的 TaskCommand。skillId 走 dataItems[].metadata（协议没有专门字段）。"""
    return {
        "type": "task-command",
        "id": f"msg-{uuid.uuid4()}",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "senderRole": "leader",
        "senderId": sender_aic,
        "sessionId": session_id,
        "taskId": task_id,
        "command": "start",
        "commandParams": {},
        "dataItems": [{"type": "text", "text": text, "metadata": {"skillId": skill}}],
    }


def _state_of(result: dict) -> str:
    return str(((result.get("status") or {}).get("state")) or "")


JOB_ID_RE = re.compile(r"job-\d{8}-\d{6}-[0-9a-f]{4}")

# supervisor 的作业目录（与它共用同一个工作区，直接读文件即可，
# **不用发 AIP 请求**——否则核实本身又会虚增 access 记录）。
JOBS_DIR = CLUSTER / "ws-supervisor" / "jobs"


def extract_job_id(text: str) -> str:
    match = JOB_ID_RE.search(text or "")
    return match.group(0) if match else ""


def read_job_status(job_id: str) -> dict:
    data = _read_json(JOBS_DIR / job_id / "status.json")
    return data if isinstance(data, dict) else {}


async def await_job(job_id: str, *, timeout: float, poll: float) -> dict:
    """轮询作业的 status.json 直到真正结束，用真实结果覆盖"受理回执"口径。

    为什么需要（2026-10-19 实测）：supervisor 对长任务常只回一张"受理回执"就
    进 awaiting-completion，feeder 于是记 ok；但真实作业在后面才跑，可能
    lock-timeout / failed。账面成功率因此虚高（8 条 ok 里有几条实际作业失败）。
    """
    deadline = time.monotonic() + timeout
    last: dict = {}
    while True:
        last = read_job_status(job_id) or last
        state = str((last or {}).get("state") or "")
        if state in ("done", "failed", "interrupted"):
            break
        if time.monotonic() >= deadline:
            return {
                "job_state": state or "unknown",
                "job_verified": False,
                "outcome": "job-timeout",
                "detail": f"作业 {job_id} 在 {int(timeout)}s 内未结束（state={state or '?'}）",
            }
        await asyncio.sleep(poll)

    ok = state == "done"
    return {
        "job_state": state,
        "job_run_status": last.get("run_status"),
        "job_verified": True,
        "job_exit_code": last.get("exit_code"),
        "job_iterations": last.get("iterations"),
        "outcome": "ok" if ok else "failed",
        "final_state": str(last.get("run_status") or state),
    }


async def dispatch_one(*, spec: dict, topic: str, caller_key: str, caller_aic: str,
                       endpoint: str, target_aic: str, wait: float,
                       poll: float | None = None) -> dict:
    """派一个任务并等终态。返回结果字典（不抛异常——失败也要记账）。"""
    from wit.acps.direct import AipDirectClient      # noqa: PLC0415

    ws = workspace_of(caller_key)
    # 信任锚：优先 --ca；否则自动抓目标链（见 trust_bundle_for 说明）。
    # 抓不到时退回调用方工作区自带的 trust-bundle（同机构部署时是对的）。
    bundle = trust_bundle_for(spec["agent"]) or (ws / "acps" / "trust-bundle.pem")
    client = AipDirectClient(
        client_certificate=ws / "acps" / "client" / "certificate.pem",
        client_private_key=ws / "acps" / "client" / "private-key.pem",
        trust_bundle=Path(bundle),
        local_aic=caller_aic,
        strict_interop=True,
        timeout=60.0,
    )
    task_id = f"feed-{uuid.uuid4()}"
    session_id = f"feed-session-{uuid.uuid4()}"
    # 轮询间隔：每问一次状态就是一条 access 记录，问太勤会把"调用量"冲成
    # 一堆 12ms 的状态查询（2026-10-19 实测：22 个任务产生 1.2 万条记录，
    # 八成是轮询）。默认 30s。
    if poll is None:
        try:
            poll = float(os.getenv("ACPS_FEEDER_POLL_SECONDS", "30"))
        except ValueError:
            poll = 30.0
    poll = max(1.0, poll)
    text = spec["template"].format(topic=topic)
    started = time.monotonic()
    out: dict = {"task_id": task_id, "session_id": session_id, "outcome": "unknown"}
    try:
        command = build_command(sender_aic=caller_aic, text=text, skill=spec["skill"],
                                session_id=session_id, task_id=task_id)
        resp = await client.call(
            endpoint,
            {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}",
             "params": {"command": command}},
            expected_partner_aic=target_aic,
        )
        if "error" in resp:
            err = resp.get("error") or {}
            out.update(outcome="dispatch_error",
                       detail=f"{err.get('code')}: {str(err.get('message'))[:200]}")
            return out

        result = resp.get("result") or {}
        state = _state_of(result)
        out["accepted_state"] = state

        deadline = time.monotonic() + wait
        # AIP 的"产物已就绪"态是 `awaiting-completion`（等 leader 关单），
        # **不是** `completed`——本项目里绝大多数任务停在这里（2026-10-19 实测：
        # writer 跑完直接进 awaiting-completion，早期只认 completed 会一直轮询到超时）。
        # `awaiting-input` 也是终态：伙伴缺料，需要调用方补，继续等没有意义。
        TERMINAL = ("completed", "awaiting-completion", "awaiting-input",
                    "failed", "rejected", "cancelled")
        while state not in TERMINAL and time.monotonic() < deadline:
            await asyncio.sleep(poll)
            get_cmd = {
                "type": "task-command", "id": f"msg-{uuid.uuid4()}",
                "sentAt": datetime.now(timezone.utc).isoformat(),
                "senderRole": "leader", "senderId": caller_aic,
                "sessionId": session_id, "taskId": task_id,
                "command": "get", "commandParams": {}, "dataItems": [],
            }
            try:
                resp = await client.call(
                    endpoint,
                    {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}",
                     "params": {"command": get_cmd}},
                    expected_partner_aic=target_aic,
                )
            except Exception as exc:                 # noqa: BLE001 网络抖动不终止
                out.setdefault("poll_errors", []).append(type(exc).__name__)
                continue
            result = resp.get("result") or result
            state = _state_of(result) or state

        out["final_state"] = state
        products = result.get("products") or []

        # ── 关单（AIP 必需的一步）────────────────────────────────────────────
        # 伙伴产出后停在 `awaiting-completion`，**必须由 leader 发 `complete` 才进终态**。
        # 只轮询不关单的后果：任务永远停在 awaiting-completion，对端每次重启都要
        # reconcile 一次"孤儿任务"，账本也拿不到 completed（2026-10-19 实测）。
        # 注意协议约束：`complete` **只能**在 awaiting-completion 时发，
        # 其它状态发会被判 `This operation is not supported`（SDK 已校验）。
        if state == "awaiting-completion" and products:
            try:
                done_cmd = {
                    "type": "task-command", "id": f"msg-{uuid.uuid4()}",
                    "sentAt": datetime.now(timezone.utc).isoformat(),
                    "senderRole": "leader", "senderId": caller_aic,
                    "sessionId": session_id, "taskId": task_id,
                    "command": "complete", "commandParams": {}, "dataItems": [],
                }
                resp = await client.call(
                    endpoint,
                    {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}",
                     "params": {"command": done_cmd}},
                    expected_partner_aic=target_aic,
                )
                closed = _state_of(resp.get("result") or {})
                if closed:
                    out["closed_state"] = closed
                out["completed"] = "error" not in resp
            except Exception as exc:      # noqa: BLE001 关单失败不影响"调用已发生"的事实
                out["complete_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

        # 成功口径：产物已就绪（completed 或 awaiting-completion 且有产物）。
        # 对"累积调用量"这件事而言，两者的价值完全相同——对端都真的跑完并交付了。
        if state in ("completed", "awaiting-completion"):
            out["outcome"] = "ok" if products else "no_product"
        elif state == "awaiting-input":
            out["outcome"] = "needs_input"
        else:
            out["outcome"] = state or "timeout"
        out["product_count"] = len(products)
        out["product_bytes"] = len(json.dumps(products, ensure_ascii=False).encode("utf-8"))

        # ── 核实真实结果：supervisor 常只回"受理回执"，真正的活在后端作业里 ──
        # 回执里带作业号，就等它真的结束，用真实状态覆盖 outcome。
        receipt = json.dumps(products, ensure_ascii=False)
        job_id = extract_job_id(receipt) or extract_job_id(str(out.get("detail") or ""))
        if job_id and out.get("outcome") in ("ok", "no_product"):
            out["job_id"] = job_id
            try:
                job_wait = float(os.getenv("ACPS_JOB_WAIT_SECONDS", "1800"))
            except ValueError:
                job_wait = 1800.0
            print(f"    ↳ 受理回执指向作业 {job_id}，等它真正结束（最多 {int(job_wait)}s）…",
                  flush=True)
            job = await await_job(job_id, timeout=job_wait, poll=poll)
            out.update(job)
            print(f"    ↳ 作业结果: {job.get('job_state')} "
                  f"run={job.get('job_run_status')} → outcome={out.get('outcome')}",
                  flush=True)

        out["elapsed_s"] = round(time.monotonic() - started, 1)
        return out
    except Exception as exc:                          # noqa: BLE001 一条失败不影响后续
        out.update(outcome="exception",
                   detail=f"{type(exc).__name__}: {str(exc)[:220]}",
                   elapsed_s=round(time.monotonic() - started, 1))
        return out
    finally:
        await client.aclose()


# ── 主流程 ────────────────────────────────────────────────────────────────────

def select_specs(args, ledger: Ledger) -> list[dict]:
    """选出本次可派的形态。

    **默认只派给 supervisor**（2026-10-19 榜单实测结论）：

    平台排行榜按 **caller 侧 span**（`record.aic == body.caller.aic`）归属计数。
    脚本直连伙伴时，只有伙伴侧（被调方）会记录，平台**不认**；
    必须让 supervisor 真正出站委派，才会产生被计入的 caller 侧记录。
    而且 supervisor 一次运行会扇出多个子委派（scout/writer…），
    一次派发等于多条有效调用——性价比也最高。

    `--targets` 显式指定时才走别的路径（例如只压测某个伙伴的吞吐）。
    """
    targets = {t.strip() for t in (args.targets or "").split(",") if t.strip()}
    if not targets:
        targets = set(DEFAULT_TARGETS)
    # designer 是"跨节点附加项"：即使用户只写了 --targets supervisor，
    # 只要 --designer-per-round > 0（默认 1），也把它加进来。
    if int(getattr(args, "designer_per_round", 0) or 0) > 0:
        targets = set(targets) | {"designer"}
    specs = [s for s in SPECS
             if (s["agent"] in targets)
             and (s["public"] or args.include_protected)
             # 重型任务（AutoResearch 论文复现）默认不参与轮转：
             # 单次要真跑 GPU 沙盒 + 多轮迭代，混进来会把队列拖满。
             # 要跑就显式加 --include-heavy（且建议配 --targets supervisor）。
             and (not s.get("heavy") or getattr(args, "include_heavy", False))]
    return specs


def heavy_quota_of(args) -> int | None:
    """本轮重型任务（AutoResearch 复现）的配额。

    - 没开 `--include-heavy` → 0（不派重型）；
    - 开了但没给 `--heavy-per-round` → 默认 1 个（每轮恰好一次复现）；
    - 显式给了 → 用给的值（0 = 一个都不要）。
    """
    if not getattr(args, "include_heavy", False):
        return 0
    value = getattr(args, "heavy_per_round", None)
    if value is None:
        return 1
    return max(0, int(value))


def run_once(args, ledger: Ledger, specs: list[dict], *, limit: int,
             rng: random.Random) -> dict:
    used = ledger.used()
    picks = next_tasks(specs, TOPICS, used, limit, rng,
                       order=getattr(args, "order", "random"),
                       heavy_quota=heavy_quota_of(args),
                       designer_quota=int(getattr(args, "designer_per_round", 0) or 0))
    if not picks:
        return {"dispatched": 0, "note": "主题池已用尽（可扩充 TOPICS 或清空账本）"}

    caller_key = args.caller
    caller_aic = aic_of(caller_key)
    if not caller_aic:
        raise SystemExit(f"调用方 ws-{caller_key} 没有 AIC（enrollment.json 缺 identity.ref.aic）")

    # 池子不够一轮时要说清楚，否则"设了 30 却只跑了 21"会让人以为脚本有 bug
    if len(picks) < limit:
        print(f"  ⚠ 本轮只有 {len(picks)} 个可用组合（请求 {limit} 个）："
              f"主题池 {len(TOPICS)} × 形态 {len(specs)} 减去账本已用的结果。"
              f"不够就扩充 TOPICS，或清空账本。")

    # 开跑前先把本轮构成说清楚（否则要等跑到那一条才知道有没有复现/队友调用）
    n_heavy = sum(1 for c in picks if c["spec"].get("heavy"))
    n_designer = sum(1 for c in picks if c["spec"].get("agent") == "designer")
    print(f"  本轮计划 {len(picks)} 个：常规 {len(picks) - n_heavy - n_designer}，"
          f"复现 {n_heavy}，队友 designer {n_designer}"
          + (f"（重型在第 {[i + 1 for i, c in enumerate(picks) if c['spec'].get('heavy')]} 位）"
             if n_heavy else "")
          + (f"（designer 在第 {[i + 1 for i, c in enumerate(picks) if c['spec'].get('agent') == 'designer']} 位）"
             if n_designer else ""))

    results = []
    for cand in picks:
        spec, topic, fp = cand["spec"], cand["topic"], cand["fingerprint"]
        target_aic = aic_of(spec["agent"])
        endpoint = endpoint_of(spec["agent"])
        # 同一 agent+skill 可能有多种形态（普通协作 / AutoResearch 复现），
        # 日志里必须能区分，否则看输出会以为"没生效"。
        variant = f"[{spec['id']}] " if spec.get("id") else ""
        label = f"{variant}{spec['agent']:14s} {spec['skill']:24s} {topic[:24]}"

        if not target_aic:
            print(f"  跳过 {label}（目标无 AIC）")
            continue
        if not endpoint:
            print(f"  跳过 {label}（目标无端点）")
            continue

        if args.dry_run:
            print(f"  [dry] {label}")
            results.append({"topic": topic, "outcome": "dry_run"})
            continue

        print(f"  → {label}", flush=True)
        res = asyncio.run(dispatch_one(
            spec=spec, topic=topic, caller_key=caller_key, caller_aic=caller_aic,
            endpoint=endpoint, target_aic=target_aic, wait=args.wait,
        ))
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": spec["agent"], "skill": spec["skill"], "topic": topic,
            # variant：区分同 agent+skill 的不同形态（normal / autoresearch），
            # 否则事后从账本看不出这一条到底是不是论文复现。
            "variant": spec.get("id") or "normal",
            "fingerprint": fp, "caller": caller_key,
            "target_aic": target_aic, **res,
        }
        ledger.add(entry)
        print(f"    {res.get('outcome')}  state={res.get('final_state', '-')} "
              f"elapsed={res.get('elapsed_s', '-')}s  bytes={res.get('product_bytes', 0)}")
        results.append(entry)
        if args.rate > 0:
            time.sleep(args.rate)
    return {"dispatched": len([r for r in results if r.get("outcome") != "dry_run"]),
            "results": results}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="研枢集群任务派发器（真实 mTLS，主题去重，累积平台调用量）")
    ap.add_argument("--caller", default="supervisor", help="用哪个工作区证书当调用方")
    ap.add_argument("--targets", default="", help="只派给这些 agent，逗号分隔")
    ap.add_argument("--limit", type=int, default=None,
                    help="本轮派几个（给定时覆盖 --round-size，且只跑 1 轮）")
    ap.add_argument("--round-size", type=int, default=30,
                    help="每轮派几个，默认 30（跑完一轮就停）")
    ap.add_argument("--rounds", type=int, default=None,
                    help="跑几轮，默认 1（即跑完一轮就停）；0=不限（配合 --watch）")
    ap.add_argument("--per-round", type=int, default=None,
                    help="（旧名，等价于 --round-size）")
    ap.add_argument("--order", choices=("random", "balanced"), default="random",
                    help="派发顺序：random=全随机（默认）；balanced=按 agent 轮转")
    ap.add_argument("--interval", type=float, default=60.0, help="轮与轮之间的间隔（秒）")
    ap.add_argument("--hours", type=float, default=0.0, help="总时长上限（0=不限）")
    ap.add_argument("--rate", type=float, default=5.0,
                    help="串行：两个任务之间的最小间隔（秒）；已完成的任务本身已等待终态")
    ap.add_argument("--wait", type=float, default=600.0,
                    help="单任务等终态上限（秒）；超时记 working 并继续下一个")
    ap.add_argument("--state", default=str(DEFAULT_STATE), help="状态目录（账本放这里）")
    ap.add_argument("--include-protected", action="store_true",
                    help="同时派受保护能力（需目标侧 PEP 放行，否则会被拒）")
    ap.add_argument("--include-heavy", action="store_true",
                    help="同时派重型任务（AutoResearch 论文复现：真跑 GPU 沙盒，耗时长、失败率高）")
    ap.add_argument("--heavy-per-round", type=int, default=None,
                    help="每轮恰好派几个重型任务（需配合 --include-heavy）。"
                         "不给时默认 1；0=不要重型。"
                         "注意：不设配额时重型会占满一半（supervisor 只有 2 个形态）")
    ap.add_argument("--designer-per-round", type=int, default=0,
                    help="每轮由派发器**直接**派给队友 designer 几个（默认 0=不派）。"
                         "默认改为 0：designer 现在由 supervisor 在计划第 1 步调控，"
                         "不再由脚本绕过 supervisor 直连（2026-10-19 需求）")
    ap.add_argument("--dry-run", action="store_true", help="只看派什么，不联网不记账")
    ap.add_argument("--watch", action="store_true", help="常驻按轮派发")
    ap.add_argument("--stats", action="store_true", help="只打印累计统计")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（复现用）")
    ap.add_argument("--ca", default=None,
                    help="信任锚文件（平台 CA 链）。默认自动从目标端点抓取中间 CA；"
                         "服务器上通常不需要，用 --ca /opt/wit/.../trust-bundle.pem 亦可")
    args = ap.parse_args(argv)

    global trust_override, caller_key_global
    trust_override = args.ca
    caller_key_global = args.caller

    state_dir = Path(args.state)
    ledger = Ledger(state_dir / "ledger.jsonl")

    if args.stats:
        s = ledger.stats()
        print(f"账本: {ledger.path}")
        print(f"累计派发: {s['total']}")
        if s["by_agent"]:
            print("\n按 agent:")
            for k, v in sorted(s["by_agent"].items(), key=lambda x: -x[1]):
                print(f"  {k:16s} {v:4d}")
        if s["by_state"]:
            print("\n按结果:")
            for k, v in sorted(s["by_state"].items(), key=lambda x: -x[1]):
                print(f"  {k:22s} {v:4d}")
        return 0

    specs = select_specs(args, ledger)
    if not specs:
        print("没有匹配的任务形态（检查 --targets / --include-protected）")
        return 1

    rng = random.Random(args.seed)

    # ── 轮次与每轮数量 ────────────────────────────────────────────────────
    # 默认：跑 1 轮、每轮 30 个，跑完即停。
    #   --limit N        → 只跑这一轮，N 个（覆盖 --round-size）
    #   --rounds R       → 跑 R 轮；0 = 不限（配合 --watch）
    #   --watch          → 未显式给 --rounds 时视为不限轮次（仍受 --hours 限制）
    if args.limit is not None:
        round_size, rounds = args.limit, 1
    else:
        round_size = args.per_round or args.round_size
        if args.rounds is not None:
            rounds = args.rounds
        else:
            rounds = 0 if args.watch else 1
    if rounds == 0 and not args.watch:
        rounds = 1                         # 没写 --watch 就别无限跑

    print(f"调用方 : ws-{args.caller}  ({aic_of(args.caller) or '无 AIC'})")
    print(f"目标   : {sorted({s['agent'] for s in specs})}")
    print(f"账本   : {ledger.path}（已有 {len(ledger.entries)} 条）")
    print(f"顺序   : {args.order}（{'全随机' if args.order == 'random' else '按 agent 轮转'}）")
    print(f"节奏   : 串行，单轮 {round_size} 个，"
          f"{'跑 ' + str(rounds) + ' 轮' if rounds else '不限轮次'}")
    print(f"模式   : {'【dry-run】' if args.dry_run else '【真实派发】'}\n")

    if not args.dry_run:
        missing = [s["agent"] for s in specs if not aic_of(s["agent"])]
        if missing:
            print(f"⚠ 这些 agent 没有 AIC，会被跳过: {sorted(set(missing))}")

    # ── 统一主循环：串行、按轮、跑完即停 ──
    deadline = time.monotonic() + args.hours * 3600 if args.hours > 0 else None
    total_dispatched = 0
    round_no = 0
    while rounds == 0 or round_no < rounds:
        round_no += 1
        total_txt = f"/{rounds}" if rounds else ""
        print(f"──── 第 {round_no}{total_txt} 轮  {datetime.now().strftime('%H:%M:%S')} ────")
        info = run_once(args, ledger, specs, limit=round_size, rng=rng)
        dispatched = int(info.get("dispatched", 0))
        total_dispatched += dispatched
        print(f"  本轮派发 {dispatched} 个"
              f"{'：' + info['note'] if info.get('note') else ''}\n")

        if info.get("note"):
            print("主题池已用尽，提前结束。")
            break
        # 是否还有下一轮
        exhausted = rounds and not info.get("note") and round_no >= rounds
        if exhausted:
            break
        if deadline and time.monotonic() + args.interval > deadline:
            print("已达 --hours 时限，结束。")
            break
        if args.dry_run:
            continue                      # dry-run 不 sleep
        print(f"  等 {args.interval:.0f}s 进入下一轮……\n")
        time.sleep(args.interval)

    if args.dry_run:
        print(f"（dry-run 不落账；账本仍为 {len(ledger.entries)} 条）")
        return 0

    s = ledger.stats()
    print(f"结束：本次派发 {total_dispatched} 个，账本累计 {s['total']}，"
          f"按结果 {s['by_state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
