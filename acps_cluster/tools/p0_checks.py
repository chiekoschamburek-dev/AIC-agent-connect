"""P0 前置验证（多入口编排计划）：ACS 指纹 / 后台作业底座 / 问迹并发。

三项验证的共同前提：**不改业务代码、不改生产配置**。`jobs --run` 会在
`acps_cluster/ws-supervisor/jobs/` 下创建一个真实后台作业（需要 `--yes`），
用于回答"后台 worker 能不能登录并跑完、跑的时候常驻 supervisor 还在不在"。

    python -m acps_cluster.tools.p0_checks acs
    python -m acps_cluster.tools.p0_checks jobs --static
    python -m acps_cluster.tools.p0_checks jobs --run --yes --timeout 600
    python -m acps_cluster.tools.p0_checks wenji --workspace .wit/wenji-aip-agent \
        --peer-aic <问迹 AIC> --long-question "统计 2010 年每个国家的销售额"

三项结论对应计划里的三个前置：

1. **acs**：新增 `research.submit` 这类能力，用 public / `visibility=domain` 会不会改
   ACS 投影（会 → 平台要重新注册 → 换 AIC → 材料全改）；非公开声明是否零影响。
2. **jobs**：`jobs_runner`（独立进程 + 同一 AIC 登录）能否在常驻 supervisor 在线时跑通，
   且不把常驻实例挤下线；失败时给出日志与下一步。
3. **wenji**：问迹的 AIP 服务是"单工作者"还是"可并发"——决定入口提交等待会不会
   堵住回派子任务。用两个并发任务（长 `wenji.ask` + 短 `wenji.schema`）实测重叠。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

CLUSTER = Path(__file__).resolve().parents[1]
REPO = CLUSTER.parent

_EXIT_OK = 0
_EXIT_FAIL = 1
_EXIT_SKIP = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def projection_sha256(skills: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical(list(skills)).encode("utf-8")).hexdigest()


def diff_skills(
    base: Sequence[Mapping[str, Any]], other: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """按 skill id 比较两份 ACS 投影：新增 / 移除 / 字段变化。"""
    base_by_id = {str(s.get("id")): s for s in base}
    other_by_id = {str(s.get("id")): s for s in other}
    added = sorted(set(other_by_id) - set(base_by_id))
    removed = sorted(set(base_by_id) - set(other_by_id))
    changed = sorted(
        sid for sid in set(base_by_id) & set(other_by_id)
        if _canonical(base_by_id[sid]) != _canonical(other_by_id[sid])
    )
    return {"added": added, "removed": removed, "changed": changed,
            "same": not (added or removed or changed)}


# ─────────────────────────── ① ACS 指纹 ───────────────────────────


def _load_make_acs():
    """按文件路径加载 `deploy/make_acs.py`（deploy 不是包），复用它的投影逻辑。"""
    path = CLUSTER / "deploy" / "make_acs.py"
    if not path.is_file():
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location("_yanshu_make_acs", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _synthetic_capabilities():
    """三个探针能力：非公开 / 公开 / 域可见——只用于投影比较，不注册。"""
    from wit import capability  # noqa: PLC0415

    @capability(id="research.submit", description="P0 探针：受理科研需求",
                public=False, model_callable=False, version="1.0.0", tags=["p0"])
    async def research_submit(task: str = "") -> dict:  # noqa: ARG001
        return {}

    @capability(id="research.status_public", description="P0 探针：公开查询",
                public=True, model_callable=False, version="1.0.0", tags=["p0"])
    async def research_status_public(rid: str = "") -> dict:  # noqa: ARG001
        return {}

    @capability(id="research.domain_demo", description="P0 探针：域可见",
                model_callable=False, version="1.0.0", tags=["p0"],
                acps_skill={"visibility": "domain", "domains": ["yanshu-research"]})
    async def research_domain_demo(task: str = "") -> dict:  # noqa: ARG001
        return {}

    return research_submit, research_status_public, research_domain_demo


def acs_report() -> dict[str, Any]:
    """比较 supervisor 当前投影与"加一个同类能力"后的投影。"""
    from acps_cluster.agents import supervisor  # noqa: PLC0415

    make_acs = _load_make_acs()
    agent = supervisor.build_supervisor_agent(None)
    version = str(getattr(agent, "version", "") or "1.2.0")
    base_caps = list(getattr(agent, "capabilities", []) or [])

    def project(extra: Sequence[Any] = ()) -> list[dict]:
        shim = SimpleNamespace(capabilities=base_caps + list(extra))
        return make_acs.build_skills(shim, [], version)

    baseline = project()
    private_cap, public_cap, domain_cap = _synthetic_capabilities()
    private = project([private_cap])
    public = project([public_cap])
    domain = project([domain_cap])

    private_diff = diff_skills(baseline, private)
    public_diff = diff_skills(baseline, public)
    domain_diff = diff_skills(baseline, domain)
    return {
        "agent": str(getattr(agent, "display_name", "") or ""),
        "version": version,
        "baseline": {
            "skills": [s.get("id") for s in baseline],
            "sha256": projection_sha256(baseline),
        },
        "non_public": {"diff": private_diff, "sha256": projection_sha256(private)},
        "public": {"diff": public_diff, "sha256": projection_sha256(public)},
        "domain": {"diff": domain_diff, "sha256": projection_sha256(domain)},
        "verdict": {
            "non_public_safe": private_diff["same"],
            "public_changes_acs": not public_diff["same"],
            "domain_changes_acs": not domain_diff["same"],
        },
    }


def cmd_acs(args: argparse.Namespace) -> int:
    try:
        report = acs_report()
    except ImportError as exc:
        print(f"[p0:acs] 需要 wit 运行时才能构建 agent：{exc}", file=sys.stderr)
        return _EXIT_SKIP
    except Exception as exc:  # noqa: BLE001 - 自检失败要打印原因而不是堆栈
        print(f"[p0:acs] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return _EXIT_FAIL

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[p0:acs] {report['agent']} v{report['version']}")
        print(f"  当前 skills   {report['baseline']['skills']}")
        print(f"  当前投影 sha256 {report['baseline']['sha256'][:16]}…")
        for key, label in (("non_public", "非公开新增"), ("public", "公开新增"),
                           ("domain", "域可见新增")):
            diff = report[key]["diff"]
            state = "不变" if diff["same"] else f"变化 {diff}"
            print(f"  {label:8s} → ACS 投影{state}")
        print(
            "[p0:acs] 结论：非公开声明不改 ACS"
            if report["verdict"]["non_public_safe"]
            else "[p0:acs] 结论：非公开声明竟然改了 ACS，必须先查清楚"
        )
    return _EXIT_OK if report["verdict"]["non_public_safe"] else _EXIT_FAIL


# ─────────────────────────── ② 后台作业底座 ───────────────────────────


def parse_env_file(path: Path) -> dict[str, str]:
    """极简 KEY=VALUE 解析（与 bootstrap/jobs_runner 的口径一致）。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            values[key] = value
    return values


