"""集群名册：本集群成员的**偏好顺序 + 能力登记**。

设计要点（2026-10-02 重构）：

- **公开能力不手写**：成员上线后，公开能力在运行时从它自己工作区的
  `ws-<key>/acps/acs.final.json`（平台签发的那份 ACS）的 `skills[].id` 派生。
  此前是手写一份拷贝，改了 `@capability(id=...)` 忘了同步就会出现"代码里有的能力、
  名册说没有"的漂移（`plan_gate` 会把合法步骤判成非法）。
- **`declared_capabilities` = 登记（预期）能力**，ACS 给不出时只能手写，共三种情况：
  1. `public=False` 的能力——按定义**不进 ACS**（实测：scout 声明 4 个能力，ACS 里只有 3 个），
     任何外部发现机制都看不到它；
  2. 尚未注册上线的成员——没有 `acs.final.json`，能力无从派生（含本地没建工作区的成员）；
  3. 尚未实现的成员（例：登记了 GPU 节点但 `partner_gpu.py` 还没写）。
- **公开能力不手写**：成员上线后，公开能力从它自己工作区的 `ws-<key>/acps/acs.final.json`
  （平台签发的那份 ACS）的 `skills[].id` 派生，并与登记结果取**并集**（`capabilities` 属性）。
  要害在于：**平台已确认的能力不会再被手写表"藏住"**——此前名册是唯一来源，代码新增/改名
  能力而忘了同步名册，`plan_gate` 就会把合法步骤判成非法；现在只会在 ACS 侧自动补上。
  （剩下的残余风险：登记里写了一个代码已删除的能力——只会在伙伴侧报错，被 product_gate 拦下。）
- **身份不写进提示词**：AIC / endpoint 运行时从 ACS 解析；未上线成员标成不可调用，
  计划里派给它会被拒。这样新增/替换 agent 不用改 supervisor 的提示词，模型也无法编造或写错 AIC。
- **顺序即偏好**：`ROSTER` 的先后顺序就是偏好顺序——同一能力有多个提供者时优先用靠前的；
  `render_roster_text` 会把这条规则告诉模型。
- **名册外走发现**：非本集群成员由 `acps_discover` 发现后落盘在
  `<workspace>/acps/discovered/`，`validate_plan(..., discovered=...)` 允许派给"已发现、
  且 ACS 里确实声明了该能力"的 agent。名册表达的是**偏好**而不是唯一授权来源
  （真正的准入控制在伙伴侧的 PEP）。

本模块只依赖标准库，便于离线单测。
"""

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from arg_shapes import as_list  # 工具入参数组宽容化（见模块 docstring）

CLUSTER_DIR = Path(__file__).parent

# 发现缓存的默认位置（相对 cluster_dir）：supervisor 的工作区
DISCOVERY_WORKSPACE = "ws-supervisor"


@dataclass(frozen=True)
class RosterMember:
    key: str  # 名册标识，也是工作区名（ws-<key>）
    name: str  # 展示名
    role: str  # 一句话职责，注入提示词用
    module: str  # 常驻脚本名（不含 .py）；leader 为 supervisor
    workspace: str  # 工作区目录名
    port_env: str
    default_port: int
    kind: str = "partner"  # partner | leader
    declared_capabilities: tuple[str, ...] = ()  # 手写登记：ACS 给不出的能力（见模块说明）
    aic: str | None = None  # 运行时从 ACS 解析；None 表示尚未注册成功
    endpoint: str | None = None
    public_capabilities: tuple[str, ...] = ()  # 运行时从 ACS 的 skills 派生

    @property
    def capabilities(self) -> tuple[str, ...]:
        """可派发能力 = ACS 派生的公开能力 ∪ 手写登记的预期能力（去重保序）。"""
        merged = list(self.public_capabilities)
        for cap in self.declared_capabilities:
            if cap not in merged:
                merged.append(cap)
        return tuple(merged)

    @property
    def available(self) -> bool:
        return bool(self.aic)

    @property
    def port(self) -> int:
        return int(os.getenv(self.port_env, str(self.default_port)))


