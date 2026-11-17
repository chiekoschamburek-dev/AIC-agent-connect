"""`python -m acps_cluster.bidirectional` 命令行入口。

四个子命令对应"取证四步"：

    doctor   开跑前自检（身份文件、依赖、入站准入配置、任务库）
    probe    以本机身份向对端发一条真实 AIP 任务（出站方向）
    ledger   从本机任务库提取一条任务的回执（入站方向 / 运行时出站）
    pack     合并两个方向 → 证据包（JSON + Markdown + SHA256SUMS）

退出码：`doctor`/`pack` 用 0=通过、1=未通过；`probe` 0=completed、1=其它；
参数错误由 argparse 返回 2。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import evidence, ledger
from .identity import discover, load_identity
from .models import TaskReceipt
from .probe import ProbeConfig, resolve_peer_endpoint, run_probe

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "artifacts" / "bidirectional"


def _date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _short_aic(aic: str) -> str:
    parts = str(aic or "").split(".")
    return ".".join(parts[-3:]) if len(parts) >= 3 else (aic or "peer")


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _load_receipt_file(path: str | Path, direction: str) -> TaskReceipt:
    """读一份回执并**转成本机视角**。

    对端用同一份工具跑出来的回执是"对端视角"：它是 outbound（对端→本机）。
    本机要拿它当 inbound 证据，必须翻转 local/remote 与 direction，否则 AIC 对称性
    自检会把两个方向判成同向（这正是"假双向"最常见的来源）。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 不是 JSON 对象")
    payload = data["receipt"] if isinstance(data.get("receipt"), dict) else data
    receipt = TaskReceipt.from_dict(payload)
    if receipt.direction == direction:
        return receipt
    flipped = TaskReceipt(
        direction=direction,
        task_ref=receipt.task_ref,
        state=receipt.state,
        local_aic=receipt.remote_aic,
        remote_aic=receipt.local_aic,
        created_at=receipt.created_at,
        updated_at=receipt.updated_at,
        session_id=receipt.session_id,
        local_error=receipt.local_error,
        products=receipt.products,
        events=receipt.events,
        source={**dict(receipt.source), "perspective": "flipped", "origin": str(path)},
        notes=tuple(receipt.notes) + (
            f"原始回执方向为 {receipt.direction}，已按本机视角翻转 local/remote（来源：{path}）",
        ),
    )
    return flipped


# ─────────────────────────── doctor ───────────────────────────


def _env_flag(name: str, values: dict[str, str] | None = None) -> bool:
    raw = (values or {}).get(name) if values is not None else os.getenv(name)
    if raw is None:
        raw = os.getenv(name, "0")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_values() -> tuple[dict[str, str], list[str]]:
    """读"生产配置"键值：先读 env 文件，再用当前进程环境覆盖（systemd 为准）。

    `doctor` 常常是在仓库根跑的独立进程，而 supervisor 的配置在
    `acps_cluster/deploy/.env.production`（systemd `EnvironmentFile`）。只看 `os.environ`
    会把"生产已开快速通道"误报成没开，所以两处都读。
    """
    values: dict[str, str] = {}
    sources: list[str] = []
    for path in (REPO_ROOT / "acps_cluster" / "deploy" / ".env.production", REPO_ROOT / ".env"):
        if not path.is_file():
            continue
        sources.append(str(path))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, _, value = text.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and value:
                values[key] = value
    for key, value in os.environ.items():
        if key.startswith("ACPS_") and value:
            values[key] = value
    return values, sources


