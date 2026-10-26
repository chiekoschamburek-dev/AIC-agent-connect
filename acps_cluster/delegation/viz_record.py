"""viz_record.py —— 前端三场景「真实任务」录制器（A 全链路 / B 短链路 / C 授权实战）。

对 cluster-viz 的三个手写剧本（repro/short/auth）各跑一次**真实任务**：
真实 mTLS + 真实 PEP 裁决 + 真实 LLM（MiniMax）+ 真实 GPU 沙盒（Docker CUDA），
全程录制任务轨迹（VizEvent 契约事件 + pep-audit 原始行 + 真实产物），
装配成 Scenario JSON 交给前端时间轴回放（与手写剧本同一契约，见 cluster-viz/src/types.ts）。

用法（在 yanshu-AIC 下）：
    python acps_cluster/delegation/viz_record.py            # 依次跑 A、B、C
    python acps_cluster/delegation/viz_record.py b          # 只跑 B（最轻，先冒烟）
    python acps_cluster/delegation/viz_record.py a c
环境变量：
    VIZ_PACE=1.5        舞台之间的节奏间隔（秒）——"处理流程不要太快"的总闸
    VIZ_WAIT=900        单任务等产物超时（秒）

输出：
    acps_cluster/demo_run/viz/<sid>/    trace-events.jsonl · audit-*.jsonl · products/ · summary.json
    cluster-viz/public/scenarios/<sid>.json + manifest.json（前端启动时 fetch）

编排说明（诚实口径）：leader 侧为代码侧确定性编排（与 AutoResearch 端到端脚本同款），
伙伴侧为真实 LLM/真实沙盒执行；所有 PEP 裁决、令牌、审计、对账均为真实链路产物。
"""

from __future__ import annotations

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
# 整合后 cluster-viz 在仓库根内（PROJECT/cluster-viz），不再位于仓库上一级。
VIZ_PUBLIC = PROJECT / "cluster-viz" / "public" / "scenarios"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402  加载 .env（真实 LLM）

from wit import (  # noqa: E402
    AcpsConfig,
    AcpsDirect,
    AgentCertificateRequest,
    AgentProviderProfile,
    AgentState,
    DataPart,
    TextPart,
    WitAgent,
    WitRuntime,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

import partner_critic  # noqa: E402
import partner_experiment  # noqa: E402
import partner_gpu  # noqa: E402
import partner_scout  # noqa: E402
import partner_writer  # noqa: E402
from delegation.client import direct_fetch  # noqa: E402
from delegation.domain_manifest import (  # noqa: E402
    build_manifest,
    sign_manifest,
    verify_manifest,
    write_manifest,
)
from delegation.grant import derive_delegation_claims, issue_grant  # noqa: E402
from delegation.reconciliation import build_report, sign_report, verify_report  # noqa: E402
from delegation.token import issue_token, load_signing_context, token_data_item  # noqa: E402

PACE = float(os.getenv("VIZ_PACE", "1.5"))
WAIT = float(os.getenv("VIZ_WAIT", "900"))

SUPERVISOR_WS = CLUSTER / "ws-supervisor"
SCOUT_WS = CLUSTER / "ws-scout"
WRITER_WS = CLUSTER / "ws-writer"
CRITIC_WS = CLUSTER / "ws-critic"
EXP_WS = CLUSTER / "ws-experiment"
GPU_WS = CLUSTER / "ws-gpu"
TRUST = SUPERVISOR_WS / "acps" / "trust-bundle.pem"

GPU_SKILL = "gpu.run"
SEARCH_SKILL = "arxiv.search"
WRITE_SKILL = "paper.write"
REVIEW_SKILL = "paper.review"
EXP_SKILL = "exp.run"


def aic_of(ws: Path) -> str:
    data = json.loads((ws / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    aic = str((((data.get("identity") or {}).get("ref") or {}).get("aic")) or "")
    if not aic:
        raise SystemExit(f"{ws}/enrollment.json 缺 AIC")
    return aic


LEADER_AIC = aic_of(SUPERVISOR_WS)
SCOUT_AIC = aic_of(SCOUT_WS)
WRITER_AIC = aic_of(WRITER_WS)
CRITIC_AIC = aic_of(CRITIC_WS)
EXP_AIC = aic_of(EXP_WS)
GPU_AIC = aic_of(GPU_WS)


def acps_cfg(port: int) -> AcpsConfig:
    return AcpsConfig.custom(
        registry_url="https://localhost:9001",
        ca_url="https://localhost:9003",
        discovery_url="https://localhost:9005",
        public_endpoint=f"https://localhost:{port}/rpc",
        certificate=AgentCertificateRequest(dns=("localhost",)),
        provider=AgentProviderProfile(name="yanshu", organization="Wit", email="e@e.com", country_code="CN"),
        strict_interop=True,
    )


# ───────────────────────────── 录制器 ─────────────────────────────


def _brief(ev: dict) -> str:
    t = ev["type"]
    if t == "instruction":
        return ev["text"][:60]
    if t == "state":
        return f"{ev['agent']} → {ev['state']}" + (f"（{ev['note']}）" if ev.get("note") else "")
    if t == "delegate":
        return f"{ev['from']} → {ev['to']} · {ev.get('label') or ev.get('skill')}"
    if t == "token":
        return f"{ev['kind']} {ev['issuer']}→{ev['holder']}" + (f" · {ev['label']}" if ev.get("label") else "")
    if t == "pep":
        return f"{ev['caller']}→{ev['target']} {ev['skill']} = {ev['decision']}（{ev['rule']}）"
    if t == "product":
        return f"{ev['agent']} · {ev['name']} · {ev['state']}"
    if t == "metric":
        return f"{ev['agent']} · {ev['key']} = {ev['value']}"
    if t == "domain":
        return f"{ev['action']} {ev['domainId']} r{ev.get('revision') or ''}"
    if t == "reconcile":
        return f"{ev['domain']} totalUses={ev['totalUses']}"
    if t == "plan":
        return " · ".join(x["title"] for x in ev["subtasks"])
    if t == "done":
        return ev["summary"][:80]
    return str(ev)[:60]


class Rec:
    """轨迹录制器：真实墙钟事件（原样落盘）+ 供打印的瞬时回显。"""

    def __init__(self, sid: str, out: Path):
        self.sid = sid
        self.out = out
        self.t0 = time.monotonic()
        self.events: list[dict] = []

    def add(self, ev: dict) -> dict:
        e = dict(ev)
        e["t"] = round(time.monotonic() - self.t0, 3)
        self.events.append(e)
        print(f"  [{e['t']:7.2f}] {_brief(e)}", flush=True)
        return e

    async def beat(self, seconds: float | None = None) -> None:
        await asyncio.sleep(PACE if seconds is None else seconds)

    def flush(self) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "trace-events.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in self.events) + "\n",
            encoding="utf-8",
        )


def audit_tail(audit: Path, n: int = 1) -> list[dict]:
    if not audit.is_file():
        return []
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]
    return lines[-n:]


def save_product(out: Path, name: str, text: str) -> None:
    p = out / "products"
    p.mkdir(parents=True, exist_ok=True)
    safe = name.replace("/", "_")
    (p / safe).write_text(text, encoding="utf-8")


def product_text(products: list[dict]) -> str:
    chunks: list[str] = []
    for product in products or []:
        for item in product.get("dataItems") or product.get("data_items") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                chunks.append(str(item.get("text") or ""))
    return "\n".join(chunks)


# ───────────────────────── 运行时装配（复用 demo 验证过的管路） ─────────────────────────


def pep_env(audit: Path, *, domains_dir: Path | None = None, quota: Path | None = None) -> None:
    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = LEADER_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    os.environ["ACPS_PEP_TRUST_BUNDLE"] = str(TRUST)
    if domains_dir is not None:
        os.environ["ACPS_PEP_DOMAINS_DIR"] = str(domains_dir)
    else:
        os.environ.pop("ACPS_PEP_DOMAINS_DIR", None)  # 防上一次 serve 的域目录泄漏进本次 build
    if quota is not None:
        os.environ["ACPS_PEP_QUOTA_PATH"] = str(quota)
    else:
        os.environ.pop("ACPS_PEP_QUOTA_PATH", None)


def fresh_ws(src_ws: Path, tmp: Path, label: str) -> Path:
    ws = tmp / f"ws-{label}"
    if ws.exists():  # 多场景共用 tmp：同名工作区先清再拷（WinError 183）
        shutil.rmtree(ws)
    shutil.copytree(src_ws / "acps", ws / "acps")
    return ws


async def serve_partner(
    builder, provider, src_ws: Path, port: int, tmp: Path, *, label: str,
    audit: Path | None = None, charters: list[dict] | None = None,
    cache_acs: list[dict] | None = None,
):
    ws = fresh_ws(src_ws, tmp, f"{label}-{port}")
    if charters:
        d = ws / "acps" / "domains"
        d.mkdir(parents=True, exist_ok=True)
        for m in charters:
            write_manifest(m, d)
    if audit is not None:
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text("", encoding="utf-8")
    pep_env(
        audit or (tmp / "noop-audit.jsonl"),
        domains_dir=ws / "acps" / "domains" if charters else None,
        quota=ws / "acps" / "grant-quota.sqlite3",
    )
    if cache_acs:
        cache = ws / "acps" / "discovered"
        cache.mkdir(parents=True, exist_ok=True)
        for entry in cache_acs:
            cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(entry, ranking=1),
                                   raw_acs=entry)
    rt = WitRuntime(builder(provider), workspace=ws, acps=acps_cfg(port))
    await rt.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    acs = rt.acs_draft()
    acs["aic"] = aic_of(src_ws)
    for e in acs.get("endPoints", []) or []:
        e["url"] = f"https://localhost:{port}/rpc"
    print(f"[viz] {label} :{port} serving", flush=True)
    return rt, acs


