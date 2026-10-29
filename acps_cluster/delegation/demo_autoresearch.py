"""AutoResearch 闭环真实业务（A 系列）：证据驱动的假设迭代环。

链路（真实 LLM + 真实沙盒执行）：leader（auto_research 激活 + 证据账本打卡）
  → frontier.detect 产方向 → 构造可证伪假设 → exp.run（持委托 token 沙盒执行）
  → verdict 反馈 → 修正假设再实证 → summarize 汇总（成立/被否双池）。

控制成本：迭代 2 轮、每轮一次 exp.run（实验员内部走真 GPU 沙盒，实验本身是
确定性规则验证——数字可信且不烧 GPU 时长）。

    cd yanshu-AIC && python acps_cluster/delegation/demo_autoresearch.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import os  # noqa: E402
import json  # noqa: E402
import hashlib  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.core import bootstrap  # noqa: E402

from wit import (  # noqa: E402
    AcpsDirect, AcpsConfig, AgentCertificateRequest, AgentProviderProfile, AgentState,
    WitAgent, WitRuntime,
)
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

from acps_cluster.core import cluster_roster  # noqa: E402
from acps_cluster.runtime import eval_runner  # noqa: E402
from acps_cluster.agents import partner_experiment  # noqa: E402
from acps_cluster.agents import partner_eval  # noqa: E402
from acps_cluster.agents import partner_gpu  # noqa: E402
from acps_cluster.agents import partner_frontier  # noqa: E402
from acps_cluster.agents import supervisor  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks  # noqa: E402
from acps_cluster.delegation.leader_tools import DelegationLeaderHooks  # noqa: E402

SUPERVISOR_WS = CLUSTER / "ws-supervisor"
FRONTIER_WS = CLUSTER / "ws-frontier"
EXPERIMENT_WS = CLUSTER / "ws-experiment"
EXP_WS = EXPERIMENT_WS  # 别名（沿用 citation 脚本的命名）
GPU_WS = CLUSTER / "ws-gpu"
EVAL_WS = CLUSTER / "ws-eval"

# frontier 的身份不硬编码：取 ws-frontier 自己 enrollment 里的那枚（本机 dev CA 铸造，
# 即 PEP 日志里的 myAic）。原先写死的 MDY3T9... 其实是 **synthesis** 的 AIC
# （demo_pep.py / test_delegation.py 里都以 SYNTHESIS_AIC 用它），会被闸门判成
# "synthesis 不提供能力 frontier.detect"（2026-10-17 预检实测）。
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"

# 端口必须与各成员 `acs.final.json` 里登记的 JSONRPC 端点一致：委派闸门会对名册成员的
# 登记端点做 TCP 探活，wit 也校验"绑定端口 vs 登记端点"——换端口等于让名册说谎，
# 委派会被判"隧道/服务未就绪"直接拒发（2026-10-17 实测用 189xx 时即为此）。
PORT_FRONTIER = 8450
PORT_EXP = 8461
PORT_LEADER = 8444
PORT_GPU = 8460
PORT_EVAL = 8464

# 自研环专用基准 id：**不共用 `demo-cls`**——那个 id 会被同机其他会话按冒烟参数反复
# 重写（2026-10-18 实测两次把 eval-private/demo-cls 改成 5 行，独立评分当场错位作废）。
BENCH_ID = os.getenv("ACPS_AR_BENCH_ID", "demo-cls300sup")
# 判据阈值：任务书里的 ≥0.85，同时也是事后对账时的裁决线
TARGET_ACC = float(os.getenv("ACPS_AR_TARGET_ACC", "0.85"))
def ledger_file_for(stamp: str) -> Path:
    """账本按 run 分文件——固定名会让下一轮截断上一轮的证据（第 15 轮即如此）。"""
    return CLUSTER / "demo_run" / "autoresearch" / f"ledger-{stamp}.v1.jsonl"


def _aic_of(ws: Path) -> str:
    data = json.loads((ws / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    ref = (data.get("identity") or {}).get("ref") or {}
    return str(ref.get("aic") or "") if isinstance(ref, dict) else ""


def _acps_config(port: int) -> AcpsConfig:
    return AcpsConfig.custom(
        registry_url="https://localhost:9001",
        ca_url="https://localhost:9003",
        discovery_url="https://localhost:9005",
        public_endpoint=f"https://localhost:{port}/rpc",
        certificate=AgentCertificateRequest(dns=("localhost",)),
        provider=AgentProviderProfile(name="yanshu", organization="Wit", email="e@e.com", country_code="CN"),
        strict_interop=True,
    )


def prime_stub(cache_dir: Path, workspace: Path, aic: str, port: int) -> Path | None:
    """把某个本地伙伴实例写成发现缓存桩。

    端点必须改写成本次 demo 实际服务的端口：常驻 ACS 里登记的端口在换端口/换实例时对不上，
    而 wit 的路由只认发现缓存——`acps.discover` 在本机不可达时，没有桩就交不出任务
    （2026-10-18 第 16 轮：eval 有桩缺失，四条路由全部 `no discovered Direct JSON-RPC endpoint`）。
    """
    acs = json.loads((workspace / "acps" / "acs.final.json").read_text(encoding="utf-8"))
    acs["aic"] = aic
    for _e in acs.get("endPoints", []) or []:
        _e["url"] = f"https://localhost:{port}/rpc"
    return cache_discovered_agent(cache_dir=cache_dir,
                                  summary=normalize_discovered_agent(acs, ranking=1),
                                  raw_acs=acs)


def prime_gpu_stub(cache_dir: Path) -> Path | None:
    return prime_stub(cache_dir, GPU_WS, GPU_AIC, PORT_GPU)


def benchmark_pair_ok(published: Path | None = None, gold: Path | None = None) -> tuple[bool, str]:
    """容器所见特征（`datasets/<bench>/input.csv`）必须与标签同源特征逐字节一致。

    2026-10-18 实测：跑完后 `eval-private/demo-cls` 被同机其他会话改成 5 行，独立评分
    拿到错位标签、结论作废。锁 hash 让这种错位在评分**之前**就暴露。
    """
    pub = published or (CLUSTER / "datasets" / BENCH_ID / "input.csv")
    ref = gold or (CLUSTER / "eval-private" / BENCH_ID / "input.csv")
    if not pub.is_file() or not ref.is_file():
        return False, f"基准文件缺失（published={pub.name}, gold={ref.name}）"
    h1 = hashlib.sha256(pub.read_bytes()).hexdigest()
    h2 = hashlib.sha256(ref.read_bytes()).hexdigest()
    if h1 != h2:
        return False, f"两侧基准不一致 {h1[:12]} != {h2[:12]}（被改动或发布未同步）"
    return True, h1[:12]


def independent_eval(since: float) -> dict:
    """宿主侧独立核对：拿沙箱产物里的预测，对 eval-private 的真标签算准确率。

    `verdict` 出自实验伙伴的模型，属于自述；真标签只留在宿主侧。没有这一步，
    "实验跑通了"就只有伙伴自己一句话作依据。
    """
    consistent, info = benchmark_pair_ok()
    if not consistent:
        return {"error": f"基准未锁定，独立评分作废：{info}"}
    runs = CLUSTER / "ws-gpu" / "runs"
    candidates = sorted(
        (d for d in runs.glob("run-*") if d.stat().st_mtime >= since),
        key=lambda d: d.stat().st_mtime, reverse=True)
    for run in candidates:
        # 容器 CWD 就是 /workspace/output：代码写 `output/x.csv` 会落到 output/output/，
        # 只查一层会把一次真实成功的实验误判成"无产物"（2026-10-18 第 13 轮假阴性）。
        found = sorted((p for p in run.rglob("*")
                        if p.is_file() and p.parent.name == "output"
                        and p.name in ("predictions.csv", "predictions.json")),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not found:
            continue
        pred = found[0]
        if pred.suffix == ".json":
            values = [str(v).strip() for v in json.loads(pred.read_text(encoding="utf-8"))]
        else:
            values = [ln.strip() for ln in pred.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if values and not values[0].isdigit():
                values = values[1:]          # 容忍 label 表头
        try:
            return {"runId": run.name, "artifact": pred.relative_to(run).as_posix(),
                    "benchmarkSha256": info, "n_predictions": len(values),
                    "predictionsSha256": hashlib.sha256(pred.read_bytes()).hexdigest()[:12],
                    **eval_runner.score_accuracy(BENCH_ID, values)}
        except ValueError as exc:
            return {"runId": run.name, "error": f"独立评分不通过: {str(exc)[:120]}"}
    return {"error": "本轮沙箱产物里没有 predictions.csv/json，自述准确率无独立佐证"}


def ledger_audit(indep: dict, ledger_file: Path) -> dict:
    """跑完之后把落盘账本与宿主侧独立度量对账。

    伙伴的估计与留出真值可能只差 1.25pp，但**宣称**层面会出现"样本内达标 / 泛化未验证"
    甚至反向漏报（第 14 轮：宿主 0.9167 → 应 supported，账本却记 inconclusive）。
    对账让这种事必须被查出来，而不是靠读长文本碰运气。
    """
    if not ledger_file.is_file():
        return {"error": "无账本文件（ACPS_AR_LEDGER 未设）"}
    entries = [json.loads(ln) for ln in ledger_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    iters = [e for e in entries if e.get("event") == "iterate"]
    score = (indep or {}).get("score")
    audit = {"entries": len(entries), "iterations": len(iters), "hostScore": score,
             "threshold": TARGET_ACC, "mismatches": []}
    if score is None:
        audit["note"] = "无独立度量：只能登记账本规模，无法对账"
        return audit
    derived = "supported" if score >= TARGET_ACC else "refuted"
    for e in iters:
        if e.get("verdictSource") == "host-score":
            continue                      # 已经是代码裁决过的，不参与对账
        if e.get("verdict") != derived:
            audit["mismatches"].append({"iteration": e.get("iteration"),
                                        "claimed": e.get("verdict"), "byHost": derived,
                                        "runRef": e.get("runRef")})
    audit["note"] = "mismatches 为空 = 自述与独立度量一致"
    return audit


async def main() -> None:
    """命令行入口：解析参数并执行本模块的主流程。"""
    out = CLUSTER / "demo_run" / "autoresearch"
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="autoresearch-"))
    started = time.monotonic()
    started_wall = time.time()      # 沙箱目录按 mtime 筛"本轮新产生的"
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ledger_file = ledger_file_for(run_stamp)

    # ① 令牌通道走真路：exp.run 缺令牌 → awaiting-input → leader 现签 → continue 附带。
    #    原先这里预置一枚 600s 静态令牌：一枚令牌跨不过两轮迭代，第二次尝试起就在算力侧
    #    admission 被拒（重放/过期），2026-10-17 实测整环因此再无凭据可用。
    os.environ["ACPS_GPU_AIC"] = GPU_AIC

    runtimes = []
    try:
        # PEP 必须在**任何伙伴构造之前**打开：`DelegationTokenScrubHooks`（唯一把 leader
        # 下发的令牌写进桥/令牌文件的钩子）只挂在 pep_hooks 里。伙伴若在没有它的情况下
        # 起来，remedy-3 的令牌会被**静默吞掉**——不报错，只回 awaiting，
        # 2026-10-17 实测：exp 因此连吃三次"句柄仍未送达"。
        gpu_audit = out / "gpu-audit.jsonl"
        gpu_audit.write_text("", encoding="utf-8")
        os.environ["ACPS_PEP_ENABLED"] = "1"
        os.environ["ACPS_PEP_LEADER_AICS"] = _aic_of(SUPERVISOR_WS)
        os.environ["ACPS_PEP_AUDIT_PATH"] = str(gpu_audit)
        # 实验员一次任务内部有"两轮 codegen + 一次委派重试"，同一枚令牌会被同作用域重放；
        # 默认一次性登记会把合法重试判成 replay → 演示里放开到 2 次（跨作用域仍不放）。
        os.environ["ACPS_PEP_JTI_MAX_USES"] = "2"
        # 证据账本落盘（闸门在构造时读这个变量 → 必须在 build_run_config 之前设）
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        os.environ["ACPS_AR_LEDGER"] = str(ledger_file)

        # frontier 服务端（真实 LLM：OpenAlex 真实数据统计新兴方向）
        ws_f = tmp / "ws-frontier"
        shutil.copytree(FRONTIER_WS / "acps", ws_f / "acps")
        frontier_rt = WitRuntime(partner_frontier.build_frontier_agent(bootstrap.build_provider()),
                                 workspace=ws_f, acps=_acps_config(PORT_FRONTIER))
        await frontier_rt.serve_direct_partner(host="127.0.0.1", port=PORT_FRONTIER, amp=False)
        print(f"[ar] frontier :{PORT_FRONTIER} serving")
        runtimes.append(frontier_rt)

        # 实验员服务端（真实 LLM：生成实验代码，桥接持 token 直连 GPU 沙盒）
        ws_e = tmp / "ws-exp"
        shutil.copytree(EXPERIMENT_WS / "acps", ws_e / "acps")
        prime_gpu_stub(ws_e / "acps" / "discovered")
        exp_rt = WitRuntime(partner_experiment.build_experiment_agent(bootstrap.build_provider()),
                            workspace=ws_e, acps=_acps_config(PORT_EXP))
        await exp_rt.serve_direct_partner(host="127.0.0.1", port=PORT_EXP, amp=False)
        print(f"[ar] experiment :{PORT_EXP} serving")
        runtimes.append(exp_rt)

        # GPU 侧（实验员桥接直连的目标；PEP 认 leader+token 的环境已在 main() 顶部设好）
        ws_g = tmp / "ws-gpu"
        shutil.copytree(GPU_WS / "acps", ws_g / "acps")
        # gpu 必须带 provider：它的入站任务要靠自己的模型决定调 gpu.run。
        # 传 None 时远端在 8ms 内 `Agent execution failed.`（partner_gpu.py:220 的生产态是
        # 有 provider 的，None 只属于 ACPS_GPU_LOCAL_NO_MODEL 的显式降级模式）。
        gpu_rt = WitRuntime(partner_gpu.build_gpu_agent(bootstrap.build_provider()), workspace=ws_g,
                            acps=_acps_config(PORT_GPU))
        await gpu_rt.serve_direct_partner(host="127.0.0.1", port=PORT_GPU, amp=False)
        print(f"[ar] gpu :{PORT_GPU} serving")
        runtimes.append(gpu_rt)

        # 评测伙伴：`eval.submit` 是**独立评分方**——标签不出域，只回分数。
        # 有了它，leader 才能在打卡时带 host_score，让 verdict 由代码裁决而不是伙伴自述。
        ws_v = tmp / "ws-eval"
        shutil.copytree(EVAL_WS / "acps", ws_v / "acps")
        eval_rt = WitRuntime(partner_eval.build_eval_agent(bootstrap.build_provider()),
                             workspace=ws_v, acps=_acps_config(PORT_EVAL))
        await eval_rt.serve_direct_partner(host="127.0.0.1", port=PORT_EVAL, amp=False)
        print(f"[ar] eval :{PORT_EVAL} serving")
        runtimes.append(eval_rt)

        # leader（真剧本自研模式 + 证据账本工具）
        roster = cluster_roster.load_roster()
        roster_text = cluster_roster.render_roster_text(roster)
        eval_aic = _aic_of(EVAL_WS)   # 独立评分方：verdict 由它返回的分数 + 判据决定
        leader_ws = tmp / "ws-leader"
        shutil.copytree(SUPERVISOR_WS / "acps", leader_ws / "acps")
        cache = leader_ws / "acps" / "discovered"
        cache.mkdir(parents=True, exist_ok=True)
        prime_gpu_stub(cache)          # leader 自己探查数据时也要能寻到 demo 的 gpu 实例
        prime_stub(cache, EVAL_WS, _aic_of(EVAL_WS), PORT_EVAL)   # ②c 交评测要用
        # frontier 的本地 ACS：dev 身份无平台 ACS——按真实能力生成（临时工作区）
        frontier_aic = _aic_of(FRONTIER_WS)  # enrollment 里那枚，不再是 synthesis 的 AIC
        _f_ws = tmp / "ws-frontier-acs"
        shutil.copytree(FRONTIER_WS / "acps", _f_ws / "acps")
        _f_rt = WitRuntime(partner_frontier.build_frontier_agent(None), workspace=_f_ws,
                           acps=_acps_config(PORT_FRONTIER))
        _f_acs = _f_rt.acs_draft()
        _f_acs["aic"] = frontier_aic
        for _e in _f_acs.get("endPoints", []) or []:
            _e["url"] = f"https://localhost:{PORT_FRONTIER}/rpc"

        # frontier 与 experiment 的端点进 leader 发现缓存
        for acs_ws, port, aic, acs_src in [(FRONTIER_WS, PORT_FRONTIER, frontier_aic, _f_acs),
                                           (EXPERIMENT_WS, PORT_EXP, EXP_AIC,
                                            json.loads((EXP_WS / "acps" / "acs.final.json").read_text(encoding="utf-8")))]:
            acs = dict(acs_src)
            acs["aic"] = aic
            for e in acs.get("endPoints", []) or []:
                e["url"] = f"https://localhost:{port}/rpc"
            cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(acs, ranking=1),
                                   raw_acs=acs)
        leader = WitRuntime(
            WitAgent(
                id="yanshu.autoresearch-leader", display_name="研枢-AutoResearch-Leader",
                version="1.0.0", description="自研模式 leader（真剧本）",
                instructions=partner_experiment_note(roster_text),
                model=bootstrap.build_provider(), capabilities=[],
                hooks=[AipDelegationHooks(), BridgeHooks(),
                       DelegationLeaderHooks(workspace=SUPERVISOR_WS)],
                protocols=[AcpsDirect()],
            ),
            workspace=leader_ws, acps=_acps_config(PORT_LEADER),
        )
        # 实验员的 GPU 令牌（leader 的 delegate_continue 或代码侧注入——演示用代码侧直备）
        print(f"[ar] leader :{PORT_LEADER} 下发自研任务（迭代≤2）")

        leader_state = AgentState()
        result = await leader.run(
            f"【自研任务·训练分类器】目标：在 {BENCH_ID} 二分类基准上训练一个测试准确率 ≥0.85 的"
            "小型分类器并交付模型参数。\n"
            "① 调 auto_research 激活自研模式；\n"
            "② 用 acps_delegate_public（partner_aic=实验员 AIC、skill_id=exp.run，见名册与发现缓存）"
            "发实验任务："
            f"question=在 {BENCH_ID} 两维特征上训练线性分类器并报告测试准确率；method=决策边界近似"
            " x0+x1=0，可加偏置拟合；代码把模型权重写进 weights.json 产物；"
            f"dataset={BENCH_ID}（真基准，容器内只读 `/data/{BENCH_ID}/`：`train.csv` 是 240 行"
            "**含 label 列**的训练集，用来真正监督拟合；`input.csv` 是 60 行**只有特征**的测试集，"
            "标签不在容器内）；除 weights.json 外**必须**再写 `predictions.csv`——对 input.csv "
            "**每一行**按顺序输出 0/1 预测（可带 label 表头），宿主侧用独立保存的测试标签评分；"
            f"读不到 /data/{BENCH_ID} 就如实报错，**不得改用合成数据顶替**；"
            "timeout_seconds=600（沙盒执行预算，实验员的轮询时长按它推导，**别填 1800**）；"
            "**不要传 compute_agent**：本次只有 gpu 沙盒在服务，cpu 节点没起，选它只会等到超时；"
            "委派后用 wait_partner(task_ref=...) 阻塞等到终态，不要用 acps_task 轮询；\n"
            "②b 若等待返回 state=awaiting-input（实验员缺 GPU 令牌）：按 remedy-3 现签——"
            "issue_delegation_token(delegate=实验员 AIC, skill_id=gpu.run, ttl_seconds=600) 取回 jti，"
            "再 delegate_continue(task_ref=<该任务>, jti=<该句柄>)。"
            "**ttl_seconds 必须显式给 600**：默认 300s 比一次代码生成（实测约 157s/轮、最多两轮）还短，"
            "令牌会在实验员来得及委派之前就过期。**每一轮都签新的一枚**：复用上一次的 "
            "jti 会被算力侧判成重放；\n"
            f"②c 实验交付的 predictions（按 {BENCH_ID} 的 input.csv 行序的 0/1 数组）**必须**交评测伙伴"
            f"独立评分：从实验员返回的 `artifactContents[\"predictions.csv\"]` **逐字**取出那 60 个"
            f" 0/1（对照 `artifactSha256[\"predictions.csv\"]` 自检；对不上就重取，**禁止自己转写或"
            f"凭记忆重建**），再 acps_delegate(partner_aic=\"{eval_aic}\", "
            f"skill_id=eval.submit, request 内写明 mode=predictions、benchmark={BENCH_ID}、"
            "predictions=[...])——标签不出评测域，它只回分数；\n"
            f"②d 用 eval 返回的分数打卡：auto_research(action='iterate', ..., "
            f"host_score=<eval 的 score>, threshold={TARGET_ACC})。此时 **verdict 由代码按判据裁决**，"
            "你不许自行宣布达标，也不得把沙盒内的样本内指标当泛化证据；\n"
            "③ 拿到实验报告后调 auto_research(action='iterate', hypothesis='线性模型可达标', "
            "run_ref=<实验taskRef>, verdict=<报告verdict>) 登记证据；\n"
            "④ 若准确率 <0.85：用 revision_from/to 修正假设（如改损失/特征标准化），再发一轮 "
            "exp.run；\n"
            "⑤ action='summarize' 收尾，输出生效的模型方案与证据链。",
            supervisor.build_run_config(roster),
        )
        elapsed = time.monotonic() - started
        final = str(result.final_text or "")
        # 消息流留档（审计）：leader 每步做了什么
        dump = [{"role": getattr(m, "role", "?"),
                 "parts": [str(getattr(p, "text", "") or "")[:100000]
                           for p in (getattr(m, "content", []) or [])]}
                for m in (result.messages or [])]
        (out / "leader-messages-dump.json").write_text(
            json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
        indep = independent_eval(started_wall)
        summary = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "task": "证据驱动假设迭代环（frontier→exp→verdict→修正）",
            "wallSeconds": round(elapsed, 1),
            "finalAnswerChars": len(final.encode("utf-8")),
            "runId": run_stamp,
            "ledgerFile": ledger_file.name,
            "runStatus": str(getattr(result, "status", "") or ""),
            "runSteps": getattr(result, "iterations", None),
            "independentEval": indep,
            "ledgerAudit": ledger_audit(indep, ledger_file),
            "mode": "auto-research（真实 LLM 迭代环）",
        }
        (out / "autoresearch-summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (out / "final-answer.md").write_text(final, encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        print(f"[ar] 产物 {out}")
    finally:
        for rt in runtimes:
            await rt.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)


def partner_experiment_note(roster_text: str) -> str:
    """真 supervisor 剧本（自研模式附加段在内）+ 演示名册。"""
    from acps_cluster.agents.supervisor import _playbook

    return _playbook(roster_text, ["frontier", "experiment", "gpu"])


if __name__ == "__main__":
    asyncio.run(main())
