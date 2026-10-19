#!/usr/bin/env python3
"""修正 job worker 独立 workspace 方案的四处置信问题（幂等，带备份）。

改动：
  1. jobs_runner._job_workspace：不再整目录 symlink，只复制小体积身份材料，
     任务库（aip-tasks.sqlite3，208MB+）留给 job 自己的空库；discovered/ 用 symlink 共享。
  2. jobs_runner._acquire_job_lock：flock 改为**非阻塞 + 超时**，避免一个卡死的
     job 永久占锁（实测已发生）。
  3. supervisor._deliver_dir()：交付目录改为运行时取 WORKSPACE，
     否则作业仍读常驻 supervisor 的 deliver/。
  4. supervisor._jobs_root()：作业目录固定到 ws-supervisor/jobs，
     否则作业模式下 check_job 去 ws-supervisor-job/jobs 找不到任务。

    python3 deploy/patch_job_workspace.py [--revert]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

CLUSTER = Path("/opt/wit/my-project/acps_cluster")
SUPERVISOR = CLUSTER / "supervisor.py"
RUNNER = CLUSTER / "jobs_runner.py"

# ── supervisor.py ────────────────────────────────────────────────────────────

OLD_DELIVER = 'DELIVER_DIR = WORKSPACE / "deliver"'
NEW_DELIVER = '''def _deliver_dir() -> Path:
    """交付目录：**运行时**取 WORKSPACE，不固化成模块常量。

    作业 worker 会把 supervisor.WORKSPACE 换成独立 workspace；若写成模块级常量，
    作业仍会去读常驻 supervisor 的 deliver/（2026-10-19 实测）。
    """
    return WORKSPACE / "deliver"'''

OLD_DELIVER_USE1 = 'root = deliver or DELIVER_DIR'
NEW_DELIVER_USE1 = 'root = deliver or _deliver_dir()'

OLD_DELIVER_USE2 = 'sent_dir = (deliver or DELIVER_DIR) / "sent"'
NEW_DELIVER_USE2 = 'sent_dir = (deliver or _deliver_dir()) / "sent"'

OLD_JOBS = '    jobs_root = WORKSPACE / "jobs"'
NEW_JOBS = '    jobs_root = _jobs_root()'

JOBS_HELPER = '''def _jobs_root() -> Path:
    """作业目录固定在常驻 supervisor 的 workspace 下。

    job worker 的 WORKSPACE 被换成独立目录后，若这里仍用 WORKSPACE，
    `check_job` 会去 `ws-supervisor-job/jobs` 找**真实存在于** `ws-supervisor/jobs`
    的任务，必然"查不到"（2026-10-19 实测）。
    """
    return Path(__file__).parent / "ws-supervisor" / "jobs"


'''

# ── jobs_runner.py ───────────────────────────────────────────────────────────

NEW_WORKSPACE_FN = '''def _job_workspace() -> Path:
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
'''

OLD_WORKSPACE_START = 'def _job_workspace() -> Path:'
OLD_LOCK_START = 'def _acquire_job_lock(path: Path):'
OLD_MAIN_START = 'def main() -> int:'


def patch_supervisor(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if "_deliver_dir()" not in text:
        if OLD_DELIVER not in text:
            raise SystemExit("supervisor.py: 找不到 DELIVER_DIR 定义，放弃")
        text = text.replace(OLD_DELIVER, NEW_DELIVER, 1)
        notes.append("DELIVER_DIR → _deliver_dir()")
        if OLD_DELIVER_USE1 in text:
            text = text.replace(OLD_DELIVER_USE1, NEW_DELIVER_USE1, 1)
        if OLD_DELIVER_USE2 in text:
            text = text.replace(OLD_DELIVER_USE2, NEW_DELIVER_USE2, 1)

    if "_jobs_root()" not in text:
        if text.count(OLD_JOBS) != 2:
            raise SystemExit(f"supervisor.py: jobs_root 出现 {text.count(OLD_JOBS)} 次（预期 2），放弃")
        text = text.replace(OLD_JOBS, NEW_JOBS)
        # 把 helper 放在第一个用到它的函数之前
        anchor = "def _make_submit_job_tool() -> Tool:"
        if anchor not in text:
            raise SystemExit("supervisor.py: 找不到 _make_submit_job_tool 锚点")
        text = text.replace(anchor, JOBS_HELPER + anchor, 1)
        notes.append("jobs_root → _jobs_root()（固定到 ws-supervisor/jobs）")

    if "import shutil" not in text and False:
        pass
    return text, notes


def patch_runner(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    if NEW_WORKSPACE_FN in text:
        return text, ["已是最新，跳过"]

    start = text.find(OLD_WORKSPACE_START)
    lock = text.find(OLD_LOCK_START)
    main_at = text.find(OLD_MAIN_START)
    if not (0 < start < lock < main_at):
        raise SystemExit("jobs_runner.py: 锚点顺序异常，放弃")

    text = text[:start] + NEW_WORKSPACE_FN + "\n\n" + text[main_at:]
    notes.append("_job_workspace/_acquire_job_lock 已替换")

    if "\nimport shutil\n" not in text:
        text = text.replace("\nimport sys\n", "\nimport shutil\nimport sys\n", 1)
        notes.append("+import shutil")
    if "\nimport time\n" not in text:
        text = text.replace("\nimport traceback\n", "\nimport time\nimport traceback\n", 1)
        notes.append("+import time")
    return text, notes


def main() -> int:
    revert = "--revert" in sys.argv

    if revert:
        for p in (SUPERVISOR, RUNNER):
            bak = p.with_suffix(p.suffix + ".bak-jobws")
            if bak.is_file():
                shutil.copy2(bak, p)
                print(f"已回滚 {p.name}")
        return 0

    for p in (SUPERVISOR, RUNNER):
        bak = p.with_suffix(p.suffix + ".bak-jobws")
        if not bak.exists():
            shutil.copy2(p, bak)
            print(f"备份 {p.name} → {bak.name}")

    for path, fn in ((SUPERVISOR, patch_supervisor), (RUNNER, patch_runner)):
        text = path.read_text(encoding="utf-8")
        new_text, notes = fn(text)
        if new_text == text:
            print(f"  {path.name}: 无需改动")
            continue
        try:
            compile(new_text, str(path), "exec")
        except SyntaxError as exc:
            raise SystemExit(f"✘ {path.name} 改后语法错误: line {exc.lineno} {exc.msg}")
        path.write_text(new_text, encoding="utf-8")
        print(f"  {path.name}: {', '.join(notes)}")

    print("\n完成。语法已校验。")
    print("验证：python3 deploy/verify_job_workspace.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
