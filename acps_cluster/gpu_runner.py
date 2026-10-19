"""GPU 沙盒执行器：把一次实验代码放进受硬限制的 Docker 容器里跑（partner_gpu 的执行层）。

沙盒八条硬边界（声明即边界，全部在本模块强制，PEP 之外的第二道防线）：
1. 镜像白名单：只允许预拉到本地的 tag（Docker Hub 直连不通，禁止现场拉取任意镜像）；
2. --network none 断网：防数据外传/下载；
3. 资源限额：--memory 7g --cpus 4 --shm-size 2g --gpus device=0（只给这一张卡）；
4. 文件系统：--read-only + tmpfs /tmp；input/ 与 datasets/ 只读挂载，output/ 可写；
5. 权限：--cap-drop ALL --security-opt no-new-privileges --user 1000:1000（非 root）；
6. 进程：--pids-limit 256（防 fork 炸弹）；
7. 时限：默认 600s、上限 1800s，超时 docker kill；
8. 大小限额：单文件 ≤10MB、任务总输入 ≤50MB、产物 ≤50MB、stdout/stderr 截断保留头尾。

文件上传三档（AIP 的 file 数据项走 partner 侧落盘后进 input/；本模块只吃已校验的 JobSpec）：
代码/小文件内联（files），大数据集由 gpu.data.fetch 宿主侧代下（沙盒外有网）后只读挂 /data。

数据集代下（fetch_dataset）的信任边界：下载发生在**宿主侧（沙盒外）**，是一个独立的、
需授权、有限额的操作——容器内依然 network=none，八条沙盒硬边界不受影响。硬控制：
- 仅 https；例外：回环地址允许 http（本地演示/自托管数据源）
- 可选域名白名单（环境变量 ACPS_GPU_DATA_DOMAINS，逗号分隔，缺省放行全部 https）
- 单数据集大小上限（默认 2GB）；压缩包解压时二次限幅 + 防路径穿越/符号链接
- 提供 sha256 则强校验；落盘写审计 JSONL（ws-gpu/logs/data-audit.jsonl）
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse

from arg_shapes import as_list  # 工具入参数组宽容化（provider 把 array 送成 JSON 字符串）

CLUSTER_DIR = Path(__file__).parent
DATASETS_DIR = CLUSTER_DIR / "datasets"
RUNS_DIR = CLUSTER_DIR / "ws-gpu" / "runs"

ALLOWED_IMAGES: tuple[str, ...] = ("pytorch:cu128",)  # 预拉到本地的白名单 tag
ALLOWED_EXECUTABLES: tuple[str, ...] = ("python", "python3")
DEFAULT_IMAGE = "pytorch:cu128"

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_ARTIFACT_TEXT_BYTES = 64 * 1024  # 产物以文本回传的单文件上限（超过只回元信息）
STDOUT_KEEP_HEAD = 32 * 1024
STDOUT_KEEP_TAIL = 8 * 1024
DEFAULT_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 1800

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._\-]+$")


@dataclass(frozen=True)
class JobSpec:
    image: str
    entrypoint: tuple[str, ...]
    files: dict[str, str]
    timeout_seconds: int
    use_gpu: bool = True  # cpu-partner 复用同一沙盒但透传不给卡（由调用方伙伴决定，模型不可选）


@dataclass
class JobResult:
    ok: bool
    run_id: str
    exit_code: int | None
    timed_out: bool
    duration_s: float
    stdout: str
    stderr: str
    artifacts: list[dict] = field(default_factory=list)  # {name, sizeBytes, truncated}
    error: str | None = None


def _clip(text: str, *, head: int = STDOUT_KEEP_HEAD, tail: int = STDOUT_KEEP_TAIL) -> str:
    if len(text) <= head + tail + 64:
        return text
    return f"{text[:head]}\n…[中段省略 {len(text) - head - tail} 字符]…\n{text[-tail:]}"


STREAM_CAPTURE_LIMIT = 2 * 1024 * 1024  # 2MB：超过部分边读边丢，防 communicate() 撑爆宿主内存


async def _read_capped(
    stream: asyncio.StreamReader | None,
    limit: int = STREAM_CAPTURE_LIMIT,
) -> tuple[bytes, int, bool]:
    if stream is None:
        return b"", 0, False
    kept = bytearray()
    total = 0
    while True:
        chunk = await stream.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        remaining = limit - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
    return bytes(kept), total, total > len(kept)


async def communicate_capped(
    proc: asyncio.subprocess.Process,
    limit: int = STREAM_CAPTURE_LIMIT,
) -> tuple[bytes, bytes, int, int, bool, bool]:
    """并发读取 stdout/stderr，超过上限只保留前 limit 字节并继续 drain。"""
    (out, out_total, out_trunc), (err, err_total, err_trunc) = await asyncio.gather(
        _read_capped(proc.stdout, limit),
        _read_capped(proc.stderr, limit),
    )
    await proc.wait()
    return out, err, out_total, err_total, out_trunc, err_trunc


def validate_job(raw: dict, *, use_gpu: bool = True) -> tuple[JobSpec | None, str | None]:
    """模型提议、代码裁决：所有硬限制在此一次性校验，返回 (spec, 错误)。"""
    if not isinstance(raw, dict):
        return None, "任务必须是 JSON 对象"

    image = str(raw.get("image") or DEFAULT_IMAGE)
    if image not in ALLOWED_IMAGES:
        return None, f"镜像必须在白名单内：{', '.join(ALLOWED_IMAGES)}（不允许现场拉取）"

    entrypoint_raw = as_list(raw.get("entrypoint")) or ["python", "/workspace/input/main.py"]
    if not isinstance(entrypoint_raw, list) or not entrypoint_raw:
        return None, "entrypoint 必须是非空数组（列表形式，不经 shell）"
    entrypoint = tuple(str(arg) for arg in entrypoint_raw)
    if entrypoint[0] not in ALLOWED_EXECUTABLES:
        return None, f"entrypoint 首个可执行文件只允许：{', '.join(ALLOWED_EXECUTABLES)}"
    if any(re.search(r"[;&|`$<>]", arg) for arg in entrypoint):
        return None, "entrypoint 参数不允许 shell 元字符（容器内也不经 shell，双保险）"

    # 入口 .py 必须在**只读代码挂载点**下：容器 CWD 是可写的 /workspace/output，
    # 模型给的 `["python3","main.py"]` 会在那里找不到文件而 exit=2（2026-10-18 实测：
    # 5 次沙箱执行全部因此失败，伙伴却把根因报成"Docker 不可用"）。
    # 裸文件名就地补全为 /workspace/input/<name>；指向别处的绝对路径直接拒。
    normalized: list[str] = []
    for arg in entrypoint[1:]:
        if arg.endswith(".py"):
            if arg.startswith("/"):
                if not arg.startswith("/workspace/input/"):
                    return None, (f"入口脚本必须在只读挂载点 /workspace/input/ 下（收到 {arg}）；"
                                  "代码文件请直接用文件名，如 main.py")
            else:
                arg = f"/workspace/input/{arg.lstrip('./')}"
        normalized.append(arg)
    entrypoint = (entrypoint[0], *normalized)

    files = raw.get("files")
    if not isinstance(files, dict) or not files:
        return None, "files 必须是非空对象：{文件名: 文件内容}，至少包含 main.py"
    total = 0
    for name, content in files.items():
        if not isinstance(name, str) or not _SAFE_FILENAME.match(name) or name.startswith("."):
            return None, f"文件名不合法（只允许字母数字._- 且非隐藏文件）：{name!r}"
        size = len(content.encode("utf-8")) if isinstance(content, str) else -1
        if size < 0:
            return None, f"文件内容必须是字符串（base64 传二进制）：{name}"
        if size > MAX_FILE_BYTES:
            return None, f"文件 {name} 超过单文件上限 {MAX_FILE_BYTES // 1024 // 1024}MB"
        total += size
    if total > MAX_TOTAL_INPUT_BYTES:
        return None, f"输入总大小超过上限 {MAX_TOTAL_INPUT_BYTES // 1024 // 1024}MB"
    if "main.py" not in files and not any(n.endswith(".py") for n in files):
        return None, "files 里至少要有一个 .py 入口文件（约定 main.py）"

    try:
        timeout_seconds = int(raw.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        return None, "timeout_seconds 必须是整数"
    timeout_seconds = max(30, min(timeout_seconds, MAX_TIMEOUT_SECONDS))

    return (
        JobSpec(image=image, entrypoint=entrypoint, files=dict(files), timeout_seconds=timeout_seconds,
                use_gpu=use_gpu),
        None,
    )


def docker_available() -> bool:
    """Docker Desktop 探活（Windows 上它是 GPU 直通的唯一路径）。"""
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_docker_args(spec: JobSpec, run_id: str, input_dir: Path, output_dir: Path) -> list[str]:
    args = [
        "docker", "run", "--rm",
        f"--name=gpu-run-{run_id}",
        "--network", "none",
        "--memory", "7g",
        "--cpus", "4",
        "--shm-size", "2g",
        "--pids-limit", "256",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=512m",
        "--user", "1000:1000",
        "-v", f"{input_dir}:/workspace/input:ro",
        "-v", f"{output_dir}:/workspace/output:rw",
        "-w", "/workspace/output",
        "--stop-timeout", "5",
    ]
    if spec.use_gpu:
        args += ["--gpus", "device=0"]
    if DATASETS_DIR.is_dir():
        args += ["-v", f"{DATASETS_DIR}:/data:ro"]
    return args + [spec.image, *spec.entrypoint]


def _collect_artifacts(output_dir: Path) -> list[dict]:
    """收集产物元信息；小的文本/图片直接带内容（文本内联，png base64）。

    output/ 是容器唯一可写挂载，因此必须假定里面的名字由被执行的代码决定：
    符号链接/目录逃逸会让宿主把挂载点之外的文件（评测标签、私钥、其他租户产物）读回来，
    且 `stat()` 跟随链接会让几十字节的链接本身冒充"小文件"绕过大小限额。
    """
    artifacts = []
    root = output_dir.resolve()
    for path in sorted(output_dir.rglob("*")):
        if path.is_symlink():
            continue
        if not path.is_file():
            continue
        if root not in path.resolve().parents:
            continue
        size = path.lstat().st_size
        item = {"name": path.relative_to(output_dir).as_posix(), "sizeBytes": size}
        if size <= MAX_ARTIFACT_TEXT_BYTES:
            suffix = path.suffix.lower()
            try:
                if suffix in {".json", ".csv", ".txt", ".log", ".md", ".yaml", ".yml"}:
                    item["content"] = path.read_text(encoding="utf-8", errors="replace")
                elif suffix == ".png":
                    import base64

                    item["content"] = base64.b64encode(path.read_bytes()).decode("ascii")
                    item["encoding"] = "base64"
            except OSError:
                pass
        artifacts.append(item)
    return artifacts


async def run_sandbox(raw: dict, *, runs_root: Path = RUNS_DIR, use_gpu: bool = True) -> JobResult:
    """校验 → 落盘 → 容器执行（超时杀）→ 收集产物。任何失败都收敛为 JobResult 而非异常。"""
    spec, error = validate_job(raw, use_gpu=use_gpu)
    run_id = uuid.uuid4().hex[:10]
    if spec is None:
        return JobResult(ok=False, run_id=run_id, exit_code=None, timed_out=False,
                         duration_s=0.0, stdout="", stderr="", error=error)
    if not docker_available():
        return JobResult(ok=False, run_id=run_id, exit_code=None, timed_out=False,
                         duration_s=0.0, stdout="", stderr="",
                         error="Docker 不可用（Docker Desktop 未启动？GPU 直通依赖它）")

    run_dir = runs_root / f"run-{run_id}"
    input_dir = run_dir / "input"
    output_dir = run_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in spec.files.items():
        (input_dir / name).write_text(content, encoding="utf-8")

    started = time.monotonic()
    timed_out = False
    proc = await asyncio.create_subprocess_exec(
        *build_docker_args(spec, run_id, input_dir, output_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        (
            stdout_b, stderr_b, _out_total, _err_total, out_trunc, err_trunc,
        ) = await asyncio.wait_for(
            communicate_capped(proc), timeout=spec.timeout_seconds
        )
    except asyncio.TimeoutError:
        timed_out = True
        await asyncio.create_subprocess_exec("docker", "kill", f"gpu-run-{run_id}")
        (
            stdout_b, stderr_b, _out_total, _err_total, out_trunc, err_trunc,
        ) = await communicate_capped(proc)
    except BaseException:
        # 2026-10-06 审查修复:框架 tool_timeout(300s) 引发的 CancelledError 不进
        # TimeoutError 分支 → 容器成了孤儿独占 --gpus device=0。任何异常路径都杀容器。
        with contextlib.suppress(Exception):
            await asyncio.create_subprocess_exec("docker", "kill", f"gpu-run-{run_id}")
        with contextlib.suppress(Exception):
            await communicate_capped(proc)
        raise
    finally:
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                await asyncio.create_subprocess_exec("docker", "kill", f"gpu-run-{run_id}")
    duration = time.monotonic() - started

    stdout = _clip((stdout_b or b"").decode("utf-8", errors="replace"))
    stderr = _clip((stderr_b or b"").decode("utf-8", errors="replace"))
    if out_trunc:
        stdout += f"\n…[stdout 超过采集上限 {STREAM_CAPTURE_LIMIT} 字节，后续输出已丢弃]…"
    if err_trunc:
        stderr += f"\n…[stderr 超过采集上限 {STREAM_CAPTURE_LIMIT} 字节，后续输出已丢弃]…"
    artifacts = _collect_artifacts(output_dir)
    total_out = sum(a["sizeBytes"] for a in artifacts)
    if total_out > MAX_OUTPUT_BYTES:
        return JobResult(ok=False, run_id=run_id, exit_code=proc.returncode, timed_out=timed_out,
                         duration_s=duration, stdout=stdout, stderr=stderr, artifacts=artifacts,
                         error=f"产物总大小 {total_out} 超过上限 {MAX_OUTPUT_BYTES}（容器已清理，产物不回传）")

    result = JobResult(
        ok=(proc.returncode == 0 and not timed_out),
        run_id=run_id,
        exit_code=proc.returncode,
        timed_out=timed_out,
        duration_s=round(duration, 1),
        stdout=stdout,
        stderr=stderr,
        artifacts=artifacts,
    )
    (run_dir / "result.json").write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return result


# ─────────────────── 数据集代下（gpu.data.fetch 的执行层） ───────────────────

MAX_DATASET_BYTES = 2 * 1024 * 1024 * 1024  # 单数据集 2GB
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_SAFE_DATASET_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def data_audit_path() -> Path:
    return CLUSTER_DIR / "ws-gpu" / "logs" / "data-audit.jsonl"


def _url_policy(url: str) -> str | None:
    """单个 URL 的取回策略（返回错误文案；None=放行）。仅 https，回环可 http，可选域名白名单。

    每一跳都要过这道检查（见 fetch_dataset 的 request 事件钩子）：只在入口校验一次的话，
    `follow_redirects=True` 会让 302 把请求带到任意主机/明文 http，白名单形同虚设。
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "URL 无法解析"
    host = (parsed.hostname or "").strip("[]")
    if parsed.scheme == "https":
        allow = {d.strip().lower() for d in os.getenv("ACPS_GPU_DATA_DOMAINS", "").split(",") if d.strip()}
        if allow and host.lower() not in allow:
            return f"域名 {host} 不在 ACPS_GPU_DATA_DOMAINS 白名单内"
    elif parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        pass  # 回环自托管数据源（本地演示/同机服务），放行 http
    else:
        return "仅允许 https（回环地址可 http）"
    return None


