"""后台作业台账（L2 受理-作业-取件交路）：`<ws>/jobs/<id>/` 目录式存储。

一个作业 = 一个目录：
    request.txt   原始任务文本
    status.json   {id, mode, state, created_at, started_at, finished_at, pid, exit_code, question_head}
    report.md     终稿（作业跑完后由 supervisor main 经 ACPS_JOB_REPORT_OUT 写入）
    log.txt       执行日志（stdout/stderr 合流）
state: queued → running → done / failed / interrupted
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ACTIVE_STATES = frozenset({"queued", "running"})
_TERMINAL_STATES = frozenset({"done", "failed", "interrupted"})
_DEFAULT_MAX_RUNTIME_S = 6 * 3600
_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_KEEP_LAST = 20
_DEFAULT_MAX_CONCURRENT = 2
_DEFAULT_POLL_HINT_SECONDS = 600
_DEFAULT_REPORT_PAGE_CHARS = 14000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone().strftime("%Y%m%d-%H%M%S")
    return f"job-{stamp}-{secrets.token_hex(2)}"


def create_job(root: Path, question: str, mode: str = "custom") -> tuple[str, Path]:
    job_id = new_job_id()
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    (job_dir / "request.txt").write_text(question, encoding="utf-8")
    _write_status(job_dir, {
        "id": job_id, "mode": mode, "state": "queued",
        "created_at": now_iso(), "started_at": None, "finished_at": None,
        "pid": None, "exit_code": None, "question_head": question[:120],
    })
    return job_id, job_dir


@contextlib.contextmanager
def _status_lock(job_dir: Path):
    """跨进程 per-job 状态锁：提交方、作业进程、取件方写状态时串行化。

    用操作系统文件锁而不是 lockfile 存在性：进程崩溃时内核自动释放，不会留下
    永久 stale lock。锁文件放在作业目录内，与唯一临时名配合，彻底关闭
    “读到 running → 判死 → 覆盖刚写入的 done” 的读后写窗口（R7）。
    """
    lock_path = job_dir / ".status.lock"
    handle = lock_path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _write_status_unlocked(job_dir: Path, status: dict) -> None:
    # 唯一临时名：提交方（写 pid）与作业进程（写 running/终态）会并发更新同一份
    # status.json，固定 `status.json.tmp` 会互相截断，读者可能读到半截 JSON。
    tmp = job_dir / f"status.json.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, job_dir / "status.json")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _write_status(job_dir: Path, status: dict) -> None:
    with _status_lock(job_dir):
        _write_status_unlocked(job_dir, status)


def read_status(job_dir: Path) -> dict:
    path = job_dir / "status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        # 不静默：状态损坏会让取件方永远看不到终稿，必须留痕（仍返回空字典容错）
        print(f"[jobs] 状态文件不可读 {path}: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return {}


def update_status(job_dir: Path, *, expected_states: frozenset[str] | None = None, **fields) -> dict:
    """读改写状态；expected_states 给定时只在当前状态仍属于该集合时写。

    用于 resolve_liveness 这类“先读后写”的收口：作业进程可能刚写完 done/failed，
    不能再用陈旧的 running 判断把终态覆盖成 interrupted（P1-16）。
    """
    with _status_lock(job_dir):
        st = read_status(job_dir)
        if expected_states is not None and st.get("state") not in expected_states:
            return st
        st.update(fields)
        _write_status_unlocked(job_dir, st)
        return st


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    return _pid_alive_posix(pid)


def _pid_alive_windows(pid: int) -> bool:
    """Windows 上**不能**用 os.kill(pid, 0) 探活：Python 对非 CTRL_* 信号会直接
    TerminateProcess(pid, sig)——sig=0 等于把作业进程杀掉。这里走 OpenProcess。"""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # 权限不足通常说明进程存在（只是不归我们查）；其余错误按不存在处理
        return ctypes.get_last_error() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # Linux: 僵尸进程仍能被 os.kill(pid, 0) 命中，但已不会再写任何结果 → 视为已结束
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
        state = stat.rsplit(b")", 1)[-1].split(maxsplit=1)[0]
        return state != b"Z"
    except (OSError, IndexError):
        return True


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def max_runtime_seconds() -> int:
    return _env_int("ACPS_JOB_MAX_RUNTIME", _DEFAULT_MAX_RUNTIME_S)


def max_concurrent() -> int:
    return _env_int("ACPS_JOB_MAX_CONCURRENT", _DEFAULT_MAX_CONCURRENT)


def suggested_poll_seconds() -> int:
    """给调用方的建议查询间隔（代码给出的提示，不是对完成时间的估算）。"""
    return _env_int("ACPS_JOB_POLL_HINT_SECONDS", _DEFAULT_POLL_HINT_SECONDS)


def report_page_chars() -> int:
    """check_job 单次返回的终稿字符数；超出用 offset 分段取。"""
    return _env_int("ACPS_JOB_REPORT_PAGE_CHARS", _DEFAULT_REPORT_PAGE_CHARS)


def resolve_liveness(job_dir: Path) -> dict:
    """未收尾但进程已死 → interrupted；活过 TTL 的也收口（取件方读取时顺手修复）。

    TTL 只改台账状态、不动进程：若进程随后正常收尾，jobs_runner 会把状态覆写成
    done/failed。没有 TTL 时，pid 活着但任务卡死的作业会永远挂在 running。
    """
    st = read_status(job_dir)
    if st.get("state") not in _ACTIVE_STATES:
        return st
    if st.get("pid") and not pid_alive(st.get("pid")):
        return update_status(job_dir, expected_states=_ACTIVE_STATES,
                             state="interrupted", finished_at=now_iso(),
                             reason="process-exited")
    elapsed = elapsed_seconds(st)
    if elapsed is not None and elapsed > max_runtime_seconds():
        return update_status(job_dir, expected_states=_ACTIVE_STATES,
                             state="interrupted", finished_at=now_iso(),
                             reason="ttl-exceeded")
    return st


def job_report(job_dir: Path) -> str | None:
    path = job_dir / "report.md"
    if not path.is_file():
        return None
    last: Exception | None = None
    for _ in range(3):
        try:
            text = path.read_text(encoding="utf-8")
            return text if text.strip() else None
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
            return text if text.strip() else None
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    print(f"[jobs] 终稿读取失败 {path}: {type(last).__name__}: {last}",
          file=sys.stderr, flush=True)
    return None


def report_present(job_dir: Path) -> bool:
    """终稿是否已落盘（按文件存在且非空判定，不依赖能否读出内容）。"""
    try:
        return (job_dir / "report.md").stat().st_size > 0
    except OSError:
        return False


def log_tail(job_dir: Path, n: int = 40) -> str:
    p = job_dir / "log.txt"
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _job_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [
        d for d in sorted(root.iterdir(), reverse=True)
        if d.is_dir() and (d / "status.json").is_file()
    ]


_JOB_REF_RE = re.compile(r"^[A-Za-z0-9-]+$")


def _inside_root(root: Path, candidate: Path) -> bool:
    """候选目录解析后必须仍在 jobs 根内（防 symlink 逃逸；`..` 已由白名单挡住）。"""
    try:
        return candidate.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def match_jobs(root: Path, ref: str) -> list[Path]:
    """按作业号/前缀找作业目录；多个前缀命中时全部返回（调用方必须处理歧义）。

    `ref` 是模型工具参数，**不能**直接拼路径：作业号只含字母/数字/连字符，先过白名单，
    再用 resolve() 兜底确认候选目录仍在 jobs 根内——否则 `../../ws-victim/jobs/...`
    或绝对路径可以越出 jobs/ 读到其他工作区的 status/report/log。
    """
    ref = (ref or "").strip()
    if not ref or not _JOB_REF_RE.fullmatch(ref) or not root.is_dir():
        return []
    try:
        root = root.resolve()
        children = sorted(root.iterdir(), reverse=True)
    except OSError:
        return []
    exact = root / ref
    if _inside_root(root, exact) and (exact / "status.json").is_file():
        return [exact]
    return [
        d for d in children
        if d.name.startswith(ref) and (d / "status.json").is_file() and _inside_root(root, d)
    ]


def find_job(root: Path, ref: str) -> Path | None:
    """唯一命中才返回；前缀有歧义返回 None（宁可要更长的号，也不猜错作业）。"""
    matches = match_jobs(root, ref)
    return matches[0] if len(matches) == 1 else None


def list_jobs(root: Path, limit: int = 10) -> list[dict]:
    out: list[dict] = []
    for d in _job_dirs(root)[:limit]:
        st = read_status(d)
        st["job_id"] = d.name
        out.append(st)
    return out


def count_active(root: Path) -> int:
    return sum(1 for d in _job_dirs(root)
               if resolve_liveness(d).get("state") in _ACTIVE_STATES)


def prune_jobs(root: Path, *, keep_days: int | None = None,
               keep_last: int | None = None) -> int:
    """清理终态作业：保留最近 keep_last 个，其余 finished_at 超过 keep_days 的删除。"""
    if not root.is_dir():
        return 0
    keep_days = (_env_int("ACPS_JOB_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS)
                 if keep_days is None else keep_days)
    keep_last = (_env_int("ACPS_JOB_KEEP_LAST", _DEFAULT_KEEP_LAST)
                 if keep_last is None else keep_last)
    terminal = [(d, st) for d, st in ((d, resolve_liveness(d)) for d in _job_dirs(root))
                if st.get("state") in _TERMINAL_STATES]
    keep = {str(d) for d, _ in terminal[:keep_last]}
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for d, st in terminal:
        if str(d) in keep:
            continue
        stamp = st.get("finished_at") or st.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(str(stamp)).timestamp()
        except (TypeError, ValueError):
            try:
                ts = d.stat().st_mtime
            except OSError:
                ts = 0.0
        if ts > cutoff:
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            removed += 1
    if removed:
        print(f"[jobs] 清理终态作业 {removed} 个（保留最近 {keep_last} 个 / {keep_days} 天）",
              flush=True)
    return removed


def elapsed_seconds(st: dict) -> float | None:
    start = st.get("started_at") or st.get("created_at")
    if not start:
        return None
    try:
        t0 = datetime.fromisoformat(str(start))
    except ValueError:
        return None
    end_raw = st.get("finished_at")
    try:
        t1 = datetime.fromisoformat(str(end_raw)) if end_raw else datetime.now(timezone.utc)
        return max(0.0, (t1 - t0).total_seconds())
    except (TypeError, ValueError):
        return None
