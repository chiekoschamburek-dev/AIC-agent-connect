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
import shutil
import sys
import threading
import time
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
    """Job worker 的独立 workspace：复用身份材料，但日志与任务库独立。

    **为什么要独立**：常驻 supervisor 与 job worker 共用
    `ws-supervisor/logs/amp_access_<AIC>.jsonl` 会触发 wit 的 AmpPersistenceError
    —— 那份 journal 有历史链校验，两个进程并发 append 必炸
    （2026-10-19 11:17 / 11:19 两个 job 实测崩在这里）。

    **为什么不整目录 symlink**：`acps/` 里含 `aip-tasks.sqlite3`（208MB + WAL），
    整目录共享等于让两个进程并发写同一个任务库。这里只复制小体积身份材料，
    任务库让 job 用自己的空库（首次由 SDK 自动创建）；`discovered/` 用 symlink
    共享（以读为主，双方都可能新增条目）。
    """
    base = CLUSTER / "ws-supervisor-job"
    base.mkdir(parents=True, exist_ok=True)
    src = CLUSTER / "ws-supervisor" / "acps"
    acps = base / "acps"
    if acps.exists():
        return base

    acps.mkdir(parents=True)
    copied: list[str] = []
    for entry in sorted(src.iterdir()):
        name = entry.name
        if name.startswith("aip-tasks.sqlite3"):
            continue                      # 任务库不共享（并发写）
        if ".pre-" in name or name.endswith(".bak") or ".bak-" in name:
            continue                      # 历史备份不复制
        if name == "discovered":
            continue                      # 单独做 symlink
        dst = acps / name
        try:
            if entry.is_dir():
                shutil.copytree(entry, dst)
            else:
                shutil.copy2(entry, dst)
            copied.append(name)
        except OSError as exc:
            raise SystemExit(f"复制 workspace 材料失败 {entry} -> {dst}: {exc}")

    disc_src = src / "discovered"
    if disc_src.is_dir():
        try:
            os.symlink(disc_src, acps / "discovered", target_is_directory=True)
        except OSError as exc:
            raise SystemExit(f"无法链接 discovered/: {exc}")

    (base / "logs").mkdir(exist_ok=True)
    (base / "tokens").mkdir(exist_ok=True)
    print(f"[job] 独立 workspace 就绪: {base}（复制 {len(copied)} 项，任务库独立）",
          flush=True)
    return base


def _acquire_job_lock(path: Path):
    """同一时间只允许一个 job worker 写独立 access journal。

    **必须带超时**：`flock(LOCK_EX)` 是无限阻塞的，一个卡死的 job 会让之后
    所有 job 永久等在锁上（2026-10-19 实测有个 job 卡了 12 分钟）。
    拿不到锁就放弃本次，而不是无声挂住。
    """
    fh = open(path, "a+", encoding="utf-8")
    try:
        import fcntl
    except ImportError:                   # 非 POSIX：退化无锁
        return fh
    limit = float(os.getenv("ACPS_JOB_LOCK_TIMEOUT", "15"))
    deadline = time.time() + limit
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if time.time() >= deadline:
                fh.close()
                raise SystemExit(
                    f"另一个 job worker 正占用 {path}，等待超过 {limit:.0f}s，本次放弃")
            time.sleep(0.5)


def main() -> int:
    job_dir = Path(sys.argv[1]).resolve()
    question = (job_dir / "request.txt").read_text(encoding="utf-8")
    jobs_store.update_status(job_dir, state="running", started_at=jobs_store.now_iso(),
                             pid=os.getpid())
    _load_production_env()  # 必须先于 import supervisor（bootstrap 只补缺、不覆盖）
    os.environ["ACPS_JOB_REPORT_OUT"] = str(job_dir / "report.md")
    os.environ["ACPS_SUPERVISOR_NO_SERVE"] = "1"  # 作业只做出站委派，不对外服务
    # 作业与常驻 supervisor 的 access journal 通过独立 workspace 隔离；
    # 不关闭 AmpAccessLoggingHooks，job worker 的 caller 侧 access 也要计入。
    # 关键：serve=False 不绑定端口，但 build_acps_config(port) 仍会用端口构造 ACS 端点；
    # 必须保持 enrollment 注册的 8444，否则会被判 acps_config_changed_after_submission。
    os.environ["ACPS_SUPERVISOR_PORT"] = "8444"
    sys.argv = ["supervisor.py", question]
    job_ws = _job_workspace()
    try:
        lock = _acquire_job_lock(job_ws / ".job.lock")
    except SystemExit as exc:
        # 拿不到锁必须立刻把状态置 failed：否则 status.json 永远停在 running，
        # 一直占着并发额度，后续 job 全被"假活跃"挤掉（2026-10-19 实测）。
        jobs_store.update_status(
            job_dir, state="failed", finished_at=jobs_store.now_iso(),
            run_status="lock-timeout", exit_code=1,
        )
        print(f"[job] {exc}", flush=True)
        return 1
    # ── 运行时长看门狗 ────────────────────────────────────────────────
    # 没有它：作业卡在某个 AIP 调用上会一直持有 .job.lock，后续作业全超时
    # （2026-10-19 实测：一个作业挂了 57 分钟，日志停在启动两行）。
    # TTL 只在"有人读状态"时改台账，不杀进程，所以必须自己到点退出。
    _limit = jobs_store.max_runtime_seconds()

    def _watchdog() -> None:
        try:
            jobs_store.update_status(
                job_dir, state="failed", finished_at=jobs_store.now_iso(),
                run_status="ttl-exceeded", exit_code=124,
            )
        finally:
            print(f"[job] 运行超过 {_limit}s，强制终止（释放作业锁）", flush=True)
            os._exit(124)

    _watchdog_timer = threading.Timer(_limit, _watchdog)
    _watchdog_timer.daemon = True
    _watchdog_timer.start()
    print(f"[job] 看门狗已启动：上限 {_limit}s", flush=True)

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