class _UrlPolicyError(ValueError):
    """重定向落到了策略不允许的地址。"""


def validate_fetch(name: str, url: str, sha256: str | None = None) -> str | None:
    """返回错误文案；None 表示通过。域名白名单经 ACPS_GPU_DATA_DOMAINS 注入。"""
    if not isinstance(name, str) or not _SAFE_DATASET_NAME.match(name):
        return "数据集名只允许 1-64 位字母数字_-（将作为容器内 /data/<name> 目录名）"
    if (DATASETS_DIR / name).exists():
        return f"数据集 {name} 已存在（换名，或由节点主人删除后重取）"
    error = _url_policy(url)
    if error:
        return error
    if sha256 is not None and (not isinstance(sha256, str) or not _SHA256_HEX.match(sha256)):
        return "sha256 必须是 64 位十六进制（或不提供）"
    return None


def _safe_unzip(zip_path: Path, target: Path) -> list[str]:
    """解压 zip：拒绝绝对路径/drive/UNC/../反斜杠穿越，返回落盘文件名。

    Windows 下 `Path('/etc/evil')` 并非绝对路径（无盘符），直接 joinpath 会落到
    当前盘根；`C:evil` 则是盘符相对路径。因此按 POSIX 与 Windows 两套语义分别
    检查，并在写入前用 resolve() 再确认一次目标仍在解压目录内（2026-10-18 P0-4）。
    """
    extracted: list[str] = []
    total = 0
    base = target.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            posix = PurePosixPath(name)
            windows = PureWindowsPath(name)
            unsafe = (
                not name
                or "\x00" in name
                or "\\" in name
                or posix.is_absolute()
                or windows.is_absolute()
                or bool(windows.drive)
                or bool(windows.root)
                or ".." in posix.parts
                or ".." in windows.parts
            )
            if unsafe:
                raise ValueError(f"压缩包含不安全路径: {name}")
            total += info.file_size
            if total > MAX_DATASET_BYTES:
                raise ValueError("解压后总大小超过上限")
            dest = target.joinpath(*posix.parts)
            if not dest.resolve().is_relative_to(base):
                raise ValueError(f"压缩包路径越出解压目录: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append(dest.relative_to(target).as_posix())
    return extracted


def _safe_untar(tar_path: Path, target: Path) -> list[str]:
    # Python 3.12+ 的 filter="data" 自带穿越/符号链接/设备文件防护
    with tarfile.open(tar_path) as tf:
        tf.extractall(target, filter="data")
    return [p.relative_to(target).as_posix() for p in sorted(target.rglob("*")) if p.is_file()]


async def fetch_dataset(name: str, url: str, sha256: str | None = None) -> dict:
    """宿主侧（沙盒外）代下数据集 → 校验/解压 → datasets/<name>/（容器内只读 /data/<name>）。"""
    import httpx

    error = validate_fetch(name, url, sha256)
    if error:
        return {"ok": False, "error": error}

    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    staging = DATASETS_DIR / ".staging"
    staging.mkdir(exist_ok=True)
    blob = staging / f"{name}.{uuid.uuid4().hex[:8]}.download"
    started = time.monotonic()
    digest = hashlib.sha256()
    size = 0
    try:
        async def _revalidate_every_hop(request) -> None:
            err = _url_policy(str(request.url))
            if err:
                raise _UrlPolicyError(f"{request.url} → {err}")

        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0,
                                     event_hooks={"request": [_revalidate_every_hop]}) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return {"ok": False, "error": f"下载失败 HTTP {resp.status_code}"}
                with blob.open("wb") as out:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > MAX_DATASET_BYTES:
                            return {"ok": False, "error": f"超过单数据集上限 {MAX_DATASET_BYTES // 1024**3}GB"}
                        digest.update(chunk)
                        out.write(chunk)
        if sha256 and digest.hexdigest() != sha256.lower():
            return {"ok": False, "error": "sha256 校验失败（内容与调用方声明不符）"}

        target = DATASETS_DIR / name
        try:
            target.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return {"ok": False, "error": f"数据集 {name!r} 已存在（并发或重复取回被拒）"}
        lower = urlparse(url).path.lower()
        try:
            if lower.endswith(".zip"):
                files = _safe_unzip(blob, target)
            elif lower.endswith((".tar", ".tar.gz", ".tgz")):
                files = _safe_untar(blob, target)
            else:
                filename = Path(urlparse(url).path).name or "data.bin"
                if not _SAFE_FILENAME.match(filename):
                    filename = "data.bin"
                shutil.move(str(blob), target / filename)
                files = [filename]
        except (ValueError, zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            shutil.rmtree(target, ignore_errors=True)
            return {"ok": False, "error": f"解压/落盘失败: {exc}"}

        total_bytes = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "name": name,
            "url": url,
            "bytes": total_bytes,
            "sha256": digest.hexdigest(),
            "files": len(files),
            "durationS": round(time.monotonic() - started, 1),
        }
        data_audit_path().parent.mkdir(parents=True, exist_ok=True)
        with data_audit_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return {"ok": True, "name": name, "files": files[:20], "totalBytes": total_bytes,
                "sha256": digest.hexdigest(), "durationSeconds": record["durationS"],
                "mountedAt": f"/data/{name}（只读）"}
    except _UrlPolicyError as exc:
        return {"ok": False, "error": f"取回策略拒绝（逐跳复验，含重定向落点）: {exc}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"下载网络错误: {type(exc).__name__}: {str(exc)[:160]}"}
    except OSError as exc:
        return {"ok": False, "error": f"取回落盘失败: {type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        blob.unlink(missing_ok=True)


def recent_run_dirs(root: Path, limit: int = 10) -> list[Path]:
    """按 result.json（无则目录）mtime 倒序取最近 run。

    run id 是随机 hex，按名字排序会把刚完成的 run 排到任意位置（P2-10）。
    """
    try:
        dirs = [path for path in root.glob("run-*") if path.is_dir()]
        dirs.sort(
            key=lambda path: (path / "result.json").stat().st_mtime
            if (path / "result.json").is_file()
            else path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    return dirs[:limit]


def list_datasets() -> dict:
    """列出一层目录：{name: {files, totalBytes}}（gpu.data.list / gpu.env 共用）。"""
    if not DATASETS_DIR.is_dir():
        return {"datasets": {}}
    datasets = {}
    for entry in sorted(DATASETS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        files = [p for p in entry.rglob("*") if p.is_file()]
        datasets[entry.name] = {
            "files": len(files),
            "totalBytes": sum(p.stat().st_size for p in files),
            "mountedAt": f"/data/{entry.name}",
        }
    return {"datasets": datasets}
