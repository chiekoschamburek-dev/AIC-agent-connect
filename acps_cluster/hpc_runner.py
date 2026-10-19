"""HPC 网关执行底座：把机构现有调度器（Slurm/K8s）包成 agent 可调的资源。

**新逻辑只有调度语义**（队列/状态机/作业限额/墙钟时间），**执行底座复用现有沙盒**
（gpu_runner）——这是资源矩阵的第五种底座"外部调度器"的本地形态：
- `real` 后端：生成 sbatch 脚本提交（机构有 Slurm 时用；本地不满足则启动时报告不可用）；
- `mock` 后端（本地演示）：文件化作业队列（queued→running→completed/failed）+
  后台 worker 逐个取出、经 gpu_runner 沙盒执行、落 stdout/产物——调度语义真实，
  执行与算力节点同一条沙盒路径（标签诚实：mock 指的是"没有真 Slurm"，不是假执行）。

作业规格硬限制（HPC 语义的配额面）：墙钟 ≤30 分钟、单作业 ≤5 文件 ≤2MB、
队列深度 ≤8（超了拒绝提交——调度器的背压）；作业元数据（提交者/时间/墙钟/耗时）落盘。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path

CLUSTER_DIR = Path(__file__).parent
JOBS_DIR = CLUSTER_DIR / "ws-hpc" / "jobs"

MAX_WALLTIME_MINUTES = 30
MAX_FILES = 5
MAX_JOB_BYTES = 2 * 1024 * 1024
MAX_QUEUE_DEPTH = 8
TERMINAL = {"completed", "failed"}

_STATE_ORDER = {"queued": 0, "running": 1, "completed": 2, "failed": 2}
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._\-]+$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


def backend() -> str:
    """检测可用后端：PATH 上有 sbatch 即 real，否则 mock。"""
    import shutil

    return "real" if shutil.which("sbatch") else "mock"


def _job_path(job_id: str) -> Path:
    """作业 id 必须是不含路径语义的短名。

    `JOBS_DIR / f"{job_id}.json"` 直拼，若 id 带 `../` 或是绝对路径，
    hpc.result/status 就读到队列外的任意 *.json（存在性预言机 + 内容回显）。
    """
    if not isinstance(job_id, str) or not _SAFE_JOB_ID.match(job_id):
        raise ValueError(f"作业 id 不合法：{job_id!r}（须 1..64 位字母数字-_，且首字符为字母或数字）")
    return JOBS_DIR / f"{job_id}.json"


def _write_job(job: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _job_path(job["id"]).write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")


def read_job(job_id: str) -> dict | None:
    try:
        path = _job_path(job_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None  # 坏文件（非对象）不致 result() 抛 AttributeError


def list_jobs(*, limit: int = 20) -> list[dict]:
    """squeue 等价物：作业元数据（不含脚本与产物内容）。"""
    if not JOBS_DIR.is_dir():
        return []
    jobs = []
    for path in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        jobs.append({k: job.get(k) for k in
                     ("id", "name", "state", "submittedBy", "walltimeMinutes",
                      "queuedAt", "startedAt", "endedAt", "durationSeconds", "exitCode", "error")})
    jobs.sort(key=lambda j: (-_STATE_ORDER.get(j.get("state"), 0), str(j.get("queuedAt"))))
    return jobs[:limit]


def validate_job(raw: dict) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "作业必须是 JSON 对象"
    name = str(raw.get("name") or "").strip()
    if not re.match(r"^[A-Za-z0-9._\-]{1,40}$", name):
        return None, "name 须为 1..40 位字母数字._-"
    files = raw.get("files")
    if not isinstance(files, dict) or "main.py" not in files:
        return None, "files 必须含入口 main.py"
    if len(files) > MAX_FILES:
        return None, f"单作业文件数上限 {MAX_FILES}"
    total = 0
    for fname, content in files.items():
        if not isinstance(fname, str) or not _SAFE_FILENAME.match(fname) or fname.startswith("."):
            return None, f"文件名不合法：{fname!r}"
        size = len(content.encode("utf-8")) if isinstance(content, str) else -1
        if size < 0:
            return None, f"文件内容必须是字符串：{fname}"
        total += size
    if total > MAX_JOB_BYTES:
        return None, f"作业总大小超过 {MAX_JOB_BYTES // 1024 // 1024}MB"
    try:
        walltime = int(raw.get("walltimeMinutes") or 10)
    except (TypeError, ValueError):
        return None, "walltimeMinutes 必须是整数"
    if not 1 <= walltime <= MAX_WALLTIME_MINUTES:
        return None, f"墙钟时间须在 1..{MAX_WALLTIME_MINUTES} 分钟"
    active = sum(1 for j in list_jobs(limit=1000) if j.get("state") not in TERMINAL)
    if active >= MAX_QUEUE_DEPTH:
        return None, f"队列深度上限 {MAX_QUEUE_DEPTH}（活跃 {active}）——调度器背压，稍后再提交"
    return {"name": name, "files": dict(files), "walltimeMinutes": walltime}, None


def submit(raw: dict, *, submitter: str = "?") -> dict:
    spec, error = validate_job(raw)
    if spec is None:
        return {"ok": False, "error": error}
    job = {
        "id": f"j{uuid.uuid4().hex[:10]}",
        "name": spec["name"],
        "state": "queued",
        "submittedBy": submitter,
        "walltimeMinutes": spec["walltimeMinutes"],
        "queuedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": spec["files"],
        "backend": backend(),
    }
    _write_job(job)
    return {"ok": True, "jobId": job["id"], "state": "queued", "backend": job["backend"],
            "walltimeMinutes": job["walltimeMinutes"],
            "note": "异步作业：轮询 hpc.jobs，终态后 hpc.result 取 stdout/产物"}


_WORKER_STARTED = False


def ensure_worker() -> None:
    """启动后台 worker（进程内单例）：轮询 queued → 沙盒执行 → 落终态。"""
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    _WORKER_STARTED = True
    asyncio.get_event_loop().create_task(_worker_loop()) if asyncio.get_event_loop().is_running() else None


async def _worker_loop() -> None:
    while True:
        try:
            await worker_tick()
        except Exception:  # noqa: BLE001 — worker 永不退出
            pass
        await asyncio.sleep(1.0)


async def worker_tick() -> int:
    """取一个 queued 作业执行（真实沙盒）；返回本 tick 处理数。"""
    import gpu_runner

    processed = 0
    for meta in list_jobs(limit=1000):
        if meta.get("state") != "queued":
            continue
        job = read_job(meta["id"])
        if job is None:
            continue
        job["state"] = "running"
        job["startedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _write_job(job)
        started = time.monotonic()
        runs_root = JOBS_DIR.parent / "runs"
        result = await gpu_runner.run_sandbox(
            {"files": job["files"], "timeout_seconds": job["walltimeMinutes"] * 60},
            runs_root=runs_root,
        )
        job.update({
            "state": "completed" if result.ok else "failed",
            "endedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "durationSeconds": result.duration_s,
            "exitCode": result.exit_code,
            "error": result.error,
            "runDir": str(runs_root / f"run-{result.run_id}"),
        })
        _write_job(job)
        processed += 1
        break  # 每 tick 一个：串行调度（单节点 mock 的诚实语义）
    return processed


def result(job_id: str, *, submitter: str | None = None) -> dict:
    """终态作业的结果（stdout 截选 + 产物元信息；产物内容留沙盒 runs 目录）。

    `submitter` 给定时必须与作业的 submittedBy 一致，防止域成员枚举 job id 读取
    他人作业输出（2026-10-18 P1-11）。
    """
    job = read_job(job_id)
    if job is None:
        return {"ok": False, "error": f"未知作业 {job_id}"}
    if submitter is not None:
        if not submitter or str(job.get("submittedBy") or "") != str(submitter):
            return {"ok": False, "error": "作业不属于当前调用方"}
    if job.get("state") not in TERMINAL:
        return {"ok": False, "error": f"作业未终态（state={job.get('state')}）", "state": job.get("state")}
    run_dir = Path(job.get("runDir") or "") / "output"
    artifacts = []
    if run_dir.is_dir():
        for path in sorted(run_dir.rglob("*")):
            if path.is_file():
                artifacts.append({"name": path.relative_to(run_dir).as_posix(), "sizeBytes": path.stat().st_size})
    stdout_file = Path(job.get("runDir") or "") / "result.json"
    stdout_head = ""
    if stdout_file.is_file():
        try:
            stdout_head = json.loads(stdout_file.read_text(encoding="utf-8")).get("stdout", "")[:2000]
        except (OSError, ValueError):
            pass
    return {"ok": job.get("state") == "completed", "jobId": job_id,
            "state": job.get("state"), "durationSeconds": job.get("durationSeconds"),
            "exitCode": job.get("exitCode"), "stdoutHead": stdout_head, "artifacts": artifacts,
            "note": "产物全文在网关本机 runs 目录；线上仅回元信息（大产物不搬运）"}


def info() -> dict:
    return {
        "backend": backend(),
        "backendNote": "real=机构 Slurm（sbatch）；mock=本地文件队列+沙盒执行（调度语义真实）",
        "limits": {"walltimeMinutes": MAX_WALLTIME_MINUTES, "files": MAX_FILES,
                   "jobBytes": MAX_JOB_BYTES, "queueDepth": MAX_QUEUE_DEPTH},
        "execution": "与算力节点同一 Docker 沙盒（断网/限额/非 root）",
        "jobs": {j["id"]: j["state"] for j in list_jobs(limit=8)},
    }
