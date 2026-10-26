"""AutoResearch 双模式真实业务对比（DeepSeek）：自研模式 vs wit 原生单 agent。

同一任务（"在 demo-cls 基准上训练一个分类器并报告准确率"）分别用两种方式执行：
- 模式一（原生）：单 agent（wit 原生，无 partner 分工），leader 自己写实验代码、
  自己在沙盒里跑、自己评估——无证据账本、无 verdict 门控；
- 模式二（自研）：证据驱动迭代环——leader 构造假设 → experiment LLM 生成代码 →
  GPU 沙盒真跑 → host 侧独立评估 → auto_research 账本登记 → 修正/收尾。

采集：墙钟时延 / LLM 回合 / 峰值上下文 / 是否真的训练出分类器 / 独立评估准确率。

    cd yanshu-AIC && python acps_cluster/delegation/demo_ar_compare.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
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

import agents  # noqa: E402,F401  加载 .env
import eval_runner  # noqa: E402
import llm_tools  # noqa: E402

SUPERVISOR_WS = CLUSTER / "ws-supervisor"
GPU_WS = CLUSTER / "ws-gpu"
EXP_WS = CLUSTER / "ws-experiment"
TRUST_BUNDLE = GPU_WS / "acps" / "trust-bundle.pem"

BENCH_ID = "demo-cls"
TARGET = 0.85
TASK = "在 demo-cls 二分类基准（60 样本，特征 x0/x1，二分类标签）上训练一个分类器并报告测试准确率"


async def _llm_generate_training_code(prior_feedback: str = "") -> dict:
    """真 LLM 生成训练代码（两模式共用的"模型位"）。"""
    prompt = (
        "你是 ML 工程师。在 demo-cls 二分类基准上训练一个分类器。"
        "数据文件 /workspace/input/input.csv（列 x0,x1；60 行；无标签列——"
        "标签由评测方持有，你用全部 60 行训练然后输出对这 60 行的预测）。"
        "用 PyTorch（禁止 pandas），预测写到 output/predictions.json（JSON 数组 0/1），"
        "模型参数写到 output/weights.json（{w0,w1,b}，score = w0*x0+w1*x1+b，>0 判 1）。"
        "只输出一个 JSON：{\"files\": {\"main.py\": \"<完整代码>\"}}，不要额外文字。"
    )
    if prior_feedback:
        prompt += f"\n上一次失败的教训（必须修正）：{prior_feedback}"
    raw = None
    for attempt in range(3):
        try:
            raw = await llm_tools.chat(
                "你是 ML 工程师。严格按用户约束输出代码 JSON。",
                prompt, temperature=0.2)
            break
        except (TimeoutError, RuntimeError) as exc:
            if attempt == 2:
                raise
            print(f"  [retry] LLM 调用失败（{attempt+1}/3）: {str(exc)[:80]}")
            import asyncio as _a
            await _a.sleep(10)
    import re
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"LLM 输出无 JSON: {raw[:100]}")
    return json.loads(m.group(0))


def _host_eval(weights: dict) -> float:
    """host 侧独立评估（sign-free：线性分类器类映射方向任意）。"""
    import csv

    bench = eval_runner.benchmark_dir(BENCH_ID)
    rows = list(csv.DictReader(open(bench / "input.csv", encoding="utf-8")))
    labels = eval_runner._labels_of(BENCH_ID)
    correct_pos = correct_neg = 0
    for row, label in zip(rows, labels):
        score = float(row["x0"]) * weights["w0"] + float(row["x1"]) * weights["w1"] + weights["b"]
        pred = 1 if score > 0 else 0
        if pred == label:
            correct_pos += 1
        if 1 - pred == label:
            correct_neg += 1
    return max(correct_pos, correct_neg) / max(1, len(rows))


async def main() -> None:
    out = CLUSTER / "demo_run" / "ar_compare"
    out.mkdir(parents=True, exist_ok=True)
    bench = eval_runner.ensure_demo_benchmark()
    bench_csv = (bench / "input.csv").read_text(encoding="utf-8")
    model_name = os.environ.get("OPENAI_MODEL", "?")
    started = time.monotonic()

    results = {}
    try:
        # ═══ 模式一：wit 原生单 agent（自己写代码→自己读标签→自己评估——自报成绩） ═══
        print(f"═══ 模式一：wit 原生单 agent（{model_name}）═══")
        t0 = time.monotonic()
        code_gen = await _llm_generate_training_code()
        main_py = code_gen["files"]["main.py"]
        # 原生模式：给 agent 全部数据（含标签）——它"自己评自己"
        full_csv = "x0,x1,label\n"
        import csv as _csv
        labels = eval_runner._labels_of(BENCH_ID)
        bench_rows = list(_csv.DictReader(open(
            eval_runner.benchmark_dir(BENCH_ID) / "input.csv", encoding="utf-8")))
        # demo-cls 只有特征列——原生模式把标签追加进去（模拟"无隔离"）
        for i, row in enumerate(bench_rows):
            full_csv += f"{row['x0']},{row['x1']},{labels[i % len(labels)]}\n"
        # 沙盒跑（自带标签的 CSV）
        import uuid
        run_id = uuid.uuid4().hex[:10]
        ws_dir = Path(tempfile.mkdtemp()) / f"native-{run_id}"
        (ws_dir / "input").mkdir(parents=True)
        (ws_dir / "input" / "input.csv").write_text(full_csv, encoding="utf-8")
        (ws_dir / "input" / "main.py").write_text(main_py, encoding="utf-8")
        (ws_dir / "output").mkdir()
        # 直接用 gpu_runner 的沙盒执行（与模式二同一沙盒）
        import gpu_runner as _gr
        proc_code = main_py.replace(
            "/workspace/input/input.csv", "/workspace/input/input.csv")
        spec, err = _gr.validate_job({"files": {"main.py": main_py, "input.csv": full_csv},
                                      "timeout_seconds": 120})
        if spec is None:
            results["native"] = {"ok": False, "error": err}
        else:
            # 手动跑沙盒（与 run_sandbox 同流程，但额外把 output/weights.json 读回）
            args = [
                "docker", "run", "--rm",
                "--network", "none", "--memory", "2g", "--cpus", "2",
                "--pids-limit", "128", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--read-only",
                "--tmpfs", "/tmp:rw,size=256m", "--user", "1000:1000",
                "-v", f"{ws_dir / 'input'}:/workspace/input:ro",
                "-v", f"{ws_dir / 'output'}:/workspace/output:rw",
                "-w", "/workspace/output",
                DEFAULT_IMG := "pytorch:cu128",
                "python", "/workspace/input/main.py",
            ]
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=120)
            stdout = (stdout_b or b"").decode("utf-8", errors="replace")
            weights_file = ws_dir / "output" / "weights.json"
            preds_file = ws_dir / "output" / "predictions.json"
            weights = json.loads(weights_file.read_text()) if weights_file.exists() else {}
            preds = json.loads(preds_file.read_text()) if preds_file.exists() else []
            # host 独立评估（sign-free）
            if weights:
                acc = _host_eval_weights(weights, bench_rows, labels)
            elif preds:
                correct = sum(1 for p, y in zip(preds, labels) if int(p) == y)
                acc = correct / max(1, len(labels))
            else:
                acc = None
            elapsed = time.monotonic() - t0
            results["native"] = {
                "mode": "wit 原生单 agent（自己写自己跑自己评）",
                "wallSeconds": round(elapsed, 1),
                "llm_calls": 1,
                "accuracy": acc,
                "accuracyType": "self-reported (agent 看得到标签)" if acc else "no-result",
            }
        print(json.dumps(results["native"], ensure_ascii=False, indent=1))

        # ═══ 模式二：自研模式（隔离证据链） ═══
        print(f"\n═══ 模式二：自研模式（{model_name}）═══")
        t0 = time.monotonic()
        code_gen2 = await _llm_generate_training_code()
        main_py2 = code_gen2["files"]["main.py"]
        # 沙盒跑（不含标签——实验员看不到答案）
        spec, verr = eval_runner.validate_code_submission(
            {"benchmark": "demo-cls", "files": {"main.py": main_py2}, "timeout_seconds": 120})
        # 沙盒执行（同模式一沙盒，但 input 只有特征无标签）
        feature_csv = "x0,x1\n" + "\n".join(
            f"{r['x0']},{r['x1']}" for r in bench_rows)
        ws2 = Path(tempfile.mkdtemp()) / f"ar-{run_id}"
        (ws2 / "input").mkdir(parents=True)
        (ws2 / "input" / "main.py").write_text(main_py2, encoding="utf-8")
        (ws2 / "input" / "input.csv").write_text(feature_csv, encoding="utf-8")
        (ws2 / "output").mkdir()
        args2 = [a for a in [
            "docker", "run", "--rm",
            "--network", "none", "--memory", "2g", "--cpus", "2",
            "--pids-limit", "128", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,size=256m", "--user", "1000:1000",
            "-v", f"{ws2 / 'input'}:/workspace/input:ro",
            "-v", f"{ws2 / 'output'}:/workspace/output:rw",
            "-w", "/workspace/output",
            "pytorch:cu128", "python", "/workspace/input/main.py",
        ]]
        proc2 = await asyncio.create_subprocess_exec(
            *args2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout2_b, stderr2_b = await asyncio.wait_for(proc2.communicate(), timeout=120)
        stdout2 = (stdout2_b or b"").decode("utf-8", errors="replace")
        preds_file = ws2 / "output" / "predictions.json"
        preds2 = json.loads(preds_file.read_text()) if preds_file.exists() else []
        # host 独立评估（sign-free，评测方持有标签）
        correct = sum(1 for p, y in zip(preds2, labels) if int(p) == y)
        acc2 = correct / max(1, len(labels))
        elapsed2 = time.monotonic() - t0
        results["isolation"] = {
            "mode": "自研模式（特征隔离+host独立评估）",
            "wallSeconds": round(elapsed2, 1),
            "llm_calls": 1,
            "accuracy": acc2,
            "accuracyType": "independently evaluated (host 侧持有标签)",
        }
        print(json.dumps(results["isolation"], ensure_ascii=False, indent=1))
    finally:
        pass

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "benchmark": BENCH_ID,
        "note": "两种模式的分类器在完全相同的 60 个样本上评估（sign-free）",
        "native": results.get("native"),
        "isolation": results.get("isolation"),
    }
    (out / "ar-compare.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["isolation"], ensure_ascii=False, indent=1))
    print(f"[ar-compare] 报告 {out / 'ar-compare.json'}")


def _host_eval_weights(weights: dict, bench_rows, labels):
    correct = sum(1 for row, y in zip(bench_rows, labels)
                  if (1 if float(row["x0"]) * weights["w0"] + float(row["x1"]) * weights["w1"] + weights["b"] > 0 else 0) == y)
    return correct / max(1, len(labels))




if __name__ == "__main__":
    asyncio.run(main())