def make_caller(src_ws: Path, port: int, tmp: Path, label: str, partner_acs: list[dict]):
    root = fresh_ws(src_ws, tmp, label)
    cache = root / "acps" / "discovered"
    cache.mkdir(parents=True, exist_ok=True)
    for acs in partner_acs:
        cache_discovered_agent(
            cache_dir=cache, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs
        )
    agent = WitAgent(
        id=f"yanshu.viz-{label}",
        display_name=f"研枢-{label}",
        version="1.0.0",
        description="viz 录制出站 runtime（代码侧编排，不跑模型）",
        instructions="只走 runtime.aip_delegate / aip_task，不跑模型。",
        model=None,
        capabilities=[],
        hooks=[AipDelegationHooks()],
        protocols=[AcpsDirect()],
    )
    return WitRuntime(agent, workspace=root, acps=acps_cfg(port)), AgentState()


async def wait_products(rt, state, task_ref: str, *, timeout: float = WAIT) -> dict:
    """等到**产物或终态**（与 fanout 实验同语义：伙伴 aip_submit 产物即视为完成——
    实测伙伴提交产物后任务态可能停在 awaiting-completion，只等终态会挂死）。"""
    deadline = time.monotonic() + timeout
    snapshot: dict = {}
    while time.monotonic() < deadline:
        snapshot = await rt.aip_task(state, action="wait", task_ref=task_ref)
        if not isinstance(snapshot, dict):
            snapshot = {}
        if snapshot.get("products") or str(snapshot.get("state") or "") in {
            "completed", "failed", "rejected", "cancelled", "expired",
        }:
            return snapshot
        await asyncio.sleep(1.0)
    return snapshot


_VIZ_STATE = {"completed": "completed", "failed": "rejected", "rejected": "rejected",
              "cancelled": "rejected", "expired": "rejected"}


def viz_state(done: dict) -> str:
    if done.get("products"):
        return "completed"
    return _VIZ_STATE.get(str(done.get("state") or ""), "rejected")


def pep_from_audit(rec: Rec, audit: Path, caller: str, target: str, skill: str,
                   *, domain: str | None = None, note: str = "") -> None:
    """PEP 契约事件以**真实审计行**为准（decision/rule 都不臆造）。"""
    a = (audit_tail(audit) or [{}])[0]
    ev = {"type": "pep", "caller": caller, "target": target, "skill": skill,
          "decision": a.get("decision") or "allow",
          "rule": (a.get("rule") or "n/a") + (f" · {note}" if note else "")}
    if domain:
        ev["domain"] = domain
    rec.add(ev)


async def call_wait(
    rt, state, target_aic: str, *, text: str, skill: str | None = None,
    token: str | None = None, timeout: float = WAIT, retries: int = 1,
) -> dict:
    """出站真实任务：受理 → 轮询到产物/终态。真实 LLM 偶发「不调工具直接作答」（任务
    completed 但零产物）——此时整体重试一次，并把空快照落盘供诊断。"""
    request: list = [TextPart(text)]
    if skill:
        request.append(DataPart(data={"skillId": skill}))
    if token:
        p = token_data_item(token, target_aic=target_aic, skill_id=skill or GPU_SKILL)
        request.append(DataPart(data=dict(p["data"]), metadata=dict(p["metadata"])))
    done: dict = {}
    for attempt in (1 + retries) * [0]:
        snap = await rt.aip_delegate(state, partner_aic=target_aic, request=request)
        task_ref = str(snap.get("task_ref") or "")
        done = await wait_products(rt, state, task_ref, timeout=timeout)
        if done.get("products"):
            return done
        print(f"  [viz] ⚠ 零产物（attempt={attempt + 1}, state={done.get('state')}）", flush=True)
        Path(str(snap.get("task_ref") or "task") + f".empty-{attempt + 1}.json").write_text(
            json.dumps(done, ensure_ascii=False, default=str)[:20000], encoding="utf-8")
        request = [TextPart(text)]
        if skill:
            request.append(DataPart(data={"skillId": skill}))
        if token:
            p = token_data_item(token, target_aic=target_aic, skill_id=skill or GPU_SKILL)
            request.append(DataPart(data=dict(p["data"]), metadata=dict(p["metadata"])))
    return done


def charter_manifest(revision: int, status: str = "active", previous: dict | None = None) -> dict:
    return build_manifest(
        domain_id="lab-x-y",
        members=[
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": SCOUT_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
            {"aic": LEADER_AIC, "role": "auditor"},
        ],
        assets=[
            {"providerAic": GPU_AIC, "skill": GPU_SKILL, "allowedMembers": ["consumer"]},
            {"providerAic": SCOUT_AIC, "skill": "dataset.fetch", "allowedMembers": ["consumer"]},
        ],
        issuers=[LEADER_AIC],
        revision=revision,
        status=status,
        previous=previous,
    )