def static_checks(cluster: Path = CLUSTER) -> list[dict[str, Any]]:
    """后台作业底座的前置条件（只读检查，不创建作业）。"""
    env_file = cluster / "deploy" / ".env.production"
    env = parse_env_file(env_file)
    ws = cluster / "ws-supervisor"
    acps = ws / "acps"
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str, *, required: bool = True) -> None:
        checks.append({"id": check_id, "ok": bool(ok), "detail": detail,
                       "required": bool(required)})

    add("env-file", env_file.is_file(), f"{env_file} {'存在' if env_file.is_file() else '缺失'}")
    model_keys = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
    add("model-config", all(env.get(k) for k in model_keys[:1]),
        "OPENAI_API_KEY " + ("已配置" if env.get("OPENAI_API_KEY") else "缺失（作业跑模型必需）"))
    add("registry-credentials",
        bool(env.get("ACPS_SUPERVISOR_USERNAME") and env.get("ACPS_SUPERVISOR_PASSWORD")),
        "ACPS_SUPERVISOR_USERNAME/PASSWORD " + (
            "已配置" if env.get("ACPS_SUPERVISOR_USERNAME") and env.get("ACPS_SUPERVISOR_PASSWORD")
            else "缺失（jobs_runner 要登录注册中心）"))
    add("supervisor-acps", (acps / "acs.final.json").is_file(),
        f"{acps / 'acs.final.json'} {'存在' if (acps / 'acs.final.json').is_file() else '缺失'}")
    add("supervisor-cert", (acps / "client" / "certificate.pem").is_file()
        and (acps / "client" / "private-key.pem").is_file(),
        "mTLS 客户端证书/私钥 " + (
            "存在" if (acps / "client" / "certificate.pem").is_file() else "缺失"))
    add("job-workspace-root", ws.is_dir() and os.access(ws, os.W_OK),
        f"{ws} {'可写' if ws.is_dir() and os.access(ws, os.W_OK) else '不存在或不可写'}")
    wit_available = importlib.util.find_spec("wit") is not None
    add("wit-runtime", wit_available, "wit 可导入" if wit_available else "wit 未安装（作业进程无法启动）")
    return checks


def _kill_process(proc: subprocess.Popen, *, pgid: bool) -> None:
    try:
        if pgid and os.name != "nt":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _supervisor_identity(cluster: Path = CLUSTER) -> tuple[str, str]:
    """从 ws-supervisor 的 ACS/enrollment 读 (AIC, endpoint)。"""
    acps = cluster / "ws-supervisor" / "acps"
    aic = endpoint = ""
    try:
        acs = json.loads((acps / "acs.final.json").read_text(encoding="utf-8"))
        aic = str(acs.get("aic") or "")
        for item in acs.get("endPoints") or []:
            if isinstance(item, dict) and str(item.get("transport") or "").upper() == "JSONRPC":
                endpoint = str(item.get("url") or "")
                break
    except (OSError, ValueError):
        pass
    if not aic:
        try:
            enrollment = json.loads((acps / "enrollment.json").read_text(encoding="utf-8"))
            aic = str((enrollment.get("review") or {}).get("aic") or "")
        except (OSError, ValueError):
            pass
    return aic, endpoint


def _probe_supervisor(workspace: str, *, timeout_s: float = 90.0) -> dict[str, Any]:
    """用指定工作区身份对 supervisor 发一条确定性任务（检查常驻实例是否还在服务）。"""
    from acps_cluster.bidirectional.identity import load_identity  # noqa: PLC0415
    from acps_cluster.bidirectional.probe import (  # noqa: PLC0415
        ProbeConfig, resolve_peer_endpoint, run_probe,
    )

    identity = load_identity(workspace)
    peer_aic, peer_endpoint = _supervisor_identity()
    endpoint = resolve_peer_endpoint(
        identity, peer_aic, explicit=peer_endpoint, cluster_root=CLUSTER,
    )
    cfg = ProbeConfig(
        identity=identity,
        peer_aic=peer_aic,
        peer_endpoint=endpoint,
        skill_id="research.orchestrate",
        args={"task": "P0 并发共存检查：作业运行期间常驻 supervisor 是否仍可服务"},
        timeout_s=timeout_s,
        poll_s=1.0,
        notes=("p0:jobs 期间探活",),
    )
    return asyncio.run(run_probe(cfg))


def jobs_run(
    *,
    question: str,
    timeout_s: float,
    probe_workspace: str = "",
    cluster: Path = CLUSTER,
    runner: Path | None = None,
) -> dict[str, Any]:
    """创建一个真实后台作业并跟踪到终态；可选在作业运行期间探活常驻 supervisor。"""
    from acps_cluster.runtime import jobs_store  # noqa: PLC0415

    jobs_root = cluster / "ws-supervisor" / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)
    runner_path = Path(runner) if runner is not None else Path(
        jobs_store.__file__).with_name("jobs_runner.py")
    job_id, job_dir = jobs_store.create_job(jobs_root, question, "custom", owner_aic="p0-check")
    env = dict(os.environ)
    env["ACPS_JOB_REPORT_OUT"] = str(job_dir / "report.md")
    log_path = job_dir / "log.txt"
    with log_path.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(runner_path), str(job_dir)],
            cwd=str(cluster), env=env,
            stdout=log_file, stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )

    timeline: list[dict[str, Any]] = []
    probe: dict[str, Any] | None = None
    deadline = time.time() + max(1.0, float(timeout_s))
    state = "queued"
    timed_out = False
    try:
        while time.time() < deadline:
            # resolve_liveness 会在 pid 已死时把状态改成 interrupted，避免傻等到超时
            status = jobs_store.resolve_liveness(job_dir)
            current = str(status.get("state") or "queued")
            if not timeline or timeline[-1]["state"] != current:
                timeline.append({"at": _now_iso(), "state": current,
                                 "pid": status.get("pid")})
            state = current
            if state in {"done", "failed", "interrupted"}:
                break
            if probe_workspace and state == "running" and probe is None:
                try:
                    probe = _probe_supervisor(probe_workspace)
                except Exception as exc:  # noqa: BLE001 - 探活失败不影响作业结论
                    probe = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            time.sleep(2.0)
        else:
            timed_out = True
    finally:
        if timed_out or proc.poll() is None and state not in {"done", "failed", "interrupted"}:
            _kill_process(proc, pgid=True)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _kill_process(proc, pgid=False)

    status = jobs_store.read_status(job_dir)
    report_size = 0
    try:
        report_size = (job_dir / "report.md").stat().st_size
    except OSError:
        pass
    log_tail = ""
    try:
        log_tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
    except OSError:
        pass
    return {
        "job_id": job_id,
        "job_dir": str(job_dir),
        "state": str(status.get("state") or ""),
        "run_status": status.get("run_status"),
        "exit_code": status.get("exit_code"),
        "iterations": status.get("iterations"),
        "timed_out": timed_out,
        "report_bytes": report_size,
        "timeline": timeline,
        "probe": probe,
        "log_tail": log_tail,
    }