def cmd_doctor(args: argparse.Namespace) -> int:
    identity = load_identity(args.workspace)
    problems: list[str] = list(identity.missing())
    report: dict[str, Any] = {
        "workspace": str(Path(args.workspace)),
        "identity": identity.as_dict(),
        "checks": [],
    }

    def add(check_id: str, ok: bool, detail: str, *, required: bool = True) -> None:
        report["checks"].append({"id": check_id, "ok": ok, "detail": detail,
                                 "required": required})
        if required and not ok:
            problems.append(f"{check_id}: {detail}")

    try:
        import acps_sdk  # noqa: F401,PLC0415
        add("acps_sdk", True, "acps_sdk 可导入")
    except ImportError:
        add("acps_sdk", False, "acps_sdk 未安装（probe 需要；vendor/acps_sdk-*.whl）")
    try:
        import httpx  # noqa: F401,PLC0415
        add("httpx", True, "httpx 可导入")
    except ImportError:
        add("httpx", False, "httpx 未安装（discovery 需要）", required=False)

    add("workspace-acs", (Path(args.workspace) / "acps" / "acs.final.json").is_file(),
        "acs.final.json 存在" if (Path(args.workspace) / "acps" / "acs.final.json").is_file()
        else "缺 acps/acs.final.json（工作区未注册/未铸造）")

    try:
        db = ledger.find_db(args.workspace)
        summary = ledger.summarize(db)
        add("ledger", True, f"{db}：{summary['total']} 条任务 {summary['by_direction']}")
        report["ledger"] = summary
        peers = ledger.iter_peer_aics(db, limit=2000)
        if peers:
            report["peers_seen"] = peers[:10]
    except (FileNotFoundError, ValueError) as exc:
        add("ledger", False, f"任务库不可读：{exc}", required=False)

    peer_aic = str(args.peer_aic or "")
    if peer_aic:
        env_values, env_sources = _env_values()
        report["env_sources"] = env_sources
        allow = [item.strip() for item in str(
            env_values.get("ACPS_SUPERVISOR_INBOUND_ALLOW_AICS", "")).split(",") if item.strip()]
        if "supervisor" in Path(args.workspace).name:
            allowed = peer_aic in allow
            add("inbound-allowlist", allowed,
                f"ACPS_SUPERVISOR_INBOUND_ALLOW_AICS={'包含' if allowed else '不含'}对端"
                f"（已读：{', '.join(env_sources) or '无 env 文件'}）；"
                "对端用标准 AIP 客户端（不指名 skill）时必须列入，否则会被 "
                "PEP 以 no-credential 拒绝",
                required=False)
            fast = _env_flag("ACPS_SUPERVISOR_SKILL_FASTPATH", env_values)
            add("skill-fastpath", fast,
                "ACPS_SUPERVISOR_SKILL_FASTPATH=1：对端以 commandParams 指名 "
                "research.orchestrate 时不跑 LLM（快、可重复）",
                required=False)

    if args.discover:
        try:
            agents = discover(identity, args.query or "")
            report["discovery"] = agents
            hit = next((a for a in agents if a.get("aic") == peer_aic), None)
            add("discovery", True,
                f"发现 {len(agents)} 个候选；对端{'命中' if hit else '未命中'}"
                + (f"（active={hit.get('active')}）" if hit else ""),
                required=False)
            if hit and hit.get("endpoint"):
                report["peer_endpoint"] = hit["endpoint"]
        except Exception as exc:  # noqa: BLE001 - 平台不可达不该让 doctor 崩
            add("discovery", False, f"发现失败（不影响离线检查）：{type(exc).__name__}: {exc}",
                required=False)

    if peer_aic and "peer_endpoint" not in report:
        endpoint = resolve_peer_endpoint(
            identity, peer_aic, cluster_root=REPO_ROOT / "acps_cluster"
        )
        if endpoint:
            report["peer_endpoint"] = endpoint
            add("peer-endpoint", True, f"对端端点（本地 ACS）：{endpoint}", required=False)

    report["ready"] = not problems
    report["problems"] = problems
    if args.json:
        _print_json(report)
    else:
        print(f"[doctor] 工作区 {report['workspace']}")
        print(f"  本机 AIC   {identity.aic or '(缺)'}")
        print(f"  端点       {identity.endpoint or '(缺)'}")
        print(f"  发现接口   {identity.discovery_url or '(缺)'}")
        for item in report["checks"]:
            mark = "OK " if item["ok"] else ("FAIL" if item["required"] else "WARN")
            print(f"  [{mark}] {item['id']}: {item['detail']}")
        print(f"[doctor] {'READY' if report['ready'] else 'NOT READY'}"
              + ("" if report["ready"] else "：" + "；".join(problems)))
    return 0 if report["ready"] else 1


# ─────────────────────────── probe ───────────────────────────