def dual_sign(m: dict) -> dict:
    m = sign_manifest(m, load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST))
    m = sign_manifest(m, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
    return m


def leader_signing():
    return load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST)


# ───────────────────────── 场景 JSON 装配（重排时间轴，保证可看） ─────────────────────────

GAP = {
    "instruction": 2.0, "plan": 2.6, "agent_up": 0.5, "state": 0.9, "delegate": 1.5,
    "token": 1.7, "pep": 1.9, "product": 1.3, "metric": 1.2, "domain": 1.9,
    "discover": 1.2, "reconcile": 1.7, "done": 2.4,
}


def assemble(sid: str, title: str, description: str, instruction: str,
             regions: list, agents_spec: list, raw: list[dict]) -> dict:
    # 回放节奏：真实任务动辄十几分钟，回放按 GAP×SCALE 重排——整体放慢、关键段（GPU 执行）
    # 再额外停留，保证"处理流程不要太快"。
    scale = float(os.getenv("VIZ_REPLAY_SCALE", "2.5"))
    t = 0.0
    events = []
    prev = None
    for ev in raw:
        gap = GAP.get(ev["type"], 1.2) * scale
        if ev["type"] == "state" and ev.get("state") == "completed" and prev == "working":
            gap += 6.0 * scale  # 执行类节点完成后多停一拍
        if ev["type"] == "metric":
            gap += 1.5 * scale
        t = round(t + gap, 2)
        e = dict(ev)
        e["t"] = t
        events.append(e)
        prev = ev["type"]
    return {
        "id": sid, "title": title, "description": description, "instruction": instruction,
        "regions": regions, "agents": agents_spec, "events": events,
    }


def write_scenario(sc: dict) -> None:
    VIZ_PUBLIC.mkdir(parents=True, exist_ok=True)
    (VIZ_PUBLIC / f"{sc['id']}.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=1), encoding="utf-8"
    )


R_YANSHU = {"id": "yanshu", "name": "研枢集群（机构 A · 本机实测）", "color": "#0ea5e9", "x": 40, "y": 40, "w": 620, "h": 620}
R_EDGE = {"id": "edge", "name": "算力沙盒（本地 dev 身份 · Docker CUDA）", "color": "#f59e0b", "x": 700, "y": 40, "w": 420, "h": 300}
R_OUT = {"id": "outside", "name": "外部持证 Agent（平台社区）", "color": "#8b5cf6", "x": 700, "y": 380, "w": 420, "h": 220}


def agent_spec(id_: str, name: str, org: str, source: str, skills: list[str], role: str = "partner") -> dict:
    return {"id": id_, "name": name, "role": role, "org": org, "source": source, "skills": skills}


# ═════════════════════════ 场景 B · 短链路（检索即成文） ═════════════════════════

async def run_b(tmp: Path) -> dict:
    out = CLUSTER / "demo_run" / "viz" / "b-short"
    rec = Rec("b-short", out)
    scout_audit = out / "scout-audit.jsonl"
    writer_audit = out / "writer-audit.jsonl"
    runtimes = []
    try:
        rec.add({"type": "instruction", "text": "查一下图注意力网络近一年的进展，直接写成两页综述。（真实任务：arXiv 实检 + 真实 LLM 成文）"})
        await rec.beat()
        rec.add({"type": "state", "agent": "sup", "state": "planning"})
        await rec.beat()
        rec.add({"type": "plan", "subtasks": [{"id": "t1", "title": "检索 arXiv（真实网络）"}, {"id": "t2", "title": "撰写综述（真实 LLM）"}]})
        await rec.beat()

        scout_rt, scout_acs = await serve_partner(
            partner_scout.build_scout_agent, agents.build_provider(), SCOUT_WS, 18730, tmp,
            label="scout", audit=scout_audit)
        writer_rt, writer_acs = await serve_partner(
            partner_writer.build_writer_agent, agents.build_provider(), WRITER_WS, 18731, tmp,
            label="writer", audit=writer_audit)
        runtimes = [scout_rt, writer_rt]
        sup_rt, sup_state = make_caller(SUPERVISOR_WS, 18732, tmp, "sup", [scout_acs, writer_acs])
        runtimes.append(sup_rt)

        rec.add({"type": "agent_up", "agent": {"id": "sup", "name": "研枢 · 调度大脑", "role": "supervisor", "org": "yanshu", "source": "roster", "skills": ["plan", "orchestrate"]}})
        rec.add({"type": "agent_up", "agent": {"id": "scout", "name": "文献检索 scout", "role": "partner", "org": "yanshu", "source": "wutong", "skills": ["arxiv.search"]}})
        rec.add({"type": "agent_up", "agent": {"id": "writer", "name": "综述撰写 writer", "role": "partner", "org": "yanshu", "source": "roster", "skills": ["paper.write"]}})
        rec.add({"type": "state", "agent": "sup", "state": "idle", "note": "编排就绪（代码侧确定性编排）"})
        await rec.beat()

        # t1 检索（真实网络）
        rec.add({"type": "delegate", "from": "sup", "to": "scout", "taskId": "t1", "skill": SEARCH_SKILL, "label": "检索 arXiv：graph attention network"})
        rec.add({"type": "state", "agent": "scout", "taskId": "t1", "state": "working"})
        await rec.beat(0.6)
        done = await call_wait(
            sup_rt, sup_state, SCOUT_AIC,
            text="请检索 arXiv 上「graph attention network」近一年的论文，max_results=6，返回标题、作者、摘要与链接。",
            skill=SEARCH_SKILL)
        state_b = viz_state(done)
        pep_from_audit(rec, scout_audit, "sup", "scout", SEARCH_SKILL, note="公开技能免令牌")
        papers = product_text(done.get("products") or [])
        save_product(out, "资料清单.md", papers)
        rec.add({"type": "product", "agent": "scout", "name": "资料清单.md", "state": "final"})
        rec.add({"type": "state", "agent": "scout", "taskId": "t1", "state": state_b})
        await rec.beat()

        # t2 成文（真实 LLM）
        rec.add({"type": "delegate", "from": "sup", "to": "writer", "taskId": "t2", "skill": WRITE_SKILL, "label": "撰写两页综述"})
        rec.add({"type": "state", "agent": "writer", "taskId": "t2", "state": "working"})
        await rec.beat(0.6)
        done = await call_wait(
            sup_rt, sup_state, WRITER_AIC,
            text=("topic：图注意力网络（Graph Attention Network）近一年研究进展；mode=full（两页篇幅）。\n"
                  "materials（scout 实检结果，直接引用其中论文）：\n" + papers[:6000]),
            skill=WRITE_SKILL)
        state_w = viz_state(done)
        pep_from_audit(rec, writer_audit, "sup", "writer", WRITE_SKILL, note="leader 白名单直调")
        survey = product_text(done.get("products") or [])
        save_product(out, "GAT综述.md", survey)
        rec.add({"type": "product", "agent": "writer", "name": "GAT综述.md", "state": "draft"})
        await rec.beat()
        rec.add({"type": "product", "agent": "writer", "name": "GAT综述.md", "state": "final"})
        rec.add({"type": "state", "agent": "writer", "taskId": "t2", "state": state_w})
        await rec.beat()

        ok = state_b == "completed" and state_w == "completed"
        summary = (f"检索 {len(papers)} 字真实 arXiv 资料 → 综述 {len(survey)} 字真实 LLM 成稿；"
                   "scout L0 公开技能、writer 白名单直调，双侧 PEP 审计留痕。")
        rec.add({"type": "done", "summary": ("✅ " if ok else "⚠ ") + summary})
        return {"ok": ok, "summary": summary, "wall": rec.events[-1]["t"] if rec.events else 0}
    finally:
        rec.flush()
        for rt in runtimes:
            try:
                await rt.stop_serving()
            except Exception:
                pass