@dataclass(frozen=True)
class DiscoveredAgent:
    """一条发现缓存记录（由 `acps_discover` 写入 `<workspace>/acps/discovered/`）。"""

    aic: str
    name: str = ""
    skills: tuple[str, ...] = ()
    endpoint: str | None = None


# 成员顺序 = 偏好顺序（同一能力有多个提供者时，优先用靠前的成员）。
ROSTER: tuple[RosterMember, ...] = (
    RosterMember(
        key="scout",
        name="研枢-Scout-Partner",
        role="多源文献检索（arXiv/OpenAlex/Semantic Scholar），产出带链接的资料清单",
        module="partner_scout",
        workspace="ws-scout",
        port_env="ACPS_SCOUT_PORT",
        default_port=8445,
        # 登记：3 个公开能力（ACS 也会派生）+ 两个非公开能力（不进 ACS，只能在这里登记）
        declared_capabilities=(
            "arxiv.search",
            "openalex.search",
            "semanticscholar.search",
            "scholar.deep_fetch",
            "dataset.fetch",
        ),
    ),
    # 2026-09-27 停用：arXiv 伙伴（8443/wit-partner）已下线，检索职责由 scout 承担；
    # 原 partner_arxiv.py 已退役并移出公开仓库；恢复需从本地历史或上游重新引入
    #     RosterMember(
    #         key="arxiv",
    #         name="arXiv 检索 Partner",
    #         role="arXiv 单源检索（演示版，scout 可用时优先用 scout）",
    #         module="partner_arxiv",
    #         workspace="ws-partner",
    #         port_env="ACPS_PARTNER_PORT",
    #         default_port=8443,
    #         declared_capabilities=("arxiv.search", "arxiv.fetch_paper"),
    #     ),
    RosterMember(
        key="synthesis",
        name="研枢-Synthesis-Partner",
        role="单篇论文精读，抽取创新点/研究缺口/方法论/实验配置四要素",
        module="partner_synthesis",
        workspace="ws-synthesis",
        port_env="ACPS_SYNTHESIS_PORT",
        default_port=8446,
        declared_capabilities=("paper.extract",),
    ),
    RosterMember(
        key="contradiction",
        name="研枢-Contradiction-Partner",
        role="跨论文矛盾侦测：消费多份四要素，输出矛盾对与可检验的调和假说",
        module="partner_contradiction",
        workspace="ws-contradiction",
        port_env="ACPS_CONTRADICTION_PORT",
        default_port=8449,
        declared_capabilities=("contradiction.detect",),
    ),
    RosterMember(
        key="frontier",
        name="研枢-Frontier-Partner",
        role="速度前沿探测：按引用速度识别近 6-12 个月爆发的新兴方向，服务选题",
        module="partner_frontier",
        workspace="ws-frontier",
        port_env="ACPS_FRONTIER_PORT",
        default_port=8450,
        declared_capabilities=("frontier.detect", "frontier.citation_profile"),
    ),
    RosterMember(
        key="writer",
        name="研枢-Writer-Partner",
        role="论文起草，outline/full 双模式，可按审稿意见返工",
        module="partner_writer",
        workspace="ws-writer",
        port_env="ACPS_WRITER_PORT",
        default_port=8447,
        declared_capabilities=("paper.write",),
    ),
    # 2026-10-02 启用（路线 B）：gpu 跑在**本机**、身份由本地 dev CA 铸造（未在平台注册），
    # 通过反向隧道被服务器侧寻址（服务器上放了它的"寻址桩" acs.final+enrollment，无密钥）。
    # 名册据此解析身份；未上线的成员仍会被标成不可调用。
    RosterMember(
        key="gpu",
        name="研枢-GPU-Partner",
        role="本地 GPU 计算节点（RTX 5060 8GB 沙盒容器）：跑 PyTorch 实验，回传 stdout 与产物",
        module="partner_gpu",
        workspace="ws-gpu",
        port_env="ACPS_GPU_PORT",
        default_port=8460,
        # 公开能力由 ACS 派生（gpu.env/gpu.status/gpu.data.list）；
        # 非公开的（acps_skill=True 或 public=False）在这里登记
        declared_capabilities=("gpu.data.fetch", "gpu.run"),
    ),
    # 实验员（experiment）：平台注册已通过（2026-10-02），服务器上以 8461 上线。
    # 未上线前 ACS 缺失 → 本条目 available=False（名册里可见但不可调度），上线后自动就绪。
    RosterMember(
        key="experiment",
        name="研枢-Experiment-Partner",
        role="实验员：把研究假设转写为实验代码，持委托令牌在 GPU 沙盒执行并产出结构化实验报告",
        module="partner_experiment",
        workspace="ws-experiment",
        port_env="ACPS_EXPERIMENT_PORT",
        default_port=8461,
        # exp.run 在 ACS 里（acps_skill=True ⇒ 可发现但非公开），这里登记只为「未拿到 ACS 时也能规划」
        declared_capabilities=("exp.run",),
    ),
    # 队友 gjw 的"研究方案设计"节点（2026-10-09 手写登记；**外部成员**，不跑在本集群）。
    # 它的 ACS 描述仅 30 字、平台语义索引搜不到（4 组中英查询实测 0 命中）→ 只能靠
    # 手写条目 + 寻址桩（ws-designer/acps/acs.final.json，无密钥）被 leader 规划到。
    # 端点 https://192.0.2.10:8590/rpc 与集群同机（TCP 实测可达），证书 SAN 覆盖该 IP。
    RosterMember(
        key="designer",
        name="Yanshu Research Designer（队友节点）",
        role="研究方案设计与校验：把研究问题/证据转成可执行方案，做可行性、样本量、计划校验",
        module="server_acps",  # 外部成员：无本地常驻脚本，仅作标识（不由本集群拉起）
        workspace="ws-designer",
        port_env="ACPS_DESIGNER_PORT",
        default_port=8590,
        declared_capabilities=(
            "research.validate_request",
            "research.validate_result",
            "research.plan_template",
            "research.estimate_sample_size",
            "research.assess_feasibility",
            "research.validate_plan",
        ),
    ),
    # 队友 hkr 的「问迹」问数/问答节点（2026-10-19 登记；**外部成员**，不跑在本集群）。
    # 它的 ACS 是通过 acps_discover 抓到的完整原件，落在 ws-wenji/acps/acs.final.json
    # （无密钥的寻址桩，只在服务器上），load_roster 从这里解析 aic / endpoint / skills。
    # 端点 https://8.160.176.215:8601/rpc 与集群同机（TCP + TLS 实测可达）。
    # 2026-10-23 自服务器回流：此前只存在于服务器名册，本地缺失（防下次同步被旧版覆盖）。
    RosterMember(
        key="wenji",
        name="问迹·可解释问数问答智能体（队友节点）",
        role="结构化问数与文档问答：NL2SQL 精准问数、RAG 带引用问答、跨源多跳，全链路可追溯",
        module="server_acps",  # 外部成员：无本地常驻脚本，仅作标识（不由本集群拉起）
        workspace="ws-wenji",
        port_env="ACPS_WENJI_PORT",
        default_port=8601,
        declared_capabilities=("wenji.ask", "wenji.data_query", "wenji.doc_qa", "wenji.schema"),
    ),
    RosterMember(
        key="critic",
        name="研枢-Critic-Partner",
        role="模拟审稿，输出结构化裁决（gate/issues/evidence_check）",
        module="partner_critic",
        workspace="ws-critic",
        port_env="ACPS_CRITIC_PORT",
        default_port=8448,
        declared_capabilities=("paper.review",),
    ),
    # 2026-10-03 资源 agent 化第 2-4 实例（本地 dev 身份，模式同 gpu 路线 B）：
    # cpu=纯 CPU 沙盒任务；eval=评测服务（数据不出域只发分数）；model=算法托管（权重不出域只推理）
    RosterMember(
        key="cpu",
        name="研枢-CPU-Partner",
        role="本地 CPU 计算节点（断网加固沙盒）：跑纯 CPU 任务，回传 stdout 与产物",
        module="partner_cpu",
        workspace="ws-cpu",
        port_env="ACPS_CPU_PORT",
        default_port=8462,
        declared_capabilities=("cpu.run",),
    ),
    RosterMember(
        key="eval",
        name="研枢-Eval-Partner",
        role="评测服务：基准橱窗公开、提交保险柜（标签不出域只发分数，支持预测/代码双模式）",
        module="partner_eval",
        workspace="ws-eval",
        port_env="ACPS_EVAL_PORT",
        default_port=8464,
        declared_capabilities=("eval.submit",),
    ),
    RosterMember(
        key="model",
        name="研枢-Model-Partner",
        role="算法托管：model.info 橱窗公开、model.predict 保险柜（权重不出域只暴露推理）",
        module="partner_model",
        workspace="ws-model",
        port_env="ACPS_MODEL_PORT",
        default_port=8467,
        declared_capabilities=("model.predict",),
    ),
    RosterMember(
        key="kb",
        name="研枢-KB-Partner",
        role="知识库：数据不动计算动（受限查询 DSL + 聚合下推），字段级脱敏三档（目录/摘要/全文）",
        module="partner_kb",
        workspace="ws-kb",
        port_env="ACPS_KB_PORT",
        default_port=8599,  # 与注册端点/现网一致（原 8465 系换端口前遗留，2026-10-15 审查 E）
        declared_capabilities=("kb.query", "kb.ingest"),
    ),
    # 2026-10-25 新增（路线 B，服务器常驻）：任务工作区管理员——伙伴间文件交换
    # 不再走 leader 上下文。ws.* 全部非公开（凭 supervisor 委派令牌调用），见
    # partner_workspace.py 头部设计说明与 docs/workspace-partner.md。
    RosterMember(
        key="workspace",
        name="研枢-Workspace-Partner",
        role=("任务工作区管理员：按任务开辟服务器端工作区。四分区：repo/ 只读参考"
              "（ws.clone_repo 专用）、products/ 产物归档（服务端按调用方+序号版本化）、"
              "scratch/<自己>/ 草稿领地（服务端强制前缀）、shared/ 协作总线"
              "（if_match 乐观锁）。委派它须在请求文本写 task_id=<任务作用域>"
              "（令牌据此限定可访问的工作区）；写入路径必须带分区前缀"),
        module="partner_workspace",
        workspace="ws-workspace",
        port_env="ACPS_WORKSPACE_PORT",
        default_port=8470,
        declared_capabilities=(
            "ws.create",
            "ws.write",
            "ws.read",
            "ws.list",
            "ws.clone_repo",
            "ws.gc",
        ),
    ),
    # 2026-10-04 降级搁置：HPC 网关（外部调度器底座）代码与测试保留
    # （partner_hpc.py + hpc_runner.py，H 系列 8/8），但演示价值条件性
    # （需真实第二机构的 Slurm 才成立，本地为 mock 后端）——不进名册不进演示；
    # 需要时取消注释即恢复。    # RosterMember(    #     key="hpc",    #     name="研枢-HPC-Partner",    #     role="机构调度器网关：异步作业（提交→排队→沙盒执行→取结果），存量 HPC 不改造接入",    #     module="partner_hpc",    #     workspace="ws-hpc",    #     port_env="ACPS_HPC_PORT",    #     default_port=8466,    #     declared_capabilities=("hpc.submit", "hpc.result"),    # ),
    # 2026-10-07 入册(此前为孤儿:有代码+ACS 填报稿,未注册也未进名册)
    RosterMember(
        key="citation",
        name="研枢-Citation-Integrity-Partner",
        role="引用完整性核验:草稿逐条 [n] 引用 → 四档裁决+证据摘录(供审稿与引用对账交叉复核)",
        module="partner_citation",
        workspace="ws-citation",
        port_env="ACPS_CITATION_PORT",
        default_port=8468,
        declared_capabilities=("citation.verify",),
    ),
    RosterMember(
        key="supervisor",
        name="研枢-Supervisor",
        role="总控（本进程）：解析意图、按需调度上述伙伴、仲裁质量并汇总",
        module="supervisor",
        workspace="ws-supervisor",
        port_env="ACPS_SUPERVISOR_PORT",
        default_port=8444,
        kind="leader",
    ),
)