def cmd_jobs(args: argparse.Namespace) -> int:
    if args.run:
        if not args.yes:
            print("[p0:jobs] --run 会在生产 jobs 目录创建真实作业；确认后加 --yes", file=sys.stderr)
            return _EXIT_FAIL
        result = jobs_run(
            question=args.question,
            timeout_s=args.timeout,
            probe_workspace=args.probe_workspace,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[p0:jobs] job={result['job_id']} state={result['state']} "
                  f"run_status={result['run_status']} report={result['report_bytes']}B")
            print("  时间线：" + " → ".join(item["state"] for item in result["timeline"]))
            if result["probe"] is not None:
                probe = result["probe"]
                print(f"  作业期间探活：ok={probe.get('ok')} "
                      f"state={probe.get('final_state')} error={probe.get('error') or '无'}")
            if result["state"] != "done":
                print("  log tail:\n" + (result["log_tail"] or "(空)"))
        return _EXIT_OK if result["state"] == "done" else _EXIT_FAIL

    checks = static_checks()
    failed = [c for c in checks if c["required"] and not c["ok"]]
    if args.json:
        print(json.dumps({"checks": checks, "ready": not failed}, ensure_ascii=False, indent=2))
    else:
        print("[p0:jobs] 静态前置检查")
        for item in checks:
            mark = "OK  " if item["ok"] else ("FAIL" if item["required"] else "WARN")
            print(f"  [{mark}] {item['id']}: {item['detail']}")
        print(f"[p0:jobs] {'READY' if not failed else 'NOT READY'}"
              + ("" if not failed else "：" + "；".join(c["id"] for c in failed)))
    return _EXIT_OK if not failed else _EXIT_FAIL


# ─────────────────────────── ③ 问迹并发 ───────────────────────────


def _ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def concurrency_verdict(long_run: Mapping[str, Any], short_run: Mapping[str, Any]) -> dict[str, Any]:
    """判断"短任务是否在长任务结束前完成"——即服务端能否并发处理入站任务。

    两个 probe 结果都用 `started_at` / `finished_at`；重叠 = 短任务在长任务结束前开始，
    且短任务在长任务结束前完成。
    """
    long_start, long_end = _ts(long_run.get("started_at")), _ts(long_run.get("finished_at"))
    short_start, short_end = _ts(short_run.get("started_at")), _ts(short_run.get("finished_at"))
    if None in (long_start, long_end, short_start, short_end):
        return {"verdict": "unknown", "reason": "时间戳缺失"}
    overlap = short_start < long_end
    short_finished_first = short_end <= long_end
    if overlap and short_finished_first and short_start >= long_start:
        verdict = "concurrent"
        reason = "短任务在长任务结束前开始并完成：服务端可并发处理入站任务"
    elif short_start >= long_end:
        verdict = "serialized"
        reason = "短任务在长任务结束后才开始：服务端串行处理（需释放入口占用）"
    else:
        verdict = "inconclusive"
        reason = "时间区间重叠但短任务未先完成（可能资源竞争）"
    return {
        "verdict": verdict,
        "reason": reason,
        "long": {"start": long_run.get("started_at"), "end": long_run.get("finished_at"),
                 "state": long_run.get("final_state")},
        "short": {"start": short_run.get("started_at"), "end": short_run.get("finished_at"),
                  "state": short_run.get("final_state")},
        "overlap": bool(overlap),
        "short_finished_first": bool(short_finished_first),
    }


