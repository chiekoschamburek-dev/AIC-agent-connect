"""Phase C：supervisor（自身 LLM 按剧本）驱动的论文复现全链路。

与 Phase A/B 的区别：编排者不再是脚本代码，而是**真实的 supervisor agent**——
它读任务后自己 plan_gate 规划、逐环委派、wait_partner 等待、product_gate 体检、
remedy-3 签发委托令牌（exp.run 返回 awaiting 时）、review_gate 审稿返工，直到汇总。

前置修补（相对 2026-10-02 上午状态）：
- supervisor 剧本新增「复现一篇论文」意图分支（gpu.env → 检索 → 萃取 → 选型 →
  exp.run → paper.write → paper.review）；
- ws-experiment 用 cluster_common.ensure_local_acs 生成终稿 ACS（否则名册
  available=False，plan_gate 拒绝 exp.run）。

用法（在 my-project 下）：python acps_cluster/repro_phase_c.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 必须在导入 cluster_common 之前：exp.run 长流水线 + LLM 慢生成（Phase B 实测）
os.environ.setdefault("ACPS_LLM_TIMEOUT", "600")
os.environ.setdefault("ACPS_TASK_TOOL_TIMEOUT", "2700")

from acps_cluster.core import bootstrap  # noqa: E402  导入即加载 .env
from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core import cluster_roster  # noqa: E402
from acps_cluster.agents import partner_critic  # noqa: E402
from acps_cluster.agents import partner_experiment  # noqa: E402
from acps_cluster.agents import partner_gpu  # noqa: E402
from acps_cluster.agents import partner_scout  # noqa: E402
from acps_cluster.agents import partner_synthesis  # noqa: E402
from acps_cluster.agents import partner_writer  # noqa: E402
from acps_cluster.agents import supervisor as supervisor_mod  # noqa: E402
from acps_cluster.delegation import demo_pep as dp  # noqa: E402  借用 _acps_config / SUPERVISOR_WS
from wit import WitRuntime  # noqa: E402
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402

OUT = CLUSTER / "demo_run" / "repro-phaseC"
PORTS = {
    "gpu": 18461,
    "scout": 18462,
    "synthesis": 18463,
    "writer": 18464,
    "critic": 18465,
    "experiment": 18466,
}
PEP_ON = ("gpu", "scout", "experiment")  # 与生产挂载一致；审计各落一份

TASK = (
    "复现一篇适合当前显卡训练的论文，并给出实验报告与审稿结论：\n"
    "① 调 gpu.env 拿本节点真实算力约束（显存/镜像白名单/沙盒限额/可用数据）；\n"
    "② scholar.deep_fetch 检索 5-8 篇候选论文（主题：卷积网络小模型从零训练、单卡短时可训）；\n"
    "③ paper.extract 逐篇萃取四要素；④ 你按算力约束选出唯一最适合的一篇，在计划里写明选型理由；\n"
    "⑤ 委派 exp.run 训练验证——机制类复现用合成数据：dataset 参数写明「代码内用 torch 固定种子"
    "合成、不读任何 CSV、不下载任何数据」，并把下面的参考实现原样放进 question（实验员会转写提交，"
    "timeout_seconds=900）；\n"
    "⑥ paper.write 根据实验过程与结果写实验报告（materials=实验报告原文+算力约束+选型理由，mode=full）；\n"
    "⑦ paper.review 审查实验报告的可行性与严谨性（materials 带上算力约束与实验原始指标），"
    "REVISE 就让 writer 带feedback 返工一轮，然后把最终报告与审稿结论一起汇总。\n"
    "注意：本任务不需要 dataset.fetch（数据全部代码内合成）；参考实现：\n"
    "```python\n"
    "import json, os, time\n"
    "import torch\n"
    "import torch.nn as nn\n"
    "import torch.nn.functional as F\n\n"
    "torch.manual_seed(0)\n"
    "device = 'cuda'\n"
    "os.makedirs('output', exist_ok=True)\n\n"
    "def make_data(n=512, size=32):\n"
    "    xs, ys = [], []\n"
    "    for i in range(n):\n"
    "        cls = i % 2\n"
    "        base = torch.zeros(1, size, size)\n"
    "        if cls == 0:\n"
    "            base[:, ::4, :] = 1.0\n"
    "        else:\n"
    "            base[:, :, ::4] = 1.0\n"
    "        xs.append(base + 0.3 * torch.randn(1, size, size))\n"
    "        ys.append(cls)\n"
    "    X = torch.stack(xs)\n"
    "    Y = torch.tensor(ys)\n"
    "    perm = torch.randperm(n)\n"
    "    return X[perm[:410]], Y[perm[:410]], X[perm[410:]], Y[perm[410:]]\n\n"
    "class SmallCNN(nn.Module):\n"
    "    def __init__(self):\n"
    "        super().__init__()\n"
    "        self.features = nn.Sequential(\n"
    "            nn.Conv2d(1, 8, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),\n"
    "            nn.Conv2d(8, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),\n"
    "            nn.AdaptiveAvgPool2d((4, 4)))\n"
    "        self.head = nn.Linear(16 * 4 * 4, 2)\n\n"
    "    def forward(self, x):\n"
    "        return self.head(self.features(x).flatten(1))\n\n"
    "def run_arm(name, schedule, Xtr, Ytr, Xva, Yva, epochs=12):\n"
    "    model = SmallCNN().to(device)\n"
    "    opt = torch.optim.Adam(model.parameters(), lr=1e-3)\n"
    "    torch.cuda.reset_peak_memory_stats()\n"
    "    t0 = time.time()\n"
    "    for ep in range(epochs):\n"
    "        size = schedule(ep)\n"
    "        x = Xtr if size == 32 else F.interpolate(Xtr, size=size, mode='nearest')\n"
    "        model.train()\n"
    "        opt.zero_grad()\n"
    "        loss = F.cross_entropy(model(x.to(device)), Ytr.to(device))\n"
    "        loss.backward()\n"
    "        opt.step()\n"
    "    model.eval()\n"
    "    with torch.no_grad():\n"
    "        acc = (model(Xva.to(device)).argmax(1).cpu() == Yva).float().mean().item()\n"
    "    return {'arm': name, 'final_val_acc': round(acc, 4),\n"
    "            'total_train_s': round(time.time() - t0, 2),\n"
    "            'per_epoch_s': round((time.time() - t0) / epochs, 3),\n"
    "            'peak_mem_mb': round(torch.cuda.max_memory_allocated() / 1e6, 1)}\n\n"
    "def main():\n"
    "    DATA = make_data()\n"
    "    results = [\n"
    "        run_arm('progressive', lambda ep: 8 if ep < 6 else 32, *DATA),\n"
    "        run_arm('fixed32', lambda ep: 32, *DATA),\n"
    "    ]\n"
    "    summary = {'hypothesis': 'ENTR: 初期低分辨率训练更高效', 'results': results}\n"
    "    print(json.dumps(summary, ensure_ascii=False))\n"
    "    with open('output/summary.json', 'w', encoding='utf-8') as f:\n"
    "        json.dump(summary, f, ensure_ascii=False, indent=2)\n\n"
    "main()\n"
    "```"
)


def _enrollment_aic(ws_name: str) -> str:
    data = json.loads((CLUSTER / ws_name / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    return data["identity"]["ref"]["aic"]


def _acs_entry(aic: str, name: str, skills: list[str], port: int) -> dict:
    template = json.loads(
        (CLUSTER / "ws-synthesis" / "acps" / "acs.final.json").read_text(encoding="utf-8")
    )
    template.update(
        aic=aic,
        name=name,
        skills=[{"id": s} for s in skills],
        endPoints=[
            {"transport": "JSONRPC", "url": f"https://localhost:{port}/rpc", "security": [{"mtls": []}]}
        ],
    )
    return template


def _seed_discovered(root: Path, entries: list[tuple[str, str, list[str], int, str]]) -> None:
    cache_dir = root / "acps" / "discovered"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for aic, name, skills, port, _key in entries:
        acs = _acs_entry(aic, name, skills, port)
        cache_discovered_agent(
            cache_dir=cache_dir, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs
        )


async def _prepare_experiment_acs() -> str:
    """给 ws-experiment 生成终稿 ACS（本地铸造身份没有注册表回填，名册靠它判可用）。"""
    ws = CLUSTER / "ws-experiment"
    provider = bootstrap.build_provider()
    try:
        runtime = WitRuntime(
            partner_experiment.build_experiment_agent(provider), workspace=ws, acps=dp._acps_config(PORTS["experiment"])
        )
        aic = cluster_common.ensure_local_acs(runtime, ws)
        print(f"[prepare] ws-experiment 终稿 ACS 就绪 aic={aic}", flush=True)
        return aic or _enrollment_aic("ws-experiment")
    finally:
        await provider.aclose()


async def _serve_partners(tmp: Path):
    builders = {
        "gpu": partner_gpu.build_gpu_agent,
        "scout": partner_scout.build_scout_agent,
        "synthesis": partner_synthesis.build_synthesis_agent,
        "writer": partner_writer.build_writer_agent,
        "critic": partner_critic.build_critic_agent,
        "experiment": partner_experiment.build_experiment_agent,
    }
    aics = {k: _enrollment_aic(f"ws-{k}") for k in PORTS}
    runtimes: dict[str, WitRuntime] = {}
    providers: dict[str, object] = {}
    for kind, port in PORTS.items():
        if kind in PEP_ON:
            audit = OUT / f"audit-{kind}.jsonl"
            audit.parent.mkdir(parents=True, exist_ok=True)
            audit.write_text("", encoding="utf-8")
            os.environ["ACPS_PEP_ENABLED"] = "1"
            os.environ["ACPS_PEP_LEADER_AICS"] = dp.LEADER_AIC
            os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
            if kind == "experiment":
                os.environ["ACPS_GPU_AIC"] = aics["gpu"]  # 令牌 env 兜底
        else:
            os.environ["ACPS_PEP_ENABLED"] = "0"
        root = tmp / f"ws-{kind}"
        shutil.copytree(CLUSTER / f"ws-{kind}" / "acps", root / "acps")
        if kind == "experiment":  # 出站直连 gpu 需要发现缓存
            _seed_discovered(
                root,
                [(aics["gpu"], "研枢-GPU-Partner", ["gpu.env", "gpu.run"], PORTS["gpu"], "gpu")],
            )
        providers[kind] = bootstrap.build_provider()
        runtimes[kind] = WitRuntime(
            builders[kind](providers[kind]), workspace=root, acps=dp._acps_config(port)
        )
        status = await runtimes[kind].serve_direct_partner(host="127.0.0.1", port=port, amp=False)
        print(f"[serve:{kind}] 端口 {port} → {status.status}", flush=True)
    return runtimes, providers, aics


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="repro-c-"))
    t0 = time.time()
    runtimes: dict[str, WitRuntime] = {}
    providers: dict[str, object] = {}
    supervisor_rt: WitRuntime | None = None
    sup_provider = None
    try:
        exp_aic = await _prepare_experiment_acs()
        runtimes, providers, aics = await _serve_partners(tmp)
        if aics["experiment"] != exp_aic:
            aics["experiment"] = exp_aic

        # supervisor 工作区（tmp 副本）+ 发现缓存预热 6 个伙伴
        sup_ws = tmp / "ws-supervisor"
        shutil.copytree(dp.SUPERVISOR_WS / "acps", sup_ws / "acps")
        _seed_discovered(
            sup_ws,
            [
                (aics["gpu"], "研枢-GPU-Partner", ["gpu.env", "gpu.status", "gpu.data.list", "gpu.data.fetch", "gpu.run"], PORTS["gpu"], "gpu"),
                (aics["scout"], "研枢-Scout-Partner", ["arxiv.search", "scholar.deep_fetch", "dataset.fetch"], PORTS["scout"], "scout"),
                (aics["synthesis"], "研枢-Synthesis-Partner", ["paper.extract"], PORTS["synthesis"], "synthesis"),
                (aics["writer"], "研枢-Writer-Partner", ["paper.write"], PORTS["writer"], "writer"),
                (aics["critic"], "研枢-Critic-Partner", ["paper.review"], PORTS["critic"], "critic"),
                (aics["experiment"], "研枢-Experiment-Partner", ["exp.run"], PORTS["experiment"], "experiment"),
            ],
        )
        roster = cluster_roster.load_roster()
        avail = {m.key: m.available for m in roster if m.kind != "leader"}
        print(f"[setup] 名册可用性 {avail}", flush=True)
        if not avail.get("experiment") or not avail.get("gpu"):
            raise SystemExit("名册里 gpu/experiment 不可调度，终止")

        sup_provider = bootstrap.build_provider()
        supervisor_rt = WitRuntime(
            supervisor_mod.build_supervisor_agent(sup_provider),
            workspace=sup_ws,
            acps=dp._acps_config(18499),
        )
        print(f"[setup] supervisor 就绪 leader={dp.LEADER_AIC}，投放复现任务…", flush=True)

        stop = asyncio.Event()

        async def heartbeat():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), 60)
                except asyncio.TimeoutError:
                    print(f"[heartbeat] 已运行 {time.time() - t0:.0f}s", flush=True)

        hb = asyncio.create_task(heartbeat())
        try:
            run = await supervisor_rt.run(TASK, supervisor_mod.build_run_config(roster))
        finally:
            stop.set()
            await hb

        final_text = run.final_text or ""
        print(f"\n[supervisor] status={run.status} steps={run.iterations} 总耗时={time.time() - t0:.0f}s", flush=True)
        (OUT / "report.md").write_text(final_text, encoding="utf-8")
        (OUT / "run-meta.json").write_text(
            json.dumps(
                {"status": str(run.status), "iterations": getattr(run, "iterations", None),
                 "total_s": round(time.time() - t0, 1), "roster_available": avail},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[done] 产物在 {OUT}", flush=True)
    finally:
        for rt in runtimes.values():
            try:
                await rt.stop_serving()
            except Exception:  # noqa: BLE001
                pass
        for p in providers.values():
            try:
                await p.aclose()
            except Exception:  # noqa: BLE001
                pass
        if supervisor_rt is not None:
            try:
                await supervisor_rt.stop_serving()
            except Exception:  # noqa: BLE001
                pass
        if sup_provider is not None:
            try:
                await sup_provider.aclose()
            except Exception:  # noqa: BLE001
                pass
        print("\n===== 审计轨迹 =====", flush=True)
        for kind in PEP_ON:
            path = OUT / f"audit-{kind}.jsonl"
            if not path.exists():
                continue
            print(f"[audit:{kind}]")
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    print(
                        f"  {r.get('decision', ''):5s} rule={r.get('rule', ''):18s} "
                        f"jti={(r.get('jti') or '-')[:10]}"
                    )
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