async def _probe_async(args: argparse.Namespace) -> dict[str, Any]:
    identity = load_identity(args.workspace)
    peer_aic = str(args.peer_aic)
    discovery: list[dict[str, Any]] = []
    if args.discover:
        try:
            discovery = discover(identity, args.query or peer_aic)
        except Exception as exc:  # noqa: BLE001 - 发现失败不阻塞（可用 --peer-endpoint 直连）
            print(f"[probe] 发现失败（继续直连）：{type(exc).__name__}: {exc}", file=sys.stderr)
    endpoint = resolve_peer_endpoint(
        identity, peer_aic,
        explicit=str(args.peer_endpoint or ""),
        cluster_root=REPO_ROOT / "acps_cluster",
        discovery=discovery,
    )
    try:
        parsed_args = json.loads(args.args) if args.args else {}
    except ValueError as exc:
        raise SystemExit(f"--args 不是合法 JSON：{exc}") from exc
    if not isinstance(parsed_args, dict):
        raise SystemExit("--args 必须是 JSON 对象")
    cfg = ProbeConfig(
        identity=identity,
        peer_aic=peer_aic,
        peer_endpoint=endpoint,
        skill_id=str(args.skill or ""),
        args=parsed_args,
        task_text=str(args.task or ""),
        answer=str(args.answer or ""),
        grant_file=str(getattr(args, "grant", "") or ""),
        timeout_s=float(args.timeout),
        poll_s=float(args.poll),
        notes=() if endpoint else ("未解析到对端端点",),
    )
    return await run_probe(cfg, discovery=discovery)


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_probe(args: argparse.Namespace) -> int:
    result = asyncio.run(_probe_async(args))
    out = Path(args.out) if args.out else (
        DEFAULT_OUTPUT_ROOT / _date_stamp() / f"probe-{_short_aic(args.peer_aic)}.json"
    )
    path = _write_json(out, result)
    receipt = result.get("receipt") or {}
    print(f"[probe] {result.get('leader_aic') or '?'} → {result.get('partner_aic') or '?'} "
          f"skill={result.get('skill_id') or '(未指名)'}")
    print(f"[probe] taskId={result.get('task_id')} 终态={result.get('final_state') or '(空)'} "
          f"耗时={result.get('elapsed_s')}s grant={'是' if args.grant else '否'}")
    if result.get("error"):
        print(f"[probe] 错误：{result['error']}")
    print(f"[probe] 回执：{path}")
    print(f"[probe] 产物：{receipt.get('products', {})}")
    previews = result.get("product_previews") or []
    if previews:
        text = str(previews[0].get("text") or "")
        print(f"[probe] 产物预览（{len(text)} 字符）：{text[:400]}")
    return 0 if result.get("ok") else 1


# ─────────────────────────── ledger ───────────────────────────


def cmd_ledger(args: argparse.Namespace) -> int:
    db = Path(args.db) if args.db else ledger.find_db(args.workspace)
    receipt = ledger.receipt_for(db, args.task_ref, direction=args.direction or None)
    path = _write_json(Path(args.out), receipt.as_dict()) if args.out else None
    _print_json(receipt.as_dict())
    if path:
        print(f"[ledger] 已写入 {path}", file=sys.stderr)
    return 0


# ─────────────────────────── pack ───────────────────────────


def cmd_pack(args: argparse.Namespace) -> int:
    identity = load_identity(args.workspace)
    peer_aic = str(args.peer_aic)
    db = Path(args.db) if args.db else None
    if db is None:
        try:
            db = ledger.find_db(args.workspace)
        except FileNotFoundError:
            db = None

    outbound: TaskReceipt | None = None
    inbound: TaskReceipt | None = None
    notes: list[str] = []
    if args.out_receipt:
        outbound = _load_receipt_file(args.out_receipt, "outbound")
        notes.append(f"出站回执来自探针/运行时文件：{args.out_receipt}")
    if args.in_receipt:
        inbound = _load_receipt_file(args.in_receipt, "inbound")
        notes.append(f"入站回执来自对端文件（已按本机视角归一）：{args.in_receipt}")

    if db is not None and (outbound is None or inbound is None):
        try:
            pair = ledger.pair_for(
                db, peer_aic,
                since=args.since or None,
                out_task=args.out_task or None,
                in_task=args.in_task or None,
            )
        except (FileNotFoundError, ValueError, KeyError) as exc:
            pair = {}
            notes.append(f"任务库读取失败：{exc}")
        if outbound is None:
            outbound = pair.get("outbound")
        if inbound is None:
            inbound = pair.get("inbound")

    mirror: dict[str, TaskReceipt | None] | None = None
    mirror_workspace = str(args.peer_workspace or "")
    if mirror_workspace:
        mirror = {}
        try:
            peer_db = ledger.find_db(mirror_workspace)
        except FileNotFoundError as exc:
            peer_db = None
            notes.append(f"对端工作区任务库不可读，跳过双侧对账：{exc}")
        if peer_db is not None:
            for key, receipt in (("outbound", outbound), ("inbound", inbound)):
                if receipt is None:
                    mirror[key] = None
                    continue
                try:
                    mirror[key] = ledger.receipt_for(peer_db, receipt.task_ref)
                except KeyError:
                    mirror[key] = None
            if not any(mirror.values()):
                notes.append(
                    "对端任务库没有这两个 taskId：对端可能不是本仓运行时（探针直连不落对端库），"
                    "或记录已被裁剪"
                )

    pack = evidence.build_pack(
        outbound, inbound,
        self_aic=identity.aic,
        peer_aic=peer_aic,
        self_label=args.self_label or Path(args.workspace).name,
        peer_label=args.peer_label or "",
        workspace=str(Path(args.workspace)),
        ledger_db=str(db) if db else "",
        notes=notes,
        mirror=mirror,
        mirror_workspace=mirror_workspace,
    )
    out_dir = Path(args.out_dir) if args.out_dir else (
        DEFAULT_OUTPUT_ROOT / _date_stamp() / _short_aic(peer_aic)
    )
    written = evidence.write_pack(pack, out_dir)
    print(f"[pack] 判定：{str(pack['verdict']).upper()}")
    for item in pack["checks"]:
        mark = "OK  " if item["ok"] else ("FAIL" if item["required"] else "WARN")
        print(f"  [{mark}] {item['title']}：{item['detail']}")
    for path in written.values():
        print(f"[pack] 已写入 {path}")
    return 0 if evidence.is_pass(pack) else 1