def wenji_report(
    *,
    workspace: str,
    peer_aic: str,
    peer_endpoint: str = "",
    long_question: str,
    short_skill: str = "wenji.schema",
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    from acps_cluster.bidirectional.identity import load_identity  # noqa: PLC0415
    from acps_cluster.bidirectional.probe import (  # noqa: PLC0415
        ProbeConfig, resolve_peer_endpoint, run_probe,
    )

    identity = load_identity(workspace)
    endpoint = resolve_peer_endpoint(identity, peer_aic, explicit=peer_endpoint,
                                     cluster_root=CLUSTER)
    long_cfg = ProbeConfig(
        identity=identity, peer_aic=peer_aic, peer_endpoint=endpoint,
        skill_id="wenji.ask", args={"question": long_question},
        timeout_s=timeout_s, poll_s=1.0, notes=("p0:wenji 长任务",),
    )
    short_cfg = ProbeConfig(
        identity=identity, peer_aic=peer_aic, peer_endpoint=endpoint,
        skill_id=short_skill, args={},
        timeout_s=timeout_s, poll_s=1.0, notes=("p0:wenji 短任务",),
    )

    async def _both():
        await asyncio.sleep(0.2)  # 让长任务先进入 working
        return await asyncio.gather(run_probe(long_cfg), run_probe(short_cfg))

    long_run, short_run = asyncio.run(_both())
    return {
        "peer_aic": peer_aic,
        "endpoint": endpoint,
        "long": long_run,
        "short": short_run,
        "concurrency": concurrency_verdict(long_run, short_run),
    }


def cmd_wenji(args: argparse.Namespace) -> int:
    try:
        report = wenji_report(
            workspace=args.workspace,
            peer_aic=args.peer_aic,
            peer_endpoint=args.peer_endpoint,
            long_question=args.long_question,
            short_skill=args.short_skill,
            timeout_s=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[p0:wenji] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return _EXIT_FAIL
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        conc = report["concurrency"]
        print(f"[p0:wenji] 长任务 {report['long'].get('final_state')} "
              f"{report['long'].get('elapsed_s')}s；短任务 "
              f"{report['short'].get('final_state')} {report['short'].get('elapsed_s')}s")
        print(f"[p0:wenji] 结论：{conc['verdict']} —— {conc['reason']}")
    return _EXIT_OK if report["long"].get("ok") and report["short"].get("ok") else _EXIT_FAIL


# ─────────────────────────── CLI ───────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acps_cluster.tools.p0_checks",
        description="P0 前置验证：ACS 指纹 / 后台作业底座 / 问迹并发（不改业务代码）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    acs = sub.add_parser("acs", help="比较新增能力对 supervisor ACS 投影的影响")
    acs.add_argument("--json", action="store_true")
    acs.set_defaults(func=cmd_acs)

    jobs = sub.add_parser("jobs", help="后台作业底座检测（--static 只读 / --run 真跑）")
    mode = jobs.add_mutually_exclusive_group()
    mode.add_argument("--static", action="store_true", help="只检查前置条件（默认）")
    mode.add_argument("--run", action="store_true", help="创建一个真实作业并跟踪终态")
    jobs.add_argument("--yes", action="store_true", help="确认在生产目录创建作业")
    jobs.add_argument("--timeout", type=float, default=600.0)
    jobs.add_argument("--question", default="只回复 p0-ok，不要调用任何工具或伙伴。")
    jobs.add_argument("--probe-workspace", default="",
                      help="作业运行期间用该工作区身份探活 supervisor（如 acps_cluster/ws-kb）")
    jobs.add_argument("--json", action="store_true")
    jobs.set_defaults(func=cmd_jobs)

    wenji = sub.add_parser("wenji", help="问迹并发实测（长 + 短两个 AIP 任务）")
    wenji.add_argument("--workspace", required=True, help="问迹的身份工作区（含 acps/）")
    wenji.add_argument("--peer-aic", required=True, help="问迹 AIC")
    wenji.add_argument("--peer-endpoint", default="")
    wenji.add_argument("--long-question", default="统计 2010 年每个国家的销售额，按金额降序")
    wenji.add_argument("--short-skill", default="wenji.schema")
    wenji.add_argument("--timeout", type=float, default=300.0)
    wenji.add_argument("--json", action="store_true")
    wenji.set_defaults(func=cmd_wenji)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "jobs" and not args.run:
        args.static = True
    return int(args.func(args))


__all__ = [
    "acs_report",
    "build_parser",
    "concurrency_verdict",
    "diff_skills",
    "jobs_run",
    "main",
    "parse_env_file",
    "projection_sha256",
    "static_checks",
    "wenji_report",
]

if __name__ == "__main__":
    raise SystemExit(main())