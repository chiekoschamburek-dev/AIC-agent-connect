"""后台作业执行器：由 supervisor 的 `submit_job` 工具以 detached 进程启动。

    python jobs_runner.py <job_dir>

- 读 `request.txt` 作为任务文本；终稿经 `ACPS_JOB_REPORT_OUT` 由 supervisor main 写进 `report.md`；
- 全程维护 `status.json`（running → done/failed）；stdout/stderr 由启动方重定向到 `log.txt`；
- 进程独立于常驻 supervisor（日志/状态都在自己的作业目录，互不干扰）；
- **自带生产环境加载**（deploy/.env.production 覆盖式，照 run_supervisor.sh 配方）：
  不依赖启动方的环境，手动/异常启动方（缺生产配置）也能跑通。
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from pathlib import Path

CLUSTER = Path(__file__).resolve().parent
sys.path.insert(0, str(CLUSTER))
sys.path.insert(0, str(CLUSTER.parent))

import jobs_store  # noqa: E402


def _load_production_env() -> None:
    env_file = CLUSTER / "deploy" / ".env.production"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


def final_state(report_present: bool, run_status: str) -> str:
    """done 只认"终稿落盘 + run 正常收尾"；残稿 + 撞顶/异常一律 failed。"""
    return "done" if report_present and run_status in ("completed", "success") else "failed"


def _job_workspace() -> Path:
    """Job worker 的独立 workspace：共享身份/ACS，但 access 日志独立。

    常驻 supervisor 与 job worker 共用 `ws-supervisor/logs/amp_access_<AIC>.jsonl`
    会触发 wit 的 AmpPersistenceError。这里让 worker 使用
    `ws-supervisor-job/logs/`，`amp_access_sender` 的 `ws-*/logs` glob 会自动扫到；
    `acps/` 用 symlink 复用同一身份/enrollment/证书，不复制 400MB 任务库。
    """
    base = CLUSTER / "ws-supervisor-job"
    base.mkdir(parents=True, exist_ok=True)
    acps = base / "acps"
    if not acps.exists():
        target = CLUSTER / "ws-supervisor" / "acps"
        try:
            os.symlink(target, acps, target_is_directory=True)
        except OSError as exc:
            raise SystemExit(f"无法创建 job workspace symlink {acps} -> {target}: {exc}")
    (base / "logs").mkdir(exist_ok=True)
    (base / "tokens").mkdir(exist_ok=True)
    return base


def _acquire_job_lock(path: Path):
    """同一时间只允许一个 job worker 写独立 access journal。"""
    fh = open(path, "a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        # 非 POSIX 环境退化为无锁；生产服务器是 Linux。
        pass
    return fh


def main() -> int:
    job_dir = Path(sys.argv[1]).resolve()
    question = (job_dir / "request.txt").read_text(encoding="utf-8")
    jobs_store.update_status(job_dir, state="running", started_at=jobs_store.now_iso(),
                             pid=os.getpid())
    _load_production_env()  # 必须先于 import supervisor（agents.py 只补缺、不覆盖）
    os.environ["ACPS_JOB_REPORT_OUT"] = str(job_dir / "report.md")
    os.environ["ACPS_SUPERVISOR_NO_SERVE"] = "1"  # 作业只做出站委派，不对外服务
    # 作业与常驻 supervisor 的 access journal 通过独立 workspace 隔离；
    # 不关闭 AmpAccessLoggingHooks，job worker 的 caller 侧 access 也要计入。
    # 关键：serve=False 不绑定端口，但 build_acps_config(port) 仍会用端口构造 ACS 端点；
    # 必须保持 enrollment 注册的 8444，否则会被判 acps_config_changed_after_submission。
    os.environ["ACPS_SUPERVISOR_PORT"] = "8444"
    sys.argv = ["supervisor.py", question]
    job_ws = _job_workspace()
    lock = _acquire_job_lock(job_ws / ".job.lock")
    rc = 0
    result: dict = {}
    try:
        import supervisor
        # 身份/ACS 走 symlink；logs/access 走独立目录，避免与常驻 supervisor 抢 journal。
        supervisor.WORKSPACE = job_ws
        result = asyncio.run(supervisor.main()) or {}
    except BaseException as exc:  # noqa: BLE001
        rc = 1
        traceback.print_exc()
        print(f"[job] 执行异常: {type(exc).__name__}: {exc}", flush=True)
        result = {"status": "exception", "iterations": 0}
    finally:
        try:
            lock.close()
        except OSError:
            pass
    if not isinstance(result, dict):
        result = {}
    run_status = str(result.get("status") or "") or "unknown"
    iterations = result.get("iterations")
    # 终稿按"文件存在且非空"判定（读取可能瞬时失败，不该因此丢掉已落盘的终稿）；
    # 但 done 必须同时满足 run 正常收尾——失败但带残稿的 run 只记 failed。
    state = final_state(jobs_store.report_present(job_dir), run_status)
    if state != "done":
        rc = rc or 1
    jobs_store.update_status(
        job_dir, state=state, finished_at=jobs_store.now_iso(), exit_code=rc,
        run_status=run_status, iterations=iterations,
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