def _acs_final(workspace: str, cluster_dir: Path) -> dict | None:
    path = cluster_dir / workspace / "acps" / "acs.final.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _enrollment_aic(workspace: str, cluster_dir: Path) -> str | None:
    """从 `enrollment.json` 取本机身份 AIC。

    **本地铸造（未过注册表）的成员必须走这条路**：注册表会回填 ACS 的 `aic`/`active`
    /`lastModifiedTime`，而本地铸造时没有人回填——实测 2026-10-02 `ws-gpu`
    （`provenance=local-dev-ca`）的 `acs.final.json` 里 `aic=""`，身份只在
    `enrollment.json` 的 `identity.ref.aic`。只看 ACS 会让这类成员永远"未上线"。
    """
    path = cluster_dir / workspace / "acps" / "enrollment.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    aic = ((data.get("identity") or {}).get("ref") or {}).get("aic")
    return str(aic) if aic else None


def _acs_skills(acs: dict) -> tuple[str, ...]:
    return tuple(
        str(s.get("id"))
        for s in (acs.get("skills") or [])
        if isinstance(s, dict) and s.get("id")
    )


def _acs_endpoint(acs: dict) -> str | None:
    for endpoint in acs.get("endPoints") or []:
        if isinstance(endpoint, dict) and str(endpoint.get("transport") or "").upper() == "JSONRPC":
            url = str(endpoint.get("url") or "")
            if url:
                return url
    return None


