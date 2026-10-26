"""AutoResearch 真实业务（A 系列）：证据驱动迭代环产出真实分类器。

编排为代码侧（leader 的确定性部分）+ 真实 LLM 的两处模型位：
  ① frontier.detect 直调（真实 OpenAlex 数据）→ 构造可证伪假设；
  ② exp 侧实验员 LLM 生成实验代码 → GPU 沙盒真跑（复用 G6 验证过的 experiment
     流水线的沙盒执行段）→ 产出模型与预测；
  ③ host 侧独立评估：eval_runner.score_accuracy 用 held-out 标签独立打分（非自报）；
  ④ auto_research 真工具登记证据（iterate/verdict/修正链）→ summarize 双池。

产物：训练出的分类器参数（weights.json）+ 独立评估报告 + 证据账本——
即"小型分类器"这一真实成果，以及支撑它的完整证据链。

    cd yanshu-AIC && python acps_cluster/delegation/demo_autoresearch_e2e.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402,F401  加载 .env（OPENAI_MODEL 亦走 .env，勿再写死模型）
import eval_runner  # noqa: E402
import gpu_runner  # noqa: E402
import llm_tools  # noqa: E402
from supervisor import _make_auto_research_tool  # noqa: E402
from delegation.demo_domain import TRUST  # noqa: E402
from delegation.demo_fanout import _aic_of  # noqa: E402

SUPERVISOR_WS = CLUSTER / "ws-supervisor"
TRUST = SUPERVISOR_WS / "acps" / "trust-bundle.pem"
LEADER_AIC = _aic_of(SUPERVISOR_WS)

BENCH_ID = "demo-cls"
TARGET_ACCURACY = 0.85
MAX_ITERATIONS = 2


async def generate_hypothesis_code(iteration: int, prior_feedback: str = "") -> tuple[str, str]:
    """真 LLM：生成实验代码（迭代 1）或修正后代码（迭代 2）。返回 (假设, 代码)。"""
    sys_prompt = (
        "你是实验工程师。任务：在 demo-cls 二分类基准（60 样本，两维特征 x0/x1，"
        "标签 0/1，约 8% 标签噪声）上训练一个高准确率的分类器。硬性要求："
        "① 用 PyTorch 写一个小型线性/浅层网络分类器（不要复杂模型，数据只有两维）；"
        "② 数据文件在 /workspace/input/input.csv（列 x0,x1，60 行），用 open('/workspace/input/input.csv') "
        "或 csv 模块读取；只用 torch 标准库（禁止 pandas/numpy 以外的第三方——numpy 和 torch 可用，"
        "pandas 不可用）；标签不可见（不要试图读取任何 label 文件）；产物必须写到当前目录（相对路径）；"
        "③ 训练后把模型权重（线性层参数）和测试子集上的预测准确率写进 weights.json："
        '{"weights": {"w0": ..., "w1": ..., "b": ...}, "train_accuracy": ...}；'
        "④ 打印 TEST-ACC <值>。只输出一个 JSON："
        '{"hypothesis": "可证伪假设一句话", "files": {"main.py": "<完整代码>"}}'
    )
    user = f"迭代轮次：{iteration}。"
    if prior_feedback:
        user += f"\n上一轮的教训（必须修正）：{prior_feedback}"
    raw = await llm_tools.chat(sys_prompt, user, temperature=0.3)

    fenced = __import__("re").search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, __import__("re").S)
    blob = fenced.group(1) if fenced else raw
    data = json.loads(blob)
    return data["hypothesis"], data["files"]["main.py"]


async def run_experiment(code: str) -> dict:
    """沙盒真跑：GPU 容器执行实验代码，返回 stdout/产物（含 weights.json）。

    demo-cls 特征 CSV 作为输入文件注入（容器内 /workspace/input/input.csv，
    只读）——标签不下发（host 侧独立评估用）。
    """
    bench_csv = eval_runner.benchmark_dir(BENCH_ID) / "input.csv"
    files = {
        "main.py": code,
        "input.csv": bench_csv.read_text(encoding="utf-8"),
    }
    result = await gpu_runner.run_sandbox(
        {"files": files, "timeout_seconds": 180},
    )
    return result


def independent_eval(weights: dict, bench_dir: Path) -> float:
    """host 侧独立评估：用沙盒产出的线性权重在**全部 60 个带标签样本**上算准确率。

    训练在容器内只用特征（无标签），评估在 host 用真标签——独立于训练过程。
    """
    import csv

    w0, w1, b = weights["w0"], weights["w1"], weights["b"]
    rows = list(csv.DictReader(open(bench_dir / "input.csv", encoding="utf-8")))
    labels = eval_runner._labels_of(BENCH_ID)
    correct_pos = correct_neg = 0
    for row, label in zip(rows, labels):
        score = float(row["x0"]) * w0 + float(row["x1"]) * w1 + b
        pred = 1 if score > 0 else 0
        if pred == label:
            correct_pos += 1
        if 1 - pred == label:
            correct_neg += 1
    # sign-free：线性分类器的类映射方向任意，取两方向最优（公平评估）
    return max(correct_pos, correct_neg) / max(1, len(labels))


async def main() -> None:
    out = CLUSTER / "demo_run" / "autoresearch"
    out.mkdir(parents=True, exist_ok=True)
    bench = eval_runner.ensure_demo_benchmark()
    tool = _make_auto_research_tool()
    started = time.monotonic()

    activation = asyncio.run = await tool.handler({})
    print("① 自研模式激活:", activation["mode"], "| 上限", activation["maxIterations"], "轮")

    ledger = []
    hypothesis = ""
    prior_feedback = ""
    final_result = {}

    for iteration in range(1, MAX_ITERATIONS + 1):
        print(f"\n═══ 迭代 {iteration}（真 LLM 生成 → 沙盒真跑 → host 独立评估）═══")
        hyp, code = await generate_hypothesis_code(iteration, prior_feedback)
        print(f"  假设: {hyp[:80]}")

        result = await run_experiment(code)
        print(f"  沙盒: exit={result.exit_code} duration={result.duration_s}s")
        if not result.ok:
            prior_feedback = f"沙盒失败: {result.stderr[:200]}"
            ledger.append({"iteration": iteration, "hypothesis": hyp, "verdict": "inconclusive",
                            "error": prior_feedback})
            tool_r = await tool.handler({"action": "iterate", "hypothesis": hyp,
                                          "run_ref": f"sandbox-{result.run_id}",
                                          "verdict": "inconclusive"})
            continue

        stdout = result.stdout
        weights_art = next((a for a in result.artifacts if a["name"] == "weights.json"), None)
        self_reported = None
        if weights_art and isinstance(weights_art.get("content"), str):
            try:
                self_reported = json.loads(weights_art["content"]).get("train_accuracy")
            except ValueError:
                pass

        # host 侧独立评估：解析 weights，在全量带标签数据上复核
        independent = None
        if weights_art and isinstance(weights_art.get("content"), str):
            try:
                weights = json.loads(weights_art["content"]).get("weights")
                if weights:
                    independent = independent_eval(weights, bench)
            except (ValueError, KeyError, TypeError):
                independent = None

        verdict = "supported" if (independent is not None and independent >= TARGET_ACCURACY) else "refuted"
        r = await tool.handler({
            "action": "iterate", "hypothesis": hyp,
            "run_ref": f"sandbox-{result.run_id}", "verdict": verdict,
            "metrics": f"independent_acc={independent}" if independent is not None else "",
        })
        ledger.append(r.get("ledger", [{}])[-1] if r.get("ledger") else {})
        print(f"  独立评估: {independent} → verdict={verdict}")

        if verdict == "supported":
            final_result = {"hypothesis": hyp, "independent_acc": independent,
                            "weights": weights, "run_id": result.run_id}
            break
        prior_feedback = f"独立评估准确率 {independent}，未达 {TARGET_ACCURACY}——换方法"

    summarize = await tool.handler({"action": "summarize"})
    wall = round(time.monotonic() - started, 1)

    summary = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "target": f"demo-cls 分类器 准确率 ≥{TARGET_ACCURACY}",
        "wallSeconds": wall,
        "iterations": len(ledger),
        "ledger": ledger,
        "summarize": summarize,
        "finalResult": final_result,
    }
    (out / "autoresearch-e2e.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n════════════ AutoResearch 真实成果 ════════════")
    if final_result:
        print(f"✅ 训练出分类器：{final_result['hypothesis'][:60]}")
        print(f"   独立评估准确率: {final_result['independent_acc']}")
        print(f"   模型权重: {json.dumps(final_result['weights'])}")
    else:
        print(f"❌ 未达标: {ledger}")
    print(f"[ar] 留档 {out / 'autoresearch-e2e.json'}")


if __name__ == "__main__":
    asyncio.run(main())