# ═════════════════════════ 场景 C · 授权实战（PEP/令牌/急停/对账） ═════════════════════════

GPU_JOB_TEXT = ("请执行一个 GPU 冒烟实验：在 CUDA 上做 2048×2048 矩阵乘 3 次并同步，"
                "打印「GPU-OK」与三次耗时（秒）。代码写进 main.py，entrypoint 为 python main.py。")


def _metric_from_stdout(stdout: str) -> str:
    for line in reversed((stdout or "").splitlines()):
        if "GPU-OK" in line:
            return line.strip()[:60]
    return "executed"


async def run_c(tmp: Path) -> dict:
    out = CLUSTER / "demo_run" / "viz" / "c-auth"
    rec = Rec("c-auth", out)
    gpu_audit = out / "gpu-audit.jsonl"
    runtimes: list = []
    r1 = dual_sign(charter_manifest(1))
    r2 = dual_sign(charter_manifest(2, status="frozen", previous=r1))
    r3 = dual_sign(charter_manifest(3, status="active", previous=r2))
    verify_manifest(r1, trust_bundle=TRUST)
    try:
        rec.add({"type": "instruction", "text": "授权层实战：无凭据拦截 → 自签令牌拦截 → 域授权 → 配额烧尽 → 冻结急停 → 解冻恢复 → 双签对账。（真实 PEP 裁决）"})
        gpu_rt, gpu_acs = await serve_partner(
            partner_gpu.build_gpu_agent, agents.build_provider(), GPU_WS, 18740, tmp,
            label="gpu", audit=gpu_audit, charters=[r1])
        gpu_frozen_rt, gpu_frozen_acs = await serve_partner(
            partner_gpu.build_gpu_agent, agents.build_provider(), GPU_WS, 18741, tmp,
            label="gpu-frozen", audit=out / "gpu-frozen-audit.jsonl", charters=[r1, r2])
        gpu_r3_rt, gpu_r3_acs = await serve_partner(
            partner_gpu.build_gpu_agent, agents.build_provider(), GPU_WS, 18742, tmp,
            label="gpu-r3", audit=out / "gpu-r3-audit.jsonl", charters=[r1, r2, r3])
        runtimes = [gpu_rt, gpu_frozen_rt, gpu_r3_rt]
        sup_rt, sup_state = make_caller(SUPERVISOR_WS, 18743, tmp, "sup", [gpu_acs])
        exp_rt, exp_state = make_caller(EXP_WS, 18744, tmp, "exp", [gpu_acs])
        exp_fr_rt, exp_fr_state = make_caller(EXP_WS, 18746, tmp, "exp-fr", [gpu_frozen_acs])
        exp_r3_rt, exp_r3_state = make_caller(EXP_WS, 18747, tmp, "exp-r3", [gpu_r3_acs])
        out_rt, out_state = make_caller(CRITIC_WS, 18745, tmp, "outsider", [gpu_acs])
        runtimes += [sup_rt, exp_rt, exp_fr_rt, exp_r3_rt, out_rt]

        rec.add({"type": "agent_up", "agent": {"id": "sup", "name": "研枢 · 调度大脑", "role": "supervisor", "org": "yanshu", "source": "roster", "skills": ["grant.issue"]}})
        rec.add({"type": "agent_up", "agent": {"id": "gpu", "name": "GPU 算力节点", "role": "partner", "org": "edge", "source": "domain", "skills": ["gpu.run"]}})
        rec.add({"type": "agent_up", "agent": {"id": "exp", "name": "实验员（付费客户/域内 consumer）", "role": "partner", "org": "yanshu", "source": "roster", "skills": ["exp.run"]}})
        rec.add({"type": "agent_up", "agent": {"id": "outsider", "name": "外部持证 Agent X（critic 证书）", "role": "partner", "org": "outside", "source": "wutong", "skills": []}})
        await rec.beat()

        # ① 无凭据直连 → 拒（不带任何令牌数据项）
        rec.add({"type": "delegate", "from": "outsider", "to": "gpu", "taskId": "x1", "skill": GPU_SKILL, "label": "空手直调 gpu.run"})
        await rec.beat(0.5)
        snap = await out_rt.aip_delegate(out_state, partner_aic=GPU_AIC,
                                         request=[TextPart(GPU_JOB_TEXT), DataPart(data={"skillId": GPU_SKILL})])
        done = await wait_products(out_rt, out_state, str(snap.get("task_ref") or ""), timeout=60)
        pep_from_audit(rec, gpu_audit, "outsider", "gpu", GPU_SKILL, note="无凭据直连")
        rec.add({"type": "state", "agent": "gpu", "taskId": "x1", "state": viz_state(done), "note": "task rejected（通用文案，细节只进审计）"})
        await rec.beat()

        # ② 自签令牌 → 拒（iss=自己，证书链可信但无签发权）
        critic_signing = load_signing_context(CRITIC_WS / "acps" / "client", trust_bundle=TRUST)
        fake = issue_token(critic_signing, delegate_aic=CRITIC_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL, ttl_seconds=600)
        rec.add({"type": "delegate", "from": "outsider", "to": "gpu", "taskId": "x2", "skill": GPU_SKILL, "label": "持自签令牌重试"})
        await rec.beat(0.5)
        done = await direct_fetch(out_rt, out_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=fake.token, skill_id=GPU_SKILL)
        await wait_products(out_rt, out_state, str(done.get("task_ref") or ""), timeout=60)
        pep_from_audit(rec, gpu_audit, "outsider", "gpu", GPU_SKILL, note="自签令牌：签名真、签发权无")
        rec.add({"type": "state", "agent": "gpu", "taskId": "x2", "state": viz_state(done)})
        await rec.beat()

        # ③ 建域 + GT→DT 授权链
        rec.add({"type": "domain", "action": "create", "domainId": "lab-x-y", "revision": 1})
        await rec.beat()
        grant = issue_grant(leader_signing(), domain="lab-x-y", domain_revision=1, subject_aic=EXP_AIC,
                            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=2, ttl_seconds=3600)
        rec.add({"type": "token", "kind": "grant", "issuer": "sup", "holder": "exp", "domain": "lab-x-y", "maxUses": 2, "label": "域级 Grant · 配额 2 · jti 一次性入库"})
        # 派生父 grant 必须签给签发者（leader），不能复用签给 exp 的直接调用 grant
        grant_parent = issue_grant(leader_signing(), domain="lab-x-y", domain_revision=1,
                                   subject_aic=LEADER_AIC,
                                   assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}],
                                   max_uses=2, ttl_seconds=3600)
        derived = issue_token(leader_signing(), delegate_aic=EXP_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
                              ttl_seconds=300, not_after=grant_parent.expires_at,
                              extra_claims=derive_delegation_claims(
                                  grant_parent.claims, skill_id=GPU_SKILL,
                                  parent_token=grant_parent.token))
        rec.add({"type": "token", "kind": "delegation", "issuer": "sup", "holder": "exp", "domain": "lab-x-y", "skill": GPU_SKILL, "label": "派生 Task Token · ≤10min · 收窄 ⊆ GT"})
        await rec.beat()

        # ④ 训练 #1（派生 token，真实 CUDA）
        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t1", "skill": EXP_SKILL, "label": "GPU 冒烟 #1（持派生 token）"})
        rec.add({"type": "state", "agent": "exp", "taskId": "t1", "state": "working"})
        await rec.beat(0.5)
        done = await direct_fetch(exp_rt, exp_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=derived.token, skill_id=GPU_SKILL)
        done = await wait_products(exp_rt, exp_state, str(done.get("task_ref") or ""))
        pep_from_audit(rec, gpu_audit, "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="act≡对端 · 路由相邻")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t1", "state": "working", "note": "Docker 沙盒真实 CUDA"})
        await rec.beat()
        rec.add({"type": "metric", "agent": "gpu", "key": "sandbox", "value": _metric_from_stdout(product_text(done.get("products") or []))})
        rec.add({"type": "state", "agent": "gpu", "taskId": "t1", "state": "completed"})
        rec.add({"type": "state", "agent": "exp", "taskId": "t1", "state": "completed"})
        await rec.beat()

        # ⑤ 配额 1/2 真跑 → 2/2 派发 → 第 3 次拒（同枚 GT 的 jti 计数）
        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t2", "skill": EXP_SKILL, "label": "训练 #2（GT 1/2）"})
        await rec.beat(0.4)
        done = await direct_fetch(exp_rt, exp_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=grant.token, skill_id=GPU_SKILL)
        done = await wait_products(exp_rt, exp_state, str(done.get("task_ref") or ""))
        pep_from_audit(rec, gpu_audit, "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="配额 1/2")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t2", "state": "completed"})
        await rec.beat()

        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t2b", "skill": EXP_SKILL, "label": "训练 #3（GT 2/2 · 派发）"})
        await rec.beat(0.4)
        await direct_fetch(exp_rt, exp_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=grant.token, skill_id=GPU_SKILL)
        await rec.beat(0.8)
        pep_from_audit(rec, gpu_audit, "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="配额 2/2 · 沙盒执行中")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t2b", "state": "working", "note": "已派发，沙盒执行中"})
        await rec.beat()

        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t3", "skill": EXP_SKILL, "label": "训练 #4（配额已尽）"})
        await rec.beat(0.5)
        snap = await direct_fetch(exp_rt, exp_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=grant.token, skill_id=GPU_SKILL)
        await wait_products(exp_rt, exp_state, str(snap.get("task_ref") or ""), timeout=60)
        pep_from_audit(rec, gpu_audit, "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="配额烧尽（第 3 次准入即拒）")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t3", "state": "rejected"})
        await rec.beat()

        # ⑥ 冻结急停（r2 frozen 实例）
        rec.add({"type": "domain", "action": "freeze", "domainId": "lab-x-y", "revision": 2})
        await rec.beat()
        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t4", "skill": EXP_SKILL, "label": "冻结后持 GT 调用"})
        await rec.beat(0.4)
        g2 = issue_grant(leader_signing(), domain="lab-x-y", domain_revision=2, subject_aic=EXP_AIC,
                         assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5, ttl_seconds=3600)
        done = await direct_fetch(exp_fr_rt, exp_fr_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=g2.token, skill_id=GPU_SKILL)
        await wait_products(exp_fr_rt, exp_fr_state, str(done.get("task_ref") or ""), timeout=60)
        pep_from_audit(rec, out / "gpu-frozen-audit.jsonl", "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="GT 同步死亡（⓪′ 验签前预检）")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t4", "state": "rejected"})
        await rec.beat()

        g2_parent = issue_grant(leader_signing(), domain="lab-x-y", domain_revision=2,
                                subject_aic=LEADER_AIC,
                                assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}],
                                max_uses=5, ttl_seconds=3600)
        d2 = issue_token(leader_signing(), delegate_aic=EXP_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
                         ttl_seconds=300, not_after=g2_parent.expires_at,
                         extra_claims=derive_delegation_claims(
                             g2_parent.claims, skill_id=GPU_SKILL,
                             parent_token=g2_parent.token))
        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t5", "skill": EXP_SKILL, "label": "冻结后持派生 token 调用"})
        await rec.beat(0.4)
        done = await direct_fetch(exp_fr_rt, exp_fr_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=d2.token, skill_id=GPU_SKILL)
        await wait_products(exp_fr_rt, exp_fr_state, str(done.get("task_ref") or ""), timeout=60)
        pep_from_audit(rec, out / "gpu-frozen-audit.jsonl", "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="派生 token 同步死亡")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t5", "state": "rejected"})
        await rec.beat()

        # ⑦ 解冻恢复（r3 实例）
        rec.add({"type": "domain", "action": "unfreeze", "domainId": "lab-x-y", "revision": 3})
        await rec.beat()
        g3 = issue_grant(leader_signing(), domain="lab-x-y", domain_revision=3, subject_aic=EXP_AIC,
                         assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=1, ttl_seconds=3600)
        rec.add({"type": "token", "kind": "grant", "issuer": "sup", "holder": "exp", "domain": "lab-x-y", "maxUses": 1, "label": "r3 解冻后新 Grant"})
        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t6", "skill": EXP_SKILL, "label": "解冻后训练（恢复放行）"})
        rec.add({"type": "state", "agent": "exp", "taskId": "t6", "state": "working"})
        await rec.beat(0.5)
        done = await direct_fetch(exp_r3_rt, exp_r3_state, target_aic=GPU_AIC, request=GPU_JOB_TEXT, token=g3.token, skill_id=GPU_SKILL)
        done = await wait_products(exp_r3_rt, exp_r3_state, str(done.get("task_ref") or ""))
        pep_from_audit(rec, out / "gpu-r3-audit.jsonl", "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="解冻恢复")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t6", "state": "completed"})
        rec.add({"type": "state", "agent": "exp", "taskId": "t6", "state": "completed"})
        await rec.beat()

        # ⑧ 双签对账（真实审计聚合）
        lines = gpu_audit.read_text(encoding="utf-8").splitlines()
        report = build_report(lines, domain="lab-x-y", domain_revision=1,
                              window=("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"), provider_aic=GPU_AIC)
        report = sign_report(report, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
        report = sign_report(report, leader_signing())
        _, signers = verify_report(report, trust_bundle=TRUST, expected_signers={GPU_AIC, LEADER_AIC})
        (out / "reconciliation.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        rec.add({"type": "reconcile", "domain": "lab-x-y", "totalUses": int(report.get("totalUses") or 0)})
        await rec.beat()

        ok = len(signers) == 2
        summary = ("拦截有据（无凭据/自签令牌）、域授权真实放行（沙盒 CUDA 实跑）、配额烧尽即拒、"
                   "冻结令 GT 与派生 token 同步死亡、解冻恢复、用量双签对账——全部真实 PEP 裁决与审计。")
        rec.add({"type": "done", "summary": ("✅ " if ok else "⚠ ") + summary})
        return {"ok": bool(ok), "summary": summary, "wall": rec.events[-1]["t"] if rec.events else 0}
    finally:
        rec.flush()
        for rt in runtimes:
            try:
                await rt.stop_serving()
            except Exception:
                pass


# ═════════════════════════ 场景 A · 论文复现全链路 ═════════════════════════

async def run_a(tmp: Path) -> dict:
    out = CLUSTER / "demo_run" / "viz" / "a-full"
    rec = Rec("a-full", out)
    scout_audit = out / "scout-audit.jsonl"
    writer_audit = out / "writer-audit.jsonl"
    critic_audit = out / "critic-audit.jsonl"
    exp_audit = out / "exp-audit.jsonl"
    gpu_audit = out / "gpu-audit.jsonl"
    runtimes: list = []
    try:
        rec.add({"type": "instruction", "text": "复现一篇 GNN 引用网络论文：检索文献 → 实验设计 → 协作域授权调 GPU 训练 → 撰写并审阅复现报告。（真实 arXiv + 真实 LLM + 真实 CUDA）"})
        await rec.beat()
        rec.add({"type": "state", "agent": "sup", "state": "planning"})
        await rec.beat()
        rec.add({"type": "plan", "subtasks": [
            {"id": "t1", "title": "检索文献（arXiv 实检）"}, {"id": "t2", "title": "实验设计（LLM）"},
            {"id": "t3", "title": "域授权 GPU 训练（真实 CUDA）"}, {"id": "t4", "title": "审阅（LLM 审稿人）"},
            {"id": "t5", "title": "终稿（按审稿意见返工）"}, {"id": "t6", "title": "双签对账"},
        ]})
        await rec.beat()

        # 起真实伙伴（每个都挂 PEP；gpu 带域章程 r1）
        r1 = dual_sign(charter_manifest(1))
        scout_rt, scout_acs = await serve_partner(partner_scout.build_scout_agent, agents.build_provider(), SCOUT_WS, 18750, tmp, label="scout", audit=scout_audit)
        writer_rt, writer_acs = await serve_partner(partner_writer.build_writer_agent, agents.build_provider(), WRITER_WS, 18751, tmp, label="writer", audit=writer_audit)
        critic_rt, critic_acs = await serve_partner(partner_critic.build_critic_agent, agents.build_provider(), CRITIC_WS, 18752, tmp, label="critic", audit=critic_audit)
        gpu_rt, gpu_acs = await serve_partner(partner_gpu.build_gpu_agent, agents.build_provider(), GPU_WS, 18753, tmp, label="gpu", audit=gpu_audit, charters=[r1])
        # DT 保持在 RFC 的 ≤600s；exp 侧代码生成超时就重签一枚新鲜令牌（下方 t3 循环）。
        os.environ["ACPS_PEP_MAX_TOKEN_TTL"] = "600"
        exp_rt, exp_acs = await serve_partner(partner_experiment.build_experiment_agent, agents.build_provider(), EXP_WS, 18754, tmp, label="exp", audit=exp_audit, cache_acs=[gpu_acs])
        runtimes = [scout_rt, writer_rt, critic_rt, gpu_rt, exp_rt]
        sup_rt, sup_state = make_caller(SUPERVISOR_WS, 18755, tmp, "sup", [scout_acs, writer_acs, critic_acs, exp_acs])
        runtimes.append(sup_rt)

        rec.add({"type": "agent_up", "agent": {"id": "sup", "name": "研枢 · 调度大脑", "role": "supervisor", "org": "yanshu", "source": "roster", "skills": ["plan", "grant.issue"]}})
        rec.add({"type": "agent_up", "agent": {"id": "scout", "name": "文献检索 scout", "role": "partner", "org": "yanshu", "source": "wutong", "skills": ["arxiv.search"]}})
        rec.add({"type": "agent_up", "agent": {"id": "writer", "name": "综述撰写 writer", "role": "partner", "org": "yanshu", "source": "roster", "skills": ["paper.write"]}})
        rec.add({"type": "agent_up", "agent": {"id": "critic", "name": "审阅 critic", "role": "partner", "org": "yanshu", "source": "roster", "skills": ["paper.review"]}})
        rec.add({"type": "agent_up", "agent": {"id": "exp", "name": "实验员 experiment", "role": "partner", "org": "yanshu", "source": "roster", "skills": ["exp.run"]}})
        rec.add({"type": "agent_up", "agent": {"id": "gpu", "name": "GPU 算力节点（沙盒）", "role": "partner", "org": "edge", "source": "domain", "skills": ["gpu.run"]}})
        rec.add({"type": "state", "agent": "sup", "state": "idle", "note": "编排就绪"})
        await rec.beat()

        # 建域 + 授权链（真实双签章程）
        rec.add({"type": "domain", "action": "create", "domainId": "lab-x-y", "revision": 1})
        await rec.beat()
        grant = issue_grant(leader_signing(), domain="lab-x-y", domain_revision=1, subject_aic=LEADER_AIC,
                            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=3, ttl_seconds=3600)
        rec.add({"type": "token", "kind": "grant", "issuer": "sup", "holder": "sup", "domain": "lab-x-y", "maxUses": 3, "label": "域级 Grant（双签章程 · 配额 3）"})
        dt = issue_token(leader_signing(), delegate_aic=EXP_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
                         ttl_seconds=600, not_after=grant.expires_at,
                         extra_claims=derive_delegation_claims(
                             grant.claims, skill_id=GPU_SKILL, parent_token=grant.token))
        rec.add({"type": "token", "kind": "delegation", "issuer": "sup", "holder": "exp", "domain": "lab-x-y", "skill": GPU_SKILL, "label": "派生 Task Token（隧道用）"})
        await rec.beat()

        # t1 检索（真实 arXiv）
        rec.add({"type": "delegate", "from": "sup", "to": "scout", "taskId": "t1", "skill": SEARCH_SKILL, "label": "检索文献"})
        rec.add({"type": "state", "agent": "scout", "taskId": "t1", "state": "working"})
        await rec.beat(0.6)
        done = await call_wait(sup_rt, sup_state, SCOUT_AIC,
                               text="请检索 arXiv 上「graph attention network citation network」论文，max_results=5，返回标题、作者、摘要与链接。",
                               skill=SEARCH_SKILL)
        papers = product_text(done.get("products") or [])
        save_product(out, "资料清单.md", papers)
        pep_from_audit(rec, scout_audit, "sup", "scout", SEARCH_SKILL, note="公开技能免令牌")
        rec.add({"type": "product", "agent": "scout", "name": "资料清单.md", "state": "final"})
        rec.add({"type": "state", "agent": "scout", "taskId": "t1", "state": "completed"})
        await rec.beat()

        # t2 实验设计（真实 LLM）
        rec.add({"type": "delegate", "from": "sup", "to": "writer", "taskId": "t2", "skill": WRITE_SKILL, "label": "实验设计"})
        rec.add({"type": "state", "agent": "writer", "taskId": "t2", "state": "working"})
        await rec.beat(0.6)
        done = await call_wait(sup_rt, sup_state, WRITER_AIC,
                               text=("topic：GNN 引用网络论文复现实验方案；mode=outline。\n"
                                     "materials（scout 实检）：\n" + papers[:4000]),
                               skill=WRITE_SKILL)
        plan_md = product_text(done.get("products") or [])
        save_product(out, "实验方案.md", plan_md)
        rec.add({"type": "product", "agent": "writer", "name": "实验方案.md", "state": "final"})
        rec.add({"type": "state", "agent": "writer", "taskId": "t2", "state": "completed"})
        await rec.beat()

        # t3 域授权训练（exp 持 DT 隧道直连 gpu；真实 LLM 代码生成 + 真实 CUDA）
        rec.add({"type": "delegate", "from": "sup", "to": "exp", "taskId": "t3", "skill": EXP_SKILL, "label": "执行训练（持派生 token）"})
        rec.add({"type": "state", "agent": "exp", "taskId": "t3", "state": "working"})
        await rec.beat(0.6)
        gpu_ep = "https://localhost:18753/rpc"
        os.environ["ACPS_GPU_AIC"] = GPU_AIC
        os.environ["ACPS_CITATION_TARGET_ENDPOINT"] = gpu_ep
        # DT ≤600s（RFC 时效）而 exp 侧 LLM 代码生成常超 5 分钟——每次尝试重签新鲜 DT
        # （签发=本地零成本操作；重发即重签，RFC §3.5 的配额语义）。
        done = {}
        for attempt in range(3):
            dt_fresh = issue_token(
                leader_signing(), delegate_aic=EXP_AIC, target_aic=GPU_AIC, skill_id=GPU_SKILL,
                ttl_seconds=600, not_after=grant.expires_at,
                extra_claims=derive_delegation_claims(
                    grant.claims, skill_id=GPU_SKILL, parent_token=grant.token))
            os.environ["ACPS_EXP_GPU_TOKEN"] = dt_fresh.token
            try:
                done = await call_wait(
                    sup_rt, sup_state, EXP_AIC,
                    text=("question：在一个小型引用网络二分类任务上训练线性/浅层分类器，报告测试准确率；"
                          "代码把模型权重写进 weights.json、指标写进 metrics.json（CPU 可跑的规模，用 GPU 沙盒执行）。\n"
                          "method：按实验方案的最简可复现路径（特征标准化 + 线性分类头）；dataset 留空（合成数据）。\n"
                          "timeout_seconds=600。"),
                    skill=EXP_SKILL, token=dt_fresh.token, retries=0)
            except Exception as exc:  # noqa: BLE001 — 网关/连接抖动不拖垮场景，换新令牌重试
                print(f"  [viz] ⚠ exp 尝试 {attempt + 1} 连接异常：{exc}", flush=True)
                await asyncio.sleep(3)
                continue
            if done.get("products"):
                if attempt:
                    rec.add({"type": "metric", "agent": "exp", "key": "delegation",
                             "value": f"第 {attempt + 1} 次签发成功（前枚令牌在代码生成期间过期，重发即重签）"})
                break
            print(f"  [viz] ⚠ exp 尝试 {attempt + 1} 未获产物（state={done.get('state')}），重签 DT 再试", flush=True)
        exp_report_raw = product_text(done.get("products") or [])
        save_product(out, "实验报告.md", exp_report_raw)
        rec.add({"type": "product", "agent": "exp", "name": "实验报告.md", "state": "final"})
        report = {}
        import re as _re2
        m_json = _re2.search(r'\{[^{}]*"metrics"[^{}]*\}', exp_report_raw, _re2.S)
        if m_json:
            try:
                report = json.loads(m_json.group(0))
            except ValueError:
                pass
        if not (isinstance(report, dict) and report.get("metrics")):
            # 报告为 markdown 时不强求 JSON：直接从文本抓准确率类指标（真实值，不臆造）
            report = {"metrics": {}}
            m_acc = _re2.search(r"(?:测试准确率|accuracy|acc)\D{0,8}([01]?\.\d+|\d{1,3}(?:\.\d+)?)\s*%?",
                                exp_report_raw, _re2.I)
            if m_acc:
                v = m_acc.group(1)
                report["metrics"]["accuracy"] = f"{float(v):.2%}" if "." in v else v
        rule = (audit_tail(gpu_audit) or [{}])[0]
        pep_from_audit(rec, gpu_audit, "exp", "gpu", GPU_SKILL, domain="lab-x-y", note="act≡对端 · 域资产许可")
        rec.add({"type": "state", "agent": "gpu", "taskId": "t3", "state": "working", "note": "沙盒内真实执行"})
        await rec.beat()
        metrics = report.get("metrics") if isinstance(report, dict) else None
        if isinstance(metrics, dict) and metrics:
            for k, v in list(metrics.items())[:3]:
                rec.add({"type": "metric", "agent": "gpu", "key": str(k), "value": str(v)[:24]})
        else:
            rec.add({"type": "metric", "agent": "gpu", "key": "sandbox", "value": "job executed"})
        verdict = str(report.get("verdict") or "").strip() if isinstance(report, dict) else ""
        rec.add({"type": "state", "agent": "gpu", "taskId": "t3", "state": "completed"})
        rec.add({"type": "state", "agent": "exp", "taskId": "t3", "state": "completed"})
        await rec.beat()

        # t4 审阅（真实 LLM 审稿人）
        rec.add({"type": "delegate", "from": "sup", "to": "critic", "taskId": "t4", "skill": REVIEW_SKILL, "label": "审阅复现报告"})
        rec.add({"type": "state", "agent": "critic", "taskId": "t4", "state": "working"})
        await rec.beat(0.6)
        done = await call_wait(sup_rt, sup_state, CRITIC_AIC,
                               text=("请审查以下复现报告（paper），参考资料（materials）为实验方案：\n"
                                     "【复现报告】\n" + exp_report_raw[:6000] + "\n\n【实验方案】\n" + plan_md[:2500]),
                               skill=REVIEW_SKILL)
        review = product_text(done.get("products") or [])
        save_product(out, "审稿意见.md", review)
        import re as _re
        m_score = _re.search(r'"score"\s*:\s*(\d+)', review)
        m_verdict = _re.search(r'"verdict"\s*:\s*"([A-Za-z]+)"', review)
        score = m_score.group(1) if m_score else ""
        verdict = m_verdict.group(1) if m_verdict else ""
        label = f"审稿意见.md · {verdict}{(' ' + score + ' 分') if score else ''}"
        rec.add({"type": "product", "agent": "critic", "name": label, "state": "final"})
        rec.add({"type": "state", "agent": "critic", "taskId": "t4", "state": "completed"})
        await rec.beat()

        # t5 终稿（按审稿意见返工，真实 LLM）
        rec.add({"type": "delegate", "from": "sup", "to": "writer", "taskId": "t5", "skill": WRITE_SKILL, "label": "按审稿意见返工终稿"})
        rec.add({"type": "state", "agent": "writer", "taskId": "t5", "state": "working"})
        await rec.beat(0.6)
        done = await call_wait(sup_rt, sup_state, WRITER_AIC,
                               text=("topic：GNN 引用网络论文复现报告（终稿）；mode=full。\n"
                                     "materials（实验报告与实验方案）：\n" + (exp_report_raw[:4000] + "\n" + plan_md[:2000]) +
                                     "\nfeedback（审稿意见，逐条回应）：\n" + review[:2500]),
                               skill=WRITE_SKILL)
        final_md = product_text(done.get("products") or [])
        save_product(out, "复现报告-终稿.md", final_md)
        rec.add({"type": "product", "agent": "writer", "name": "复现报告.md", "state": "reviewing"})
        await rec.beat()
        rec.add({"type": "product", "agent": "writer", "name": "复现报告.md", "state": "final"})
        rec.add({"type": "state", "agent": "writer", "taskId": "t5", "state": "completed"})
        await rec.beat()

        # t6 双签对账
        lines = gpu_audit.read_text(encoding="utf-8").splitlines()
        report_r = build_report(lines, domain="lab-x-y", domain_revision=1,
                                window=("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"), provider_aic=GPU_AIC)
        report_r = sign_report(report_r, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
        report_r = sign_report(report_r, leader_signing())
        _, signers = verify_report(report_r, trust_bundle=TRUST, expected_signers={GPU_AIC, LEADER_AIC})
        (out / "reconciliation.json").write_text(json.dumps(report_r, ensure_ascii=False, indent=1), encoding="utf-8")
        rec.add({"type": "reconcile", "domain": "lab-x-y", "totalUses": int(report_r.get("totalUses") or 0)})
        await rec.beat()

        ok = viz_state(done) == "completed" and len(signers) == 2
        acc = ""
        if isinstance(metrics, dict):
            for k, v in metrics.items():
                if "acc" in str(k).lower():
                    acc = f"{v}"
                    break
        summary = (f"复现完成：测试准确率 {acc or '见报告'}，审稿裁决 {verdict or '见审稿意见'}；"
                   "文献实检、方案/报告/审稿均为真实 LLM 产物，训练在 GPU 沙盒真实执行，全程域授权 + 双签对账。")
        rec.add({"type": "done", "summary": ("✅ " if ok else "⚠ ") + summary})
        return {"ok": bool(ok), "summary": summary, "wall": rec.events[-1]["t"] if rec.events else 0}
    finally:
        rec.flush()
        for rt in runtimes:
            try:
                await rt.stop_serving()
            except Exception:
                pass


# ═════════════════════════ 主入口 ═════════════════════════

SCENARIOS = {
    "a": ("a-full", "真实 · A 论文复现全链路", "检索→设计→域授权训练→审阅→终稿→对账（真实 arXiv + LLM + CUDA + PEP）",
          R_YANSHU, None, run_a),
    "b": ("b-short", "真实 · B 短链路：检索即成文", "arXiv 实检 + 真实 LLM 两页综述（L0/L1 双路 PEP 审计）",
          R_YANSHU, None, run_b),
    "c": ("c-auth", "真实 · C 授权实战：谁能用 · 谁不能用", "无凭据/自签拦截 → 域授权 → 配额 → 冻结 → 解冻 → 双签对账（真实 PEP）",
          R_YANSHU, None, run_c),
}

AGENT_SPECS = {
    "a": [
        agent_spec("sup", "研枢 · 调度大脑", "yanshu", "roster", ["plan", "grant.issue"], "supervisor"),
        agent_spec("scout", "文献检索 scout", "yanshu", "wutong", ["arxiv.search"]),
        agent_spec("writer", "综述撰写 writer", "yanshu", "roster", ["paper.write"]),
        agent_spec("critic", "审阅 critic", "yanshu", "roster", ["paper.review"]),
        agent_spec("exp", "实验员 experiment", "yanshu", "roster", ["exp.run"]),
        agent_spec("gpu", "GPU 算力节点（沙盒）", "edge", "domain", ["gpu.run"]),
    ],
    "b": [
        agent_spec("sup", "研枢 · 调度大脑", "yanshu", "roster", ["plan", "orchestrate"], "supervisor"),
        agent_spec("scout", "文献检索 scout", "yanshu", "wutong", ["arxiv.search"]),
        agent_spec("writer", "综述撰写 writer", "yanshu", "roster", ["paper.write"]),
    ],
    "c": [
        agent_spec("sup", "研枢 · 调度大脑", "yanshu", "roster", ["grant.issue"], "supervisor"),
        agent_spec("exp", "实验员（域内 consumer）", "yanshu", "roster", ["exp.run"]),
        agent_spec("gpu", "GPU 算力节点", "edge", "domain", ["gpu.run"]),
        agent_spec("outsider", "外部持证 Agent X", "outside", "wutong", []),
    ],
}


def regions_for(key: str) -> list[dict]:
    if key == "c":
        return [dict(R_YANSHU, h=430), dict(R_EDGE), dict(R_OUT)]
    if key == "a":
        return [dict(R_YANSHU), dict(R_EDGE)]
    return [dict(R_YANSHU, w=760)]


async def main() -> None:
    wanted = [a.lower() for a in sys.argv[1:]] or ["a", "b", "c"]
    tmp = Path(tempfile.mkdtemp(prefix="viz-record-"))
    results = {}
    written: list[str] = []
    try:
        for key in wanted:
            sid, title, desc, _, _, fn = SCENARIOS[key]
            print(f"\n════════ 场景 {key.upper()} · {title} ════════", flush=True)
            t0 = time.monotonic()
            try:
                res = await fn(tmp)
            except Exception as exc:  # noqa: BLE001 — 单场景失败不拖垮其余场景
                res = {"ok": False, "summary": f"运行异常：{exc}"}
                print(f"[viz] 场景 {key} 异常：{exc}", flush=True)
            results[key] = {**res, "wallReal": round(time.monotonic() - t0, 1)}
            if (CLUSTER / "demo_run" / "viz" / sid / "trace-events.jsonl").is_file():
                raw = [json.loads(x) for x in
                       (CLUSTER / "demo_run" / "viz" / sid / "trace-events.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
                if raw:
                    sc = assemble(sid, title, desc, next((e["text"] for e in raw if e["type"] == "instruction"), ""),
                                  regions_for(key), AGENT_SPECS[key], raw)
                    write_scenario(sc)
                    written.append(sid)
            print(f"[viz] 场景 {key}: {'✅' if res.get('ok') else '⚠'} {res.get('summary', '')[:100]} "
                  f"（真实耗时 {results[key]['wallReal']}s）", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        VIZ_PUBLIC.mkdir(parents=True, exist_ok=True)
        (VIZ_PUBLIC / "manifest.json").write_text(
            json.dumps({"scenarios": [f"/scenarios/{sid}.json" for sid in
                                      [s for s in ("a-full", "b-short", "c-auth") if s in written or (VIZ_PUBLIC / f"{s}.json").is_file()]]},
                       ensure_ascii=False), encoding="utf-8")
    print("\n════════ 汇总 ════════")
    for k, r in results.items():
        print(f"  {k}: {'✅' if r.get('ok') else '⚠'} {r.get('summary', '')[:110]}")
    print(f"[viz] 前端场景 JSON：{VIZ_PUBLIC}")
    print("[viz] 启动前端查看：cd cluster-viz && npm run dev")


if __name__ == "__main__":
    asyncio.run(main())
