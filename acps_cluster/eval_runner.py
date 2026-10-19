"""评测服务执行底座：数据集藏在 agent 背后，调用方只能交预测或交代码，只拿分数。

两种提交模式：
- **模式 A（交预测，submit_predictions）**：调用方在自己的环境里产出预测
  （JSON 数组），host 侧可信指标函数打分。零沙盒——地面真相全程不出本进程。
- **模式 B（交代码，submit_code）**：调用方提交代码，在**沙盒**里读隐藏特征
  （`/eval/input.csv` 只读挂载）产出预测，host 侧打分。沙盒三道防泄题闸门：
  ① **标签永不进容器**——挂载的是特征文件，labels.csv 只存在于 host 侧；
  ② **stdout 管制**——返回值不含 stdout（提交代码可以 print 特征，拿不回来；
     原始 stdout 仅落本地运行日志供审计）；
  ③ **产物不回传**——容器内 output/ 的任何文件（含 predictions.json 本身）不返回
     给调用方，只返回分数；外加 network=none（复用算力沙盒的既有边界）。

指标与榜单：指标函数本机可信执行；榜单 JSON 追加写（提交者 AIC + 分数 + 时间）。
复用边界：沙盒旗标沿用 gpu_runner 的硬边界清单（镜像白名单/断网/限额/只读根/非 root），
挂载布局不同（/eval 特征挂载，无共享 datasets/ 挂载），故独立成表。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
import uuid
from pathlib import Path

from arg_shapes import as_list  # 工具入参数组宽容化（provider 把 array 送成 JSON 字符串）

CLUSTER_DIR = Path(__file__).parent
EVAL_PRIVATE_DIR = CLUSTER_DIR / "eval-private"
EVAL_RUNS_DIR = CLUSTER_DIR / "ws-eval" / "runs"
LEADERBOARD_PATH = CLUSTER_DIR / "ws-eval" / "leaderboard.json"

ALLOWED_IMAGES = ("pytorch:cu128",)  # 与算力沙盒同一份本地白名单
ALLOWED_EXECUTABLES = ("python", "python3")
DEFAULT_IMAGE = "pytorch:cu128"
MAX_CODE_FILES_BYTES = 2 * 1024 * 1024
MAX_PREDICTIONS = 100_000
EVAL_TIMEOUT_SECONDS = 300
SCORE_BUCKET = 0.05  # 对外反馈的分档粒度（防"逐条翻转还原隐藏标签"探测）
_SAFE_BENCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$")

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._\-]+$")


# ─────────────────── 基准（benchmark）注册表 ───────────────────


def benchmark_dir(bench_id: str) -> Path:
    """基准目录。

    id 直接拼进路径，所以必须是短名：`..`/`../..`/绝对路径会把宿主目录当成基准，
    于是评测方能挂载别人的 input.csv、并让 `_labels_of` 抛未捕获的 FileNotFoundError
    （2026-10-07 审查修复）。
    """
    if not isinstance(bench_id, str) or not _SAFE_BENCH_ID.match(bench_id):
        raise ValueError(f"基准 id 不合法：{bench_id!r}（须 1..32 位字母数字与 .-_，首字符为字母或数字）")
    return EVAL_PRIVATE_DIR / bench_id


def list_benchmarks() -> list[dict]:
    items = []
    if not EVAL_PRIVATE_DIR.is_dir():
        return items
    for entry in sorted(EVAL_PRIVATE_DIR.iterdir()):
        if entry.is_dir() and (entry / "input.csv").is_file():
            with (entry / "input.csv").open(encoding="utf-8") as handle:
                rows = sum(1 for _ in handle) - 1
            items.append({
                "id": entry.name,
                "features": (entry / "input.csv").name,
                "metric": "accuracy",
                "rows": rows,
                "note": "标签由评测方持有，不随特征下发（防泄题）",
            })
    return items


def ensure_demo_benchmark(*, rows: int = 60, seed: int = 42) -> Path:
    """生成确定性演示基准 demo-cls：两维特征、二分类标签（规则可学的噪声数据）。"""
    import random

    bench = benchmark_dir("demo-cls")
    bench.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    lines_in = ["x0,x1"]
    lines_label = ["label"]
    for _ in range(rows):
        x0 = rng.uniform(-2, 2)
        x1 = rng.uniform(-2, 2)
        flip = rng.random() < 0.08  # 8% 标签噪声：满分不可能是 100%，预测必须真学
        label = 1 if (x0 + x1 > 0) != flip else 0
        lines_in.append(f"{x0:.4f},{x1:.4f}")
        lines_label.append(str(label))
    (bench / "input.csv").write_text("\n".join(lines_in) + "\n", encoding="utf-8")
    (bench / "labels.csv").write_text("\n".join(lines_label) + "\n", encoding="utf-8")
    return bench


def _labels_of(bench_id: str) -> list[int]:
    path = benchmark_dir(bench_id) / "labels.csv"
    rows = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [int(v) for v in rows[1:]]  # 首行 header


def score_accuracy(bench_id: str, predictions: list) -> dict:
    """host 侧可信指标：accuracy + 混淆计数。预测长度必须与标签一致。"""
    labels = _labels_of(bench_id)
    preds = [int(p) for p in predictions]
    if len(preds) != len(labels):
        raise ValueError(f"预测数量 {len(preds)} 与样本数 {len(labels)} 不符")
    correct = sum(1 for p, y in zip(preds, labels) if p == y)
    return {
        "metric": "accuracy",
        "score": round(correct / max(1, len(labels)), 4),
        "correct": correct,
        "total": len(labels),
    }


# ─────────────────── 模式 A：交预测 ───────────────────


def validate_predictions(bench_id: str, predictions) -> str | None:
    predictions = as_list(predictions)
    if not isinstance(predictions, list) or not predictions:
        return "predictions 必须是非空数组（0/1 标签按 input.csv 行序）"
    if len(predictions) > MAX_PREDICTIONS:
        return f"predictions 超过上限 {MAX_PREDICTIONS}"
    try:
        known = benchmark_dir(bench_id).is_dir()
    except ValueError as exc:
        return str(exc)
    if not known:
        return f"未知基准 {bench_id}（可用：{[b['id'] for b in list_benchmarks()]}）"
    try:
        [int(p) for p in predictions]
    except (TypeError, ValueError):
        return "predictions 的每个元素必须是可解析为整数的标签"
    return None


def record_leaderboard(bench_id: str, submitter: str, score: float, *, path: Path | None = None) -> list[dict]:
    target = path or LEADERBOARD_PATH
    board: list[dict] = []
    if target.is_file():
        try:
            board = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            board = []
    board.append({
        "benchmark": bench_id, "submitter": submitter, "score": score,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    board.sort(key=lambda e: -e["score"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(board[:50], ensure_ascii=False, indent=1), encoding="utf-8")
    return board[:50]


def _bucket_score(score: float) -> float:
    return round(round(float(score) / SCORE_BUCKET) * SCORE_BUCKET, 2)


def _coarse(result: dict) -> dict:
    """对外反馈收敛成分档。

    精确 `score` + `correct/total` 等于给出逐行边际信号：调用方可在配额内逐条翻转预测、
    按分数增减反推每一行的真实标签，把隐藏标签一条条问出来（2026-10-07 审查修复）。
    分档后单次探测最多指示 ±1 行的方向；精确分仍完整记入宿主侧榜单与审计。
    """
    return {"metric": result["metric"], "score": _bucket_score(result["score"]),
            "scoreGranularity": SCORE_BUCKET}


def _public_board(board: list[dict]) -> list[dict]:
    """调用方可见榜单：分数只给分档值，精确分留在宿主审计文件。"""
    return [
        {**entry, "score": _bucket_score(entry.get("score") or 0.0),
         "scoreGranularity": SCORE_BUCKET}
        for entry in board
    ]


def submit_predictions(bench_id: str, predictions, *, submitter: str = "?") -> dict:
    predictions = as_list(predictions)
    error = validate_predictions(bench_id, predictions)
    if error:
        return {"ok": False, "error": error}
    try:
        result = score_accuracy(bench_id, predictions)
    except (OSError, TypeError, ValueError) as exc:
        return {"ok": False, "error": f"评分失败: {exc}"}
    board = record_leaderboard(bench_id, submitter, result["score"])
    return {"ok": True, "mode": "predictions", "benchmark": bench_id,
            **_coarse(result), "leaderboard": _public_board(board[:5])}


# ─────────────────── 模式 B：交代码（沙盒 + 三道闸门） ───────────────────


def validate_code_submission(raw: dict) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "提交必须是 JSON 对象"
    bench_id = str(raw.get("benchmark") or "").strip()
    try:
        known = benchmark_dir(bench_id).is_dir()
    except ValueError as exc:
        return None, str(exc)
    if not known:
        return None, f"未知基准 {bench_id}"
    files = raw.get("files")
    if not isinstance(files, dict) or not files:
        return None, "files 必须是非空对象：{文件名: 内容}，入口 main.py"
    total = 0
    for name, content in files.items():
        if not isinstance(name, str) or not _SAFE_FILENAME.match(name) or name.startswith("."):
            return None, f"文件名不合法：{name!r}"
        size = len(content.encode("utf-8")) if isinstance(content, str) else -1
        if size < 0:
            return None, f"文件内容必须是字符串：{name}"
        if size > MAX_CODE_FILES_BYTES:
            return None, f"文件 {name} 超过单文件上限 2MB"
        total += size
    if total > MAX_CODE_FILES_BYTES * 2:
        return None, "提交总大小超过 4MB"
    if "main.py" not in files:
        return None, "入口文件必须是 main.py"
    try:
        timeout_seconds = max(30, min(int(raw.get("timeout_seconds") or 120), EVAL_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return None, "timeout_seconds 必须是整数"
    return {"benchmark": bench_id, "files": dict(files), "timeout_seconds": timeout_seconds}, None


def _eval_docker_args(spec: dict, run_id: str, input_csv: Path, code_dir: Path, output_dir: Path) -> list[str]:
    """沙盒旗标与算力沙盒同源（gpu_runner 硬边界清单），挂载布局不同：
    特征只读挂 /eval/input.csv；**无共享 datasets/ 挂载**；labels 永不挂载。"""
    return [
        "docker", "run", "--rm",
        f"--name=eval-run-{run_id}",
        "--network", "none",
        "--memory", "2g",
        "--cpus", "2",
        "--pids-limit", "128",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=256m",
        "--user", "1000:1000",
        "-v", f"{input_csv}:/eval/input.csv:ro",
        "-v", f"{code_dir}:/workspace/input:ro",
        "-v", f"{output_dir}:/workspace/output:rw",
        "-w", "/workspace/output",
        "--stop-timeout", "5",
        DEFAULT_IMAGE, "python", "/workspace/input/main.py",
    ]


async def submit_code(raw: dict, *, submitter: str = "?", runs_root: Path = EVAL_RUNS_DIR) -> dict:
    """模式 B：代码在沙盒内读 /eval/input.csv 产出 output/predictions.json，host 打分。

    **返回值只含分数**——stdout（可能含特征）与产物（可能夹带数据）都不回传；
    stdout 原文落 runs/<id>/stdout.log 仅供评测方本地审计。
    """
    import gpu_runner

    spec, error = validate_code_submission(raw)
    run_id = uuid.uuid4().hex[:10]
    if spec is None:
        return {"ok": False, "error": error}
    if not gpu_runner.docker_available():
        return {"ok": False, "error": "Docker 不可用（模式 B 需要）"}

    try:
        bench = benchmark_dir(spec["benchmark"])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    run_dir = runs_root / f"run-{run_id}"
    input_dir, output_dir = run_dir / "input", run_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in spec["files"].items():
        (input_dir / name).write_text(content, encoding="utf-8")

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *_eval_docker_args(spec, run_id, bench / "input.csv", input_dir, output_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        (
            stdout_b, stderr_b, _out_total, _err_total, out_trunc, err_trunc,
        ) = await asyncio.wait_for(
            gpu_runner.communicate_capped(proc), timeout=spec["timeout_seconds"]
        )
    except asyncio.TimeoutError:
        timed_out = True
        await asyncio.create_subprocess_exec("docker", "kill", f"eval-run-{run_id}")
        (
            stdout_b, stderr_b, _out_total, _err_total, out_trunc, err_trunc,
        ) = await gpu_runner.communicate_capped(proc)
    except BaseException:
        # 与 gpu_runner 同款保护（2026-10-11 审查 P1）：框架 tool_timeout 引发的
        # CancelledError 不会进 TimeoutError 分支 → 容器成孤儿、独占设备。
        # 任何异常路径都杀容器；原先只捕 TimeoutError，是同一轮审查的漏改项。
        with contextlib.suppress(Exception):
            await asyncio.create_subprocess_exec("docker", "kill", f"eval-run-{run_id}")
        with contextlib.suppress(Exception):
            await gpu_runner.communicate_capped(proc)
        raise
    finally:
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                await asyncio.create_subprocess_exec("docker", "kill", f"eval-run-{run_id}")
    duration = round(time.monotonic() - started, 1)

    # stdout 管制：原文只落本地审计日志，绝不进入返回值
    stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")
    if out_trunc:
        stdout_text += (
            f"\n…[stdout 超过采集上限 {gpu_runner.STREAM_CAPTURE_LIMIT} 字节，后续输出已丢弃]…"
        )
    (run_dir / "stdout.log").write_text(stdout_text, encoding="utf-8")

    if timed_out:
        return {"ok": False, "runId": run_id, "error": f"超时（>{spec['timeout_seconds']}s）", "durationSeconds": duration}
    predictions_file = output_dir / "predictions.json"
    if proc.returncode != 0 or not predictions_file.is_file():
        # 闸门 ②：stderr 正文不回传——"把特征打到 stderr 并非零退出"曾是泄题旁路，
        # 每次 200 字节足以分页搬空 /eval/input.csv。正文只落宿主 run 目录供节点主人核查。
        (run_dir / "stderr.log").write_text(
            (stderr_b or b"").decode("utf-8", errors="replace"), encoding="utf-8")
        return {"ok": False, "runId": run_id,
                "error": f"代码未成功产出 predictions.json（exit={proc.returncode}）",
                "durationSeconds": duration,
                "note": "stderr 已落盘本节点 run 目录（防泄题，不随响应返回）"}
    try:
        predictions = json.loads(predictions_file.read_text(encoding="utf-8"))
    except ValueError:
        return {"ok": False, "runId": run_id, "error": "predictions.json 不是合法 JSON", "durationSeconds": duration}

    try:
        result = score_accuracy(spec["benchmark"], predictions)
    except (OSError, TypeError, ValueError) as exc:
        return {"ok": False, "runId": run_id, "error": str(exc), "durationSeconds": duration}
    board = record_leaderboard(spec["benchmark"], submitter, result["score"])
    # 闸门 ③：只回**分档**分数与榜单；stdout/产物/predictions 原文一概不回
    return {"ok": True, "mode": "code", "runId": run_id, "benchmark": spec["benchmark"],
            "durationSeconds": duration, **_coarse(result),
            "leaderboard": _public_board(board[:5])}
