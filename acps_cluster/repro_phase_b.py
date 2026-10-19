"""Phase B：论文复现真训练——ENTR 假设（Phase A 选型 #1），experiment 持委托令牌驱动 gpu 沙盒。

链路：leader 签发 gpu.run 委托令牌（jti 句柄）→ 令牌以 delegation dataItem 随任务下发
experiment（剥离子钩子按"搬运工"分支把 JWS 原文落 local_context，模型只见脱敏标记）→
exp.run 生成训练代码 → 持令牌直连 gpu.run（PEP L2）→ Docker 沙盒真训练 → 实验报告回传。

选型依据（Phase A S5）：ENTR 假设（arXiv 2018）"先用低分辨率训练再渐进放大更高效"——
合成数据即可机制性验证（两臂对比：渐进分辨率 vs 固定分辨率），完全契合沙盒断网约束。

用法（在 my-project 下）：python acps_cluster/repro_phase_b.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 必须在导入 cluster_common（经 partner_experiment）之前设置：
# exp.run 全流水线 = 代码生成 LLM + 沙盒训练 + 报告 LLM，远超 300s 默认工具超时；
# ACPS_LLM_TIMEOUT 在 llm_tools 每次调用时读取——LLM 网关生成完整训练脚本偏慢，
# 180s 默认连续两轮超时（实测），放宽到 600s/次。
os.environ.setdefault("ACPS_LLM_TIMEOUT", "600")
os.environ.setdefault("ACPS_TASK_TOOL_TIMEOUT", "2700")

import agents  # noqa: E402  导入即加载 .env
import partner_experiment  # noqa: E402
import partner_gpu  # noqa: E402
from delegation import demo_pep as dp  # noqa: E402  借用 _acps_config / SUPERVISOR_WS（自带 sys.path 卫生）
from delegation import issue_token, load_signing_context, token_data_item  # noqa: E402
from wit import AcpsDirect, AgentState, DataPart, TextPart, WitAgent, WitRuntime  # noqa: E402
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

TERMINAL = {"completed", "failed", "canceled", "rejected"}
OUT = CLUSTER / "demo_run" / "repro-phaseB"
PORT_GPU = 18461
PORT_EXP = 18466
PORT_CALLER = 18499

GPU_SKILL = "gpu.run"
JOB_TIMEOUT = 900  # 沙盒训练时限（JobSpec 上限 1800）

QUESTION = (
    "复现论文《Testing the Efficient Network TRaining (ENTR) Hypothesis》(arXiv 2018) 的核心假设："
    "训练初期用低分辨率图像可以提升 CNN 训练效率。两臂对照（同一小 CNN、同一合成数据、同一 12 epoch 预算）："
    "A 臂渐进分辨率（前 6 epoch 用 8x8，后 6 epoch 用 32x32）；B 臂固定 32x32。"
    "验证统一在 32x32 上做（公平比较）。指标：最终验证准确率、每 epoch 平均耗时、GPU 峰值显存。\n"
    "leader 已预写参考实现（本地语法校验通过）——请**原样转写**为 main.py 提交，不要改实验设计：\n"
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
    "```\n"
    "提交参数：timeout_seconds=900；沙盒断网，上述代码不读任何外部数据。"
)
METHOD = "渐进式图像尺寸训练（ENTR 假设）vs 固定分辨率基线的两臂对照；小 CNN（2 卷积块+自适应池化）从零训练"
DATASET = (
    "无外部数据（沙盒断网）：在代码内用 torch 固定种子合成两类各 256 张 32x32 小图"
    "（横条纹/竖条纹+高斯噪声），不要读取任何 CSV 或下载任何数据集。"
)


def _now() -> float:
    return time.time()


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


class Trace:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.t0 = _now()
        self.saved = False

    def add(self, name: str, detail: str, output: str, state: str = "-", elapsed: float = 0.0) -> None:
        self.steps.append(
            {
                "step": name,
                "detail": detail,
                "state": state,
                "elapsed_s": round(elapsed, 1),
                "total_s": round(_now() - self.t0, 1),
                "output_chars": len(output or ""),
                "output_head": (output or "")[:1500],
            }
        )
        print(f"[{name}] state={state} 用时={elapsed:.1f}s 输出={len(output or '')}字", flush=True)

    def save(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "result.json").write_text(
            json.dumps(
                {"total_s": round(_now() - self.t0, 1), "steps": self.steps}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        self.saved = True


def _serve_partners(tmp: Path, gpu_aic: str):
    """起 gpu + experiment 两个真实伙伴；双双开 PEP（experiment 开 PEP 是令牌搬运工分支的前提）。"""
    builders = {"gpu": partner_gpu.build_gpu_agent, "experiment": partner_experiment.build_experiment_agent}
    ports = {"gpu": PORT_GPU, "experiment": PORT_EXP}
    runtimes: dict[str, WitRuntime] = {}
    providers: dict[str, object] = {}
    for kind in ("gpu", "experiment"):
        audit = OUT / f"audit-{kind}.jsonl"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text("", encoding="utf-8")
        os.environ["ACPS_PEP_ENABLED"] = "1"
        os.environ["ACPS_PEP_LEADER_AICS"] = dp.LEADER_AIC
        os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
        if kind == "experiment":
            # 令牌/env 兜底通道（桥接通道优先；gpu AIC 也兜底一份）
            os.environ["ACPS_GPU_AIC"] = gpu_aic
        root = tmp / f"ws-{kind}"
        shutil.copytree(CLUSTER / f"ws-{kind}" / "acps", root / "acps")
        if kind == "experiment":
            # 预热 experiment 的发现缓存：gpu 端点指向本地直连监听（exp.run 出站要用）
            cache_dir = root / "acps" / "discovered"
            cache_dir.mkdir(parents=True, exist_ok=True)
            acs = _acs_entry(
                gpu_aic,
                "研枢-GPU-Partner",
                ["gpu.env", "gpu.status", "gpu.data.list", "gpu.data.fetch", "gpu.run"],
                PORT_GPU,
            )
            cache_discovered_agent(
                cache_dir=cache_dir, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs
            )
        providers[kind] = agents.build_provider()
        runtimes[kind] = WitRuntime(
            builders[kind](providers[kind]), workspace=root, acps=dp._acps_config(ports[kind])
        )
    return runtimes, providers


async def _start_all(runtimes: dict[str, WitRuntime], trace: Trace) -> None:
    ports = {"gpu": PORT_GPU, "experiment": PORT_EXP}
    for kind, rt in runtimes.items():
        status = await rt.serve_direct_partner(host="127.0.0.1", port=ports[kind], amp=False)
        trace.add(f"serve-{kind}", f"端口 {ports[kind]}", f"status={status.status}")


def _make_caller(tmp: Path, exp_aic: str, gpu_aic: str):
    root = tmp / "ws-caller"
    shutil.copytree(dp.SUPERVISOR_WS / "acps", root / "acps")
    cache_dir = root / "acps" / "discovered"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for aic, name, skills, port in (
        (exp_aic, "研枢-Experiment-Partner", ["exp.run"], PORT_EXP),
        (gpu_aic, "研枢-GPU-Partner", ["gpu.env", "gpu.run"], PORT_GPU),
    ):
        acs = _acs_entry(aic, name, skills, port)
        cache_discovered_agent(
            cache_dir=cache_dir, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs
        )
    agent = WitAgent(
        id="yanshu.phaseb-leader",
        display_name="研枢-PhaseB-Leader",
        version="1.0.0",
        description="Phase B 编排者（supervisor 身份）：签发委托令牌并委派实验任务",
        instructions="Phase B 专用；令牌签发与委派都在代码侧完成。",
        model=None,
        capabilities=[],
        hooks=[AipDelegationHooks()],
        protocols=[AcpsDirect()],
    )
    return WitRuntime(agent, workspace=root, acps=dp._acps_config(PORT_CALLER)), AgentState()


async def _delegate_with_token(
    caller, state, exp_aic: str, request: str, token: str, *, trace: Trace, max_polls: int = 270
) -> str:
    """委派实验任务：任务文本 + 委托令牌 dataItem（阶段三搬运工路径）。"""
    t0 = _now()
    payload = token_data_item(token, target_aic=_enrollment_aic("ws-gpu"), skill_id=GPU_SKILL)
    result = await caller.aip_delegate(
        state,
        partner_aic=exp_aic,
        request=[
            TextPart(request),
            DataPart(data=dict(payload["data"]), metadata=dict(payload["metadata"])),
        ],
    )
    task_ref = result.get("task_ref")
    for _ in range(max_polls):
        if result.get("state") in TERMINAL or result.get("state") == "awaiting-completion" or not task_ref:
            break
        await asyncio.sleep(10)
        result = await caller.aip_task(state, action="wait", task_ref=task_ref)
        print(f"  [poll] state={result.get('state')} elapsed={_now() - t0:.0f}s", flush=True)
    if result.get("state") == "awaiting-completion" and task_ref:
        result = await caller.aip_task(state, action="complete", task_ref=task_ref)
    elapsed = _now() - t0
    text = ""
    for product in result.get("products") or []:
        for item in product.get("data_items") or product.get("dataItems") or []:
            text += (item.get("text") or "") + "\n"
    state_str = str(result.get("state"))
    trace.add("B2-exp.run", f"taskRef={task_ref}", text, state=state_str, elapsed=elapsed)
    return text


def _print_audit(path: Path, label: str) -> None:
    if not path.exists():
        print(f"[audit:{label}] （无审计文件）")
        return
    print(f"[audit:{label}]")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        print(
            f"  {r.get('decision', ''):5s} rule={r.get('rule', ''):18s} "
            f"jti={(r.get('jti') or '-')[:10]} reason={r.get('reason', '')}"
        )


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    trace = Trace()
    tmp = Path(tempfile.mkdtemp(prefix="repro-b-"))
    exp_aic = _enrollment_aic("ws-experiment")
    gpu_aic = _enrollment_aic("ws-gpu")
    print(f"[setup] 输出目录 {OUT} | experiment={exp_aic[-24:]} | gpu={gpu_aic[-24:]}", flush=True)
    runtimes: dict[str, WitRuntime] = {}
    providers: dict[str, object] = {}
    try:
        # B0 签发委托令牌：delegate=experiment, target=gpu, skill=gpu.run
        issued = issue_token(
            load_signing_context(dp.SUPERVISOR_WS / "acps" / "client", trust_bundle=dp.TRUST_BUNDLE),
            delegate_aic=exp_aic,
            target_aic=gpu_aic,
            skill_id=GPU_SKILL,
            ttl_seconds=600,  # 签发侧硬上限（MAX_TTL_SECONDS）；令牌只在 gpu.run 准入时消费一次，与训练时长无关
        )
        print(f"[B0] 已签发 jti={issued.jti[:10]}… 有效期 {issued.expires_at - int(time.time())}s", flush=True)
        trace.add("B0-issue-token", f"jti={issued.jti[:10]}…", "ok")
        (OUT / "token-jti.txt").write_text(issued.jti, encoding="utf-8")

        runtimes, providers = _serve_partners(tmp, gpu_aic)
        await _start_all(runtimes, trace)
        caller, state = _make_caller(tmp, exp_aic, gpu_aic)

        # B1+ 待办合并：一次委派完成 生成代码→沙盒训练→报告
        request = (
            "请调用 exp.run 执行下面的论文复现实验，并把返回的实验报告原样转达。\n"
            f"question={QUESTION}\n\nmethod={METHOD}\n\ndataset={DATASET}\n\n"
            f"timeout_seconds={JOB_TIMEOUT}"
        )
        report = await _delegate_with_token(caller, state, exp_aic, request, issued.token, trace=trace)

        (OUT / "report.md").write_text(report, encoding="utf-8")
        ok = "awaiting" not in report.lower()[:200]
        print(f"\n[done] 实验报告 {'已产出' if ok else '未完成（awaiting/缺项）'}，产物在 {OUT}", flush=True)
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
        if not trace.saved:
            trace.save()
        print("\n===== 审计轨迹 =====", flush=True)
        _print_audit(OUT / "audit-gpu.jsonl", "gpu")
        _print_audit(OUT / "audit-experiment.jsonl", "experiment")
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
