"""P2 自证（一键）：双签章程 → 签发 grant → 核验 →（可选）真实入口调用。

为什么单独写：P2 的 grant 工具分 prepare/cosign/install/issue 四步，同机自证时容易漏步、
也容易把"签发成功"误当成"授权生效"。本脚本按**幂等**顺序一次做完，并把每一步的
证据落到一个目录里，失败时给出可执行的下一步。

    # 只签章程 + 发 grant + 核验（不跑模型、不花配额）
    python -m acps_cluster.tools.p2_selftest \
        --supervisor-workspace acps_cluster/ws-supervisor \
        --peer-workspace acps_cluster/ws-kb

    # 再用这个 grant 跑一次真实入口调用并轮询到终态
    python -m acps_cluster.tools.p2_selftest ... --submit

默认不执行 `--submit`：它会在生产里创建一个后台作业（一次完整编排，有模型成本）。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from acps_cluster.bidirectional.identity import load_identity
from acps_cluster.bidirectional.probe import ProbeConfig, run_probe
from acps_cluster.delegation.domain_manifest import load_domain_policies
from acps_cluster.delegation.token import sign_payload, verify_payload_jws
from acps_cluster.tools import entry_grant
from acps_cluster.tools.p0_checks import parse_env_file

DEFAULT_QUESTION = "请列出本集群当前可用的伙伴及其主要能力，不要委派任何任务。"
DEFAULT_DOMAIN = entry_grant.DEFAULT_DOMAIN
_TERMINAL = {"completed", "failed", "cancelled", "expired"}
_ROOT_ID_RE = re.compile(r"rt-\d{8}-\d{6}-[0-9a-f]{4}")
_JOB_ID_RE = re.compile(r"job-\d{8}-\d{6}-[0-9a-f]{4}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _out_dir(base: str | Path | None) -> Path:
    if base:
        return Path(base)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return Path("artifacts") / "p2" / f"selftest-{stamp}"


def supervisor_endpoint(workspace: Path) -> str:
    acs = workspace / "acps" / "acs.final.json"
    try:
        data = json.loads(acs.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for item in data.get("endPoints") or []:
        if isinstance(item, dict) and str(item.get("transport") or "").upper() == "JSONRPC":
            return str(item.get("url") or "")
    return ""


def _tcp_reachable(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not host:
        return False, "端点里没有主机名"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} 可连"
    except OSError as exc:
        return False, f"{host}:{port} 不可达：{type(exc).__name__}: {exc}"


def preflight(
    *,
    supervisor_workspace: Path,
    peer_workspace: Path,
    env_file: Path,
    submit: bool,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(check_id: str, ok: bool, detail: str, *, required: bool = True) -> None:
        checks.append({"id": check_id, "ok": bool(ok), "detail": detail,
                       "required": bool(required)})

    for label, workspace in (("supervisor", supervisor_workspace), ("peer", peer_workspace)):
        acps = workspace / "acps"
        add(f"{label}-acs", (acps / "acs.final.json").is_file(),
            f"{acps / 'acs.final.json'} " + ("存在" if (acps / 'acs.final.json').is_file() else "缺失"))
        add(f"{label}-cert", (acps / "client" / "certificate.pem").is_file()
            and (acps / "client" / "private-key.pem").is_file(),
            "mTLS 客户端证书/私钥 " + ("存在" if (acps / 'client' / 'certificate.pem').is_file() else "缺失"))
        add(f"{label}-trust", (acps / "trust-bundle.pem").is_file(),
            f"{acps / 'trust-bundle.pem'} " + ("存在" if (acps / 'trust-bundle.pem').is_file() else "缺失"))

    try:
        import acps_sdk  # noqa: F401,PLC0415
        add("acps-sdk", True, "acps_sdk 可导入")
    except ImportError:
        add("acps-sdk", False, "acps_sdk 未安装（probe 需要）")
    try:
        import cryptography  # noqa: F401,PLC0415
        add("cryptography", True, "cryptography 可导入")
    except ImportError:
        add("cryptography", False, "cryptography 未安装（章程/grant 签名需要）")

    env = parse_env_file(env_file)
    pep_on = str(env.get("ACPS_PEP_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
    add("pep-enabled", pep_on if submit else True,
        f"{env_file} 的 ACPS_PEP_ENABLED=" + ("1（会走 grant 裁决）" if pep_on else "未开/缺失"))
    if not pep_on and submit:
        add("pep-note", False,
            "PEP 未开时 grant 不会被裁决（框架默认 accept），拿不到 domain-grant 证据", required=False)

    endpoint = supervisor_endpoint(supervisor_workspace)
    add("supervisor-endpoint", bool(endpoint), endpoint or "ACS 里没有 JSONRPC 端点")
    if submit and endpoint:
        ok, detail = _tcp_reachable(endpoint)
        add("supervisor-reachable", ok, detail)

    # 已存在的域章程是否与本次参数兼容（不匹配时 mint 会覆盖失败）
    domains = supervisor_workspace / "acps" / "domains"
    if domains.is_dir():
        try:
            policies = load_domain_policies(
                domains, trust_bundle=supervisor_workspace / "acps" / "trust-bundle.pem")
            add("existing-charter", True,
                f"已存在章程 {sorted(policies)}（兼容则复用）", required=False)
        except Exception as exc:  # noqa: BLE001
            add("existing-charter", False, f"章程目录读取失败：{type(exc).__name__}: {exc}")
    else:
        add("existing-charter", True, "尚无 domains 目录（将新建）", required=False)
    return checks


def mutual_trust(supervisor_workspace: Path, peer_workspace: Path) -> list[dict[str, Any]]:
    """用双方证书各签一条测试 JWS，再用对端 trust-bundle 验——提前暴露信任链问题。"""
    checks: list[dict[str, Any]] = []
    pairs = (
        ("peer→supervisor", peer_workspace, supervisor_workspace),
        ("supervisor→peer", supervisor_workspace, peer_workspace),
    )
    for label, signer_ws, verifier_ws in pairs:
        try:
            signing = entry_grant.signing_context(signer_ws)
            jws = sign_payload(signing, {"probe": "p2-selftest", "at": _now()}, typ="TEST")
            payload, signer = verify_payload_jws(
                jws, trust_bundle=entry_grant.trust_bundle(verifier_ws))
            ok = signer == signing.issuer_aic and payload.get("probe") == "p2-selftest"
            checks.append({"id": f"trust-{label}", "ok": ok,
                           "detail": f"签署 {signing.issuer_aic}，对端验签 {'通过' if ok else '失败'}",
                           "required": True})
        except Exception as exc:  # noqa: BLE001
            checks.append({"id": f"trust-{label}", "ok": False,
                           "detail": f"{type(exc).__name__}: {exc}", "required": True})
    return checks


def ensure_charter(
    *,
    supervisor_workspace: Path,
    peer_workspace: Path,
    peer_aic: str,
    domain: str,
    skills: Sequence[str],
    out_dir: Path,
    extra_consumers: Sequence[str] = (),
) -> dict[str, Any]:
    """已有兼容章程则复用；没有则双签并安装；不兼容则拒绝（要求人工升版）。"""
    supervisor_signing = entry_grant.signing_context(supervisor_workspace)
    supervisor_aic = supervisor_signing.issuer_aic
    domains_dir = supervisor_workspace / "acps" / "domains"
    trust = entry_grant.trust_bundle(supervisor_workspace)
    policies = load_domain_policies(domains_dir, trust_bundle=trust) if domains_dir.is_dir() else {}
    policy = policies.get(domain)
    if policy is not None:
        role = policy.member_role(peer_aic)
        assets_ok = all(
            policy.asset_allows(provider_aic=supervisor_aic, skill_id=skill, role=role or "")
            for skill in skills
        )
        extras_ok = all(policy.member_role(aic) == "consumer" for aic in extra_consumers)
        compatible = (
            policy.status == "active"
            and role is not None
            and policy.member_role(supervisor_aic) == "provider"
            and assets_ok
            and extras_ok
        )
        if not compatible:
            raise SystemExit(
                f"已存在域 {domain}（r{policy.revision}）但与本次参数不兼容："
                f"role={role} assets_ok={assets_ok} extras_ok={extras_ok}。"
                f"请用 entry_grant prepare/cosign/install 出一份 r{policy.revision + 1}"
                f"（带 --previous）而不是覆盖 r{policy.revision}。"
            )
        return {"mode": "reused", "revision": policy.revision, "charter": "",
                "signers": sorted(policy.signer_aics)}

    manifest = entry_grant.build_entry_manifest(
        domain=domain, supervisor_aic=supervisor_aic, peer_aic=peer_aic, skills=skills,
        extra_consumers=extra_consumers,
    )
    signed = entry_grant.sign_manifest(manifest, supervisor_signing)
    signed, signers = entry_grant.cosign_manifest(signed, workspace=peer_workspace)
    if len(signers) < 2:
        raise SystemExit(f"联签后签名不足：{sorted(signers)}")
    entry_grant.assert_charter_usable(entry_grant.manifest_body(signed))
    path, policy = entry_grant.install_charter(
        signed, workspace=supervisor_workspace, domains_dir=domains_dir)
    charter_copy = out_dir / "charter.signed.json"
    entry_grant.write_json(charter_copy, signed)
    return {"mode": "minted", "revision": policy.revision,
            "charter": str(path), "signers": sorted(signers)}


def issue_and_verify(
    *,
    supervisor_workspace: Path,
    peer_aic: str,
    domain: str,
    revision: int,
    skills: Sequence[str],
    max_uses: int,
    ttl_days: int,
    out_dir: Path,
) -> dict[str, Any]:
    supervisor_aic = entry_grant.signing_context(supervisor_workspace).issuer_aic
    grant, document = entry_grant.issue_entry_grant(
        workspace=supervisor_workspace,
        subject_aic=peer_aic,
        domain=domain,
        revision=revision,
        provider_aic=supervisor_aic,
        skills=skills,
        max_uses=max_uses,
        ttl_days=ttl_days,
    )
    grant_path = entry_grant.write_json(out_dir / "entry-grant.json", document, secret=True)
    verified = entry_grant.check_grant_against_charter(
        document, workspace=supervisor_workspace)
    entry_grant.write_json(out_dir / "grant-verify.json", verified)
    return {
        "path": str(grant_path),
        "jti": grant.jti,
        "expiresAt": grant.expires_at,
        "maxUses": int(grant.claims["acps_max_uses"]),
        "assets": [f"{p}:{s}" for p, s in grant.assets],
        "verified": verified,
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """从产物文本里取第一个 JSON 对象（自动提交会加一行前缀）。"""
    start = text.find("{")
    decoder = json.JSONDecoder()
    while start != -1:
        try:
            obj, _end = decoder.raw_decode(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    return None


def _text_of(result: Mapping[str, Any]) -> str:
    return "\n".join(str(p.get("text") or "") for p in (result.get("product_previews") or []))


def _receipt_of(result: Mapping[str, Any]) -> dict[str, Any] | None:
    for preview in result.get("product_previews") or []:
        found = _extract_json(str(preview.get("text") or ""))
        if found is not None:
            return found
    return None


def _ids_from_text(text: str) -> tuple[str, str]:
    """回执是纯文本时（旧版作业回执）从正文里取 root_id / job_id。"""
    root = _ROOT_ID_RE.search(text or "")
    job = _JOB_ID_RE.search(text or "")
    return (root.group(0) if root else "", job.group(0) if job else "")


def _run_probe(
    *,
    identity_workspace: Path,
    supervisor_aic: str,
    endpoint: str,
    skill: str,
    args: Mapping[str, Any],
    grant_path: str,
    timeout_s: float,
    poll_s: float,
) -> dict[str, Any]:
    identity = load_identity(identity_workspace)
    cfg = ProbeConfig(
        identity=identity,
        peer_aic=supervisor_aic,
        peer_endpoint=endpoint,
        skill_id=skill,
        args=dict(args),
        grant_file=grant_path,
        timeout_s=timeout_s,
        poll_s=poll_s,
    )
    return asyncio.run(run_probe(cfg))


def submit_once(**kwargs) -> dict[str, Any]:
    return _run_probe(**kwargs)


def status_once(**kwargs) -> dict[str, Any]:
    return _run_probe(**kwargs)


def poll_until_done(
    *,
    identity_workspace: Path,
    supervisor_aic: str,
    endpoint: str,
    root_id: str = "",
    job_id: str = "",
    grant_path: str,
    out_dir: Path,
    interval_s: float,
    overall_timeout_s: float,
) -> dict[str, Any]:
    started = time.time()
    timeline: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    index = 0
    while time.time() - started < max(30.0, overall_timeout_s):
        index += 1
        result = status_once(
            identity_workspace=identity_workspace, supervisor_aic=supervisor_aic,
            endpoint=endpoint, skill="research.status",
            args={"root_id": root_id} if root_id else {"job_id": job_id},
            grant_path=grant_path, timeout_s=120.0, poll_s=1.0,
        )
        entry_grant.write_json(out_dir / f"status-{index:02d}.json", result)
        receipt = _receipt_of(result) or {}
        state = str(receipt.get("state") or "")
        timeline.append({"at": _now(), "state": state,
                         "elapsed_s": round(time.time() - started, 1)})
        last = receipt
        if state in _TERMINAL:
            return {"receipt": receipt, "timeline": timeline, "calls": index}
        if str(receipt.get("error") or "") in {"unknown-root", "forbidden"}:
            return {"receipt": receipt, "timeline": timeline, "calls": index}
        time.sleep(max(5.0, float(interval_s)))
    return {"receipt": last, "timeline": timeline, "calls": index, "timeout": True}


def _summary_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# P2 自证报告",
        "",
        f"- 时间：{report.get('generated_at')}",
        f"- 判定：**{str(report.get('verdict') or '').upper()}**",
        f"- supervisor：`{report.get('supervisor_aic')}`",
        f"- 入口伙伴：`{report.get('peer_aic')}`",
        "",
        "## 章程与 grant",
        "",
        f"- 章程：{report.get('charter', {}).get('mode')} r{report.get('charter', {}).get('revision')}"
        f"（signers={report.get('charter', {}).get('signers')}）",
        f"- grant：jti=`{report.get('grant', {}).get('jti')}` "
        f"expiresAt={report.get('grant', {}).get('expiresAt')} "
        f"maxUses={report.get('grant', {}).get('maxUses')}",
        f"- grant 文件：`{report.get('grant', {}).get('path')}`",
        "",
    ]
    submit = report.get("submit") or {}
    if submit:
        lines += [
            "## 入口调用",
            "",
            f"- 平台 taskId（T1）：`{submit.get('task_id')}`",
            f"- 终态：`{submit.get('final_state')}`",
            f"- root_id：`{submit.get('root_id')}` / job_id：`{submit.get('job_id')}`",
            "",
        ]
    status = report.get("status") or {}
    if status:
        lines += [
            "## 状态轮询",
            "",
            f"- 查询次数：{status.get('calls')}",
            f"- 终态：`{(status.get('receipt') or {}).get('state')}`",
            f"- 结果字符数：{(status.get('receipt') or {}).get('result_chars')}",
            "",
        ]
    lines += ["## 检查", ""]
    for item in report.get("checks") or []:
        mark = "OK  " if item.get("ok") else ("FAIL" if item.get("required", True) else "WARN")
        lines.append(f"- [{mark}] {item.get('id')}: {item.get('detail')}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m acps_cluster.tools.p2_selftest",
        description="P2 一键自证：双签章程 → 发 grant → 核验 →（可选）真实入口调用",
    )
    parser.add_argument("--supervisor-workspace", default="acps_cluster/ws-supervisor")
    parser.add_argument("--peer-workspace", default="acps_cluster/ws-kb")
    parser.add_argument("--peer-aic", default="", help="默认从 peer 工作区证书读")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--skills", default=",".join(entry_grant.ENTRY_SKILLS))
    parser.add_argument("--max-uses", type=int, default=entry_grant.DEFAULT_MAX_USES)
    parser.add_argument("--ttl-days", type=int, default=entry_grant.DEFAULT_TTL_DAYS)
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--extra-consumer", action="append", default=[],
                        help="预先列入章程、但本次不签名的其他入口 AIC（可重复，例如问迹）")
    parser.add_argument("--env-file", default="acps_cluster/deploy/.env.production")
    parser.add_argument("--submit", action="store_true",
                        help="真跑一次 research.submit（会创建后台作业，有模型成本）")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--request-id", default="")
    parser.add_argument("--user-scope", default="demo")
    parser.add_argument("--status-interval", type=float, default=60.0)
    parser.add_argument("--status-timeout", type=float, default=1800.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    supervisor_ws = Path(args.supervisor_workspace)
    peer_ws = Path(args.peer_workspace)
    out_dir = _out_dir(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {"generated_at": _now(), "checks": [], "verdict": "incomplete"}
    checks: list[dict[str, Any]] = preflight(
        supervisor_workspace=supervisor_ws,
        peer_workspace=peer_ws,
        env_file=Path(args.env_file),
        submit=bool(args.submit),
    )
    checks += mutual_trust(supervisor_ws, peer_ws)
    report["checks"] = checks
    required_failed = [c for c in checks if c["required"] and not c["ok"]]
    if required_failed:
        entry_grant.write_json(out_dir / "report.json", report)
        print("[p2] 预检未通过：", file=sys.stderr)
        for item in required_failed:
            print(f"  [{item['id']}] {item['detail']}", file=sys.stderr)
        print(f"[p2] 预检结果已写入 {out_dir / 'report.json'}", file=sys.stderr)
        return 2

    supervisor_aic = entry_grant.signing_context(supervisor_ws).issuer_aic
    peer_aic = args.peer_aic or entry_grant.signing_context(peer_ws).issuer_aic
    skills = entry_grant.parse_skills(args.skills)
    endpoint = supervisor_endpoint(supervisor_ws)
    report.update({
        "supervisor_aic": supervisor_aic, "peer_aic": peer_aic,
        "supervisor_endpoint": endpoint,
    })

    try:
        charter = ensure_charter(
            supervisor_workspace=supervisor_ws, peer_workspace=peer_ws,
            peer_aic=peer_aic, domain=args.domain, skills=skills, out_dir=out_dir,
            extra_consumers=getattr(args, "extra_consumer", []) or [],
        )
        grant_info = issue_and_verify(
            supervisor_workspace=supervisor_ws, peer_aic=peer_aic,
            domain=args.domain, revision=int(charter["revision"]), skills=skills,
            max_uses=args.max_uses, ttl_days=args.ttl_days, out_dir=out_dir,
        )
    except (SystemExit, ValueError, FileNotFoundError) as exc:
        report["checks"].append({"id": "charter-grant", "ok": False,
                                 "detail": f"{type(exc).__name__}: {exc}", "required": True})
        entry_grant.write_json(out_dir / "report.json", report)
        print(f"[p2] 签发失败：{exc}", file=sys.stderr)
        return 1

    report["charter"] = charter
    report["grant"] = grant_info
    report["checks"].append({"id": "charter-grant", "ok": True,
                             "detail": f"{charter['mode']} r{charter['revision']}；"
                                       f"grant jti={grant_info['jti']}", "required": True})

    if not args.submit:
        report["verdict"] = "minted"
        entry_grant.write_json(out_dir / "report.json", report)
        (out_dir / "report.md").write_text(_summary_markdown(report), encoding="utf-8")
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=1))
        else:
            print(f"[p2] 章程 {charter['mode']} r{charter['revision']}，"
                  f"grant 已签发并核验：{grant_info['path']}")
            print("[p2] 下一步（会创建一个真实后台作业）：")
            print(f"  python -m acps_cluster.tools.p2_selftest "
                  f"--supervisor-workspace {supervisor_ws} --peer-workspace {peer_ws} --submit")
            print(f"[p2] 证据目录：{out_dir}")
        return 0

    request_id = args.request_id or f"p2-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
    submit = submit_once(
        identity_workspace=peer_ws, supervisor_aic=supervisor_aic, endpoint=endpoint,
        skill="research.submit",
        args={"question": args.question, "request_id": request_id,
              "user_scope": args.user_scope},
        grant_path=grant_info["path"], timeout_s=180.0, poll_s=1.0,
    )
    entry_grant.write_json(out_dir / "submit.json", submit)
    receipt = _receipt_of(submit) or {}
    root_id = str(receipt.get("root_id") or "")
    job_id = str(receipt.get("job_id") or "")
    if not root_id:
        text_root, text_job = _ids_from_text(_text_of(submit))
        root_id = root_id or text_root
        job_id = job_id or text_job
    report["submit"] = {
        "task_id": submit.get("task_id"),
        "final_state": submit.get("final_state"),
        "ok": submit.get("ok"),
        "request_id": request_id,
        "root_id": root_id,
        "job_id": job_id,
        "receipt": receipt,
        "error": submit.get("error"),
    }
    if not submit.get("ok") or not (root_id or job_id):
        report["checks"].append({"id": "entry-submit", "ok": False,
                                 "detail": f"入口调用失败：state={submit.get('final_state')} "
                                           f"error={submit.get('error') or '无产物/无 root_id'}",
                                 "required": True})
        report["verdict"] = "failed"
        entry_grant.write_json(out_dir / "report.json", report)
        (out_dir / "report.md").write_text(_summary_markdown(report), encoding="utf-8")
        print(f"[p2] 入口调用失败：{report['checks'][-1]['detail']}", file=sys.stderr)
        return 1
    report["checks"].append({"id": "entry-submit", "ok": True,
                             "detail": f"T1={submit.get('task_id')} root={root_id}", "required": True})

    status = poll_until_done(
        identity_workspace=peer_ws, supervisor_aic=supervisor_aic, endpoint=endpoint,
        root_id=root_id, job_id=job_id, grant_path=grant_info["path"], out_dir=out_dir,
        interval_s=args.status_interval, overall_timeout_s=args.status_timeout,
    )
    report["status"] = status
    final_state = str((status.get("receipt") or {}).get("state") or "")
    ok = final_state == "completed"
    report["checks"].append({"id": "root-completed", "ok": ok,
                             "detail": f"root={root_id} state={final_state or '未知'}",
                             "required": True})
    report["verdict"] = "passed" if ok else "failed"
    entry_grant.write_json(out_dir / "report.json", report)
    (out_dir / "report.md").write_text(_summary_markdown(report), encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"[p2] 入口 T1={submit.get('task_id')} root={root_id} → {final_state or '未知'}"
              f"（查询 {status.get('calls')} 次）")
        print(f"[p2] 证据目录：{out_dir}")
    return 0 if ok else 1


__all__ = [
    "build_parser",
    "ensure_charter",
    "issue_and_verify",
    "main",
    "mutual_trust",
    "poll_until_done",
    "preflight",
    "supervisor_endpoint",
]

if __name__ == "__main__":
    raise SystemExit(main())