"""本地评测打分及榜单逻辑，由评分回归测试使用。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from acps_cluster.core.arg_shapes import as_list  # 工具入参数组宽容化（provider 把 array 送成 JSON 字符串）

CLUSTER_DIR = Path(__file__).resolve().parents[1]
EVAL_PRIVATE_DIR = CLUSTER_DIR / "eval-private"
LEADERBOARD_PATH = CLUSTER_DIR / "ws-eval" / "leaderboard.json"

MAX_PREDICTIONS = 100_000
SCORE_BUCKET = 0.05  # 对外反馈的分档粒度（防"逐条翻转还原隐藏标签"探测）
_SAFE_BENCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,31}$")


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