def load_roster(cluster_dir: Path | None = None) -> list[RosterMember]:
    """解析身份 + **公开能力**；未上线的成员保留在名册里（aic=None），能力只剩登记的那部分。"""
    base = cluster_dir or CLUSTER_DIR
    resolved: list[RosterMember] = []
    for member in ROSTER:
        acs = _acs_final(member.workspace, base)
        if not acs:
            resolved.append(member)
            continue
        # 注册表回填的 ACS 里 aic 有值；本地铸造的没有（未过注册表）→ 回退读 enrollment
        aic = str(acs.get("aic") or "") or _enrollment_aic(member.workspace, base)
        resolved.append(
            replace(
                member,
                aic=aic,
                endpoint=_acs_endpoint(acs),
                public_capabilities=_acs_skills(acs),
            )
        )
    return resolved


def allow_discovered() -> bool:
    """名册外"已发现伙伴"兜底的开关(ACPS_ROSTER_ALLOW_DISCOVERED,默认开)。

    设 0/false/no/off 后 validate_plan 只认名册内能力——硬隔离模式
    (2026-10-06 审查:护栏此前依赖"平台发现 400 + 种子只写自家"两重巧合)。
    """
    import os

    return os.getenv("ACPS_ROSTER_ALLOW_DISCOVERED", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def load_discovered(
    cluster_dir: Path | None = None, *, workspace: str = DISCOVERY_WORKSPACE
) -> dict[str, DiscoveredAgent]:
    """读本机发现缓存（`<workspace>/acps/discovered/*.json`），返回 {aic: DiscoveredAgent}。

    缓存文件形如 `{"normalizedSummary": ..., "rawAcs": <完整 ACS>}`（wit 写的），能力直接
    从 `rawAcs.skills` 取——所以"名册外兜底"也能按能力校验，而不是只看名字。
    解析失败的条目跳过（缓存损坏不该让整次规划失败）。
    """
    base = cluster_dir or CLUSTER_DIR
    directory = base / workspace / "acps" / "discovered"
    found: dict[str, DiscoveredAgent] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        acs = record.get("rawAcs") if isinstance(record, dict) else None
        if not isinstance(acs, dict):
            continue
        aic = str(acs.get("aic") or "")
        if not aic:
            continue
        found[aic] = DiscoveredAgent(
            aic=aic,
            name=str(acs.get("name") or ""),
            skills=_acs_skills(acs),
            endpoint=_acs_endpoint(acs),
        )
    return found


def capability_index(roster: list[RosterMember]) -> dict[str, list[RosterMember]]:
    index: dict[str, list[RosterMember]] = {}
    for member in roster:
        for cap in member.capabilities:
            index.setdefault(cap, []).append(member)
    return index


# 仅测试使用（tests/test_cluster_logic.py 的名册选择样例）；生产委派走 supervisor 的
# roster/plan_gate 路径。2026-10-14 死代码审查定性：保留（随测试保活）。
def preferred_provider(roster: list[RosterMember], capability: str) -> RosterMember | None:
    """按偏好顺序（= ROSTER 顺序）返回该能力的首选**可用**提供者；没有则 None。"""
    for member in roster:
        if member.available and capability in member.capabilities:
            return member
    return None


def resolve_member(
    roster: list[RosterMember], *, partner: str | None = None, partner_aic: str | None = None
) -> RosterMember | None:
    """按 key / 展示名 / AIC 定位成员；定位不到返回 None（调用方据此判为编造）。"""
    wanted = [v.strip() for v in (partner, partner_aic) if v and v.strip()]
    if not wanted:
        return None
    for token in wanted:
        for member in roster:
            if token in (member.key, member.name) or (member.aic and token == member.aic):
                return member
    return None


def _resolve_discovered(
    discovered: dict[str, DiscoveredAgent],
    *,
    partner: str | None = None,
    partner_aic: str | None = None,
) -> DiscoveredAgent | None:
    wanted = [v.strip() for v in (partner, partner_aic) if v and v.strip()]
    for token in wanted:
        for agent in discovered.values():
            if token == agent.aic:
                return agent
            name = agent.name or ""
            if name and (token == name or (len(token) >= 3 and token in name)):
                return agent
    return None


def check_delegation_target(
    roster: list[RosterMember],
    *,
    partner_aic: str,
    capability: str | None = None,
    discovered: dict[str, DiscoveredAgent] | None = None,
    registered_aics: set[str] | None = None,
    reachable: object | None = None,
) -> tuple[bool, str]:
    """委派点的标量闸门：目标必须真实存在，能力必须真由其声明。

    这是 `validate_plan` 两条硬约束（不许编造伙伴、不许错派能力）的**逐步版本**
    （2026-10-16 审查：plan_gate 从硬纪律降为可选自检，拦截下沉到委派点）。入参与判定
    全是标量比较，所以模型提供方把 array 参数丢空时也不会卡住编排——那正是当天现网
    故障里 plan_gate 唯一失效的地方。

    返回 `(ok, 说明或拒绝原因)`。`partner_aic` 允许填名册 key、展示名或 AIC
    （`resolve_member` 本就三种都认），填错不会被误放行。

    路线 B 的可达性（2026-10-16 定案：走探活，不走 provenance 标记）：
    传 `registered_aics`（平台注册集合，来自 `registered_acs_overlay.json`）与
    `reachable`（`endpoint -> bool` 的探活回调）后，**不在该平台注册集合里**的成员
    （本地 dev 身份 + 反向隧道，如 gpu）会被实测端点；探不到就拒绝，不签发令牌。
    为什么需要：这类成员的 `available` 只表示"名册解析得出 AIC"，隧道没起来时它依然
    为真，放过去的结果是传输层 ConnectError + 账本里留下非终态垃圾行 + 白烧一轮
    （2026-10-16 13:34 现网实测）。
    两个"不探"的边界：`registered_aics` 为 None（覆盖表读不到）时一律不探——不能因为
    少一个配置文件就把所有委派卡住；平台注册成员也不探（存活由心跳/平台侧反映，且
    mTLS 握手本来就会暴露不可达）。
    """
    token = (partner_aic or "").strip()
    if not token:
        return False, "partner_aic 为空：只能派给名册或发现缓存里的伙伴"

    def _probe(aic: str, endpoint: str, label: str) -> str | None:
        """需要探活且探不到时返回拒绝原因；不需要探活或探得到返回 None。"""
        if reachable is None or registered_aics is None or aic in registered_aics:
            return None
        if not callable(reachable):  # 传了非回调 = 配置错误，按不探活处理（别卡住编排）
            return None
        if reachable(endpoint):
            return None
        return (f"{label} 的端点探活失败（{endpoint or '解析不出端点'}）：隧道/服务未就绪，"
                f"已拒绝委派且不签发令牌；请先起隧道，或改用同能力的其他伙伴")

    member = resolve_member(roster, partner_aic=token)
    if member is not None:
        # leader 自己也在名册里（kind="leader"、available=True、且 ACS 声明了
        # research.orchestrate），展示层（render_roster_text / callable_keys）会跳过它，
        # 但闸门不跳 → 模型可以委派给自己，每层再烧一个完整 run 预算，形成 runaway，
        # 且自调用不产生任何有用的互联量（2026-10-17 审查 P1-4，服务器名册实测可达）。
        if member.kind == "leader":
            return False, (f"{member.key} 是本集群的 leader（编排方自己），不能委派给自己；"
                           f"请改派给名册里的 partner，或直接在本次 run 内完成该步")
        if not member.available:
            return False, (f"{member.key} 尚未注册上线（名册里没有它的 AIC），不能派任务；"
                           f"请改用同能力的其他伙伴，或去掉该步并在汇总里说明该步缺失")
        if capability and capability not in member.capabilities:
            return False, (f"{member.key} 不提供能力 '{capability}'；它提供："
                           f"{', '.join(member.capabilities) or '（无）'}")
        blocked = _probe(str(member.aic or ""), str(member.endpoint or ""),
                         f"路线 B 成员 {member.key}")
        if blocked:
            return False, blocked
        return True, f"名册成员 {member.key}（AIC={member.aic}）"

    if not allow_discovered():
        return False, (f"AIC={token} 不在名册里（硬隔离模式 ACPS_ROSTER_ALLOW_DISCOVERED=0，"
                       f"发现缓存不参与放行）")

    agent = _resolve_discovered(discovered or {}, partner_aic=token)
    if agent is None:
        return False, (f"AIC={token} 既不在名册也不在发现缓存里——不要编造伙伴；"
                       f"先用 acps_discover 找到候选伙伴，再委派")
    if capability and capability not in agent.skills:
        return False, (f"已发现伙伴 {agent.name or token} 的缓存声明里没有能力 '{capability}'；"
                       f"已声明：{', '.join(agent.skills) or '（无）'}")
    blocked = _probe(agent.aic, str(agent.endpoint or ""),
                     f"发现缓存成员 {agent.name or agent.aic}")
    if blocked:
        return False, blocked
    return True, f"发现缓存成员 {agent.name or token}"


def render_roster_text(roster: list[RosterMember]) -> str:
    """把名册渲染成注入 supervisor 上下文的文本。"""
    lines = [
        "【本集群伙伴名册】（代码生成，身份以此为准；不要自行编造 AIC）",
        "顺序即偏好：同一能力有多个提供者时，优先用列表里靠前的成员。",
    ]
    for member in roster:
        if member.kind == "leader":
            continue
        caps = ", ".join(member.capabilities) or "-"
        if member.available:
            lines.append(
                f"- {member.key}（{member.name}）职责：{member.role}\n"
                f"    能力：{caps}\n"
                f"    身份：{member.aic}"
            )
        else:
            lines.append(
                f"- {member.key}（{member.name}）职责：{member.role}\n"
                f"    预期能力：{caps}（尚未上线，不可调用）"
            )
    available = [m.key for m in roster if m.kind != "leader" and m.available]
    pending = [m.key for m in roster if m.kind != "leader" and not m.available]
    lines.append(f"当前可调用：{', '.join(available) or '（无）'}")
    if pending:
        lines.append(f"尚未上线（本次不要派任务给它）：{', '.join(pending)}")
    lines.append(
        "名册外的社区伙伴：先 acps_discover 找到候选，再把它的 AIC 写进计划交给 plan_gate 校验"
        "（plan_gate 会核对它确实声明了该能力）；只有名册与本机发现缓存里都没有的伙伴才禁止使用。"
    )
    return "\n".join(lines)


def validate_plan(
    plan: dict,
    roster: list[RosterMember],
    *,
    discovered: dict[str, DiscoveredAgent] | None = None,
) -> dict:
    """代码侧校验模型的调度计划。

    计划形如::

        {"needed_capabilities": ["arxiv.search", "paper.extract"],
         "steps": [{"capability": "arxiv.search", "partner": "scout", "purpose": "..."}]}

    `partner` 可用成员 key / 展示名；也接受 `partner_aic`。名册里没有的伙伴，若出现在
    `discovered`（本机发现缓存）里且其 ACS 声明了该能力，同样放行（`source="discovered"`）。

    返回 ``{"ok": bool, "errors": [...], "resolved_steps": [...]}``，其中 resolved_steps 已把
    成员归一化成权威 AIC（并标注来源），supervisor 应只使用它派发。
    """
    errors: list[str] = []
    resolved_steps: list[dict] = []
    index = capability_index(roster)
    outside = discovered or {} if allow_discovered() else {}
    outside_caps = {cap for agent in outside.values() for cap in agent.skills}
    usable_caps = sorted(set(index) | outside_caps)
    callable_keys = ", ".join(m.key for m in roster if m.kind != "leader" and m.available)

    if not isinstance(plan, dict):
        return {"ok": False, "errors": ["计划不是 JSON 对象"], "resolved_steps": []}

    needs = as_list(plan.get("needed_capabilities"))
    if not isinstance(needs, list) or not needs:
        errors.append("needed_capabilities 必须是非空数组（列出本任务需要的能力 id）")
        needs = []
    needs = [str(n).strip() for n in needs if str(n).strip()]

    unknown = [n for n in needs if n not in index and n not in outside_caps]
    if unknown:
        errors.append(
            f"名册与本机发现缓存里都没有这些能力：{', '.join(unknown)}；"
            f"可用能力：{', '.join(usable_caps)}"
        )

    steps = as_list(plan.get("steps"))
    if not isinstance(steps, list) or not steps:
        errors.append("steps 必须是非空数组")
        steps = []

    covered: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"steps[{i}] 不是对象")
            continue
        cap = str(step.get("capability") or "").strip()
        if cap not in index and cap not in outside_caps:
            errors.append(
                f"steps[{i}].capability='{cap or '(空)'}' 不在名册/发现缓存的能力里；"
                f"可用能力：{', '.join(usable_caps)}"
            )
            continue

        member = resolve_member(
            roster, partner=step.get("partner"), partner_aic=step.get("partner_aic")
        )
        if member is not None:
            if cap not in member.capabilities:
                errors.append(
                    f"steps[{i}] 把能力 '{cap}' 派给了 {member.key}，但它只提供 "
                    f"{', '.join(member.capabilities) or '（无）'}"
                )
                continue
            if not member.available:
                errors.append(
                    f"steps[{i}] 的伙伴 {member.key} 尚未注册上线，不能派任务；"
                    f"请在计划里改用同能力的其他伙伴，或去掉该步并在汇总里说明该步缺失（不得由 leader 代写）"
                )
                continue
            covered.add(cap)
            resolved_steps.append(
                {
                    "capability": cap,
                    "partner": member.key,
                    "partner_aic": member.aic,
                    "endpoint": member.endpoint,
                    "purpose": str(step.get("purpose") or ""),
                    "source": "roster",
                }
            )
            continue

        agent = _resolve_discovered(
            outside, partner=step.get("partner"), partner_aic=step.get("partner_aic")
        )
        if agent is None:
            errors.append(
                f"steps[{i}] 指定的伙伴 {step.get('partner') or step.get('partner_aic')!r} "
                f"既不在名册、也不在本机发现缓存里（禁止编造伙伴）；名册可调用：{callable_keys}"
            )
            continue
        if cap not in agent.skills:
            errors.append(
                f"steps[{i}] 把能力 '{cap}' 派给了名册外伙伴 {agent.name or agent.aic}，"
                f"但它的 ACS 只声明 {', '.join(agent.skills) or '（无公开能力）'}"
            )
            continue
        covered.add(cap)
        resolved_steps.append(
            {
                "capability": cap,
                "partner": agent.name or agent.aic,
                "partner_aic": agent.aic,
                "endpoint": agent.endpoint,
                "purpose": str(step.get("purpose") or ""),
                "source": "discovered",
            }
        )

    missing = [n for n in needs if (n in index or n in outside_caps) and n not in covered]
    if missing:
        errors.append(f"这些能力被声明需要，但没有任何可执行的步骤覆盖：{', '.join(missing)}")

    return {"ok": not errors, "errors": errors, "resolved_steps": resolved_steps}