# ─────────────────────────── parser ───────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acps_cluster.bidirectional",
        description="双向互调验证与取证（只读任务库；探针真实走 AIP/mTLS）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="开跑前自检")
    doctor.add_argument("--workspace", default="acps_cluster/ws-supervisor")
    doctor.add_argument("--peer-aic", default="")
    doctor.add_argument("--query", default="")
    doctor.add_argument("--discover", action="store_true", help="同时做一次平台发现")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    probe = sub.add_parser("probe", help="以本机身份发起一次出站 AIP 任务")
    probe.add_argument("--workspace", default="acps_cluster/ws-supervisor")
    probe.add_argument("--peer-aic", required=True)
    probe.add_argument("--peer-endpoint", default="")
    probe.add_argument("--skill", default="", help="技能 id（写入 commandParams.skillId）")
    probe.add_argument("--args", default="", help='技能参数 JSON，如 \'{"task":"..."}\'')
    probe.add_argument("--task", default="", help="任务正文（不给则用 args 的 JSON）")
    probe.add_argument("--answer", default="", help="awaiting-input 时的补充输入")
    probe.add_argument("--grant", default="",
                       help="域 grant 文件（JSON {token} 或纯 JWS）：随任务附上域授权")
    probe.add_argument("--timeout", type=float, default=300.0)
    probe.add_argument("--poll", type=float, default=2.0)
    probe.add_argument("--discover", action="store_true", help="先用平台发现解析端点")
    probe.add_argument("--query", default="")
    probe.add_argument("--out", default="")
    probe.set_defaults(func=cmd_probe)

    ledger_cmd = sub.add_parser("ledger", help="从任务库提取一条任务回执")
    ledger_cmd.add_argument("--workspace", default="acps_cluster/ws-supervisor")
    ledger_cmd.add_argument("--db", default="")
    ledger_cmd.add_argument("--task-ref", required=True)
    ledger_cmd.add_argument("--direction", choices=["outbound", "inbound"], default="")
    ledger_cmd.add_argument("--out", default="")
    ledger_cmd.set_defaults(func=cmd_ledger)

    pack = sub.add_parser("pack", help="合并两个方向生成证据包")
    pack.add_argument("--workspace", default="acps_cluster/ws-supervisor")
    pack.add_argument("--db", default="")
    pack.add_argument("--peer-aic", required=True)
    pack.add_argument("--out-task", default="", help="显式指定出站方向 taskId")
    pack.add_argument("--in-task", default="", help="显式指定入站方向 taskId")
    pack.add_argument("--out-receipt", default="", help="出站方向回执 JSON（probe 产出，或运行时导出）")
    pack.add_argument("--in-receipt", default="",
                      help="对端提供的回执 JSON（自动按本机视角翻转 local/remote）")
    pack.add_argument("--since", default="", help="只看该 ISO 时间之后创建的任务")
    pack.add_argument("--peer-workspace", default="",
                      help="对端工作区（读取其任务库做同 taskId 双侧对账）")
    pack.add_argument("--self-label", default="")
    pack.add_argument("--peer-label", default="")
    pack.add_argument("--out-dir", default="")
    pack.set_defaults(func=cmd_pack)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"[{args.command}] 缺文件：{exc}", file=sys.stderr)
        return 2
    except (ValueError, KeyError) as exc:
        print(f"[{args.command}] 失败：{exc}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]