#!/usr/bin/env python3
"""部署前自检：把"能不能跑"变成可执行判断，而不是靠肉眼。

在服务器上执行（root 或 wit 均可，除非某项需要读别人家目录）：

    /opt/wit/venv/bin/python deploy/preflight_check.py
    /opt/wit/venv/bin/python deploy/preflight_check.py --json

## 为什么需要它

这条链路上有四个曾经真实踩坑、且**失败时看起来都像"脚本坏了"**的地方：

1. 模型网关只认校园网源 IP → 服务器直连必然超时（`docs/model-routing-bupt-deepseek.md`）；
2. 本地 dev 证书 vs 平台证书 → TLS 能连上但握手被拒，报 `ReadError`；
3. `amp_hooks` 没挂 → 调用全成功、排行榜恒 0（`incident-2026-10-19-ranking-zero.md`）；
4. systemd 启动时 `rm -f logs/*.jsonl` → access 证据每重启清空一次。

本脚本逐项探测并给出**可执行的下一步**，避免在第 5 步才发现第 1 步没做。

## 输出约定

- `[OK]`   通过
- `[WARN]` 不阻塞，但要知道
- `[FAIL]` 阻塞：不修就会白跑

退出码：0 全部可用 / 1 有阻塞项 / 2 只有告警。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import sys
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
PROJECT = CLUSTER.parent

RESULTS: list[dict] = []


def record(level: str, area: str, message: str, hint: str = "") -> None:
    RESULTS.append({"level": level, "area": area, "message": message, "hint": hint})
    tag = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
    print(f"{tag} {area:14s} {message}")
    if hint and level != "OK":
        for line in hint.strip().splitlines():
            print(f"          → {line}")


def load_env_file(path: Path) -> None:
    """照 jobs_runner 的口径覆盖式加载（与 systemd EnvironmentFile 行为一致）。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        value = value.strip().strip('"').strip("'")
        key = key.strip()
        if key and value and not value.startswith("<"):   # 跳过 <填入...> 占位符
            os.environ[key] = value


# ── 各项检查 ──────────────────────────────────────────────────────────────────

def check_files() -> None:
    required = [
        "supervisor.py",
        "feed_tasks.py",
        "deploy/patch_amp_access.py",
        "deploy/amp_access_sender.py",
        "deploy/wit-amp-access-sender.service",
        "deploy/wit-task-feeder.service",
        "model_router.py",
        "llm_tools.py",
    ]
    missing = [p for p in required if not (CLUSTER / p).is_file()]
    if missing:
        record("FAIL", "文件", f"缺 {len(missing)} 个文件: {', '.join(missing)}",
               "先把这些文件同步到服务器（见 docs/ 下的部署说明）")
    else:
        record("OK", "文件", f"{len(required)} 个必需文件齐全")


def check_env() -> None:
    primary = (os.getenv("ACPS_MODEL_PRIMARY") or "bupt").strip().lower()
    bupt = all(os.getenv(k) for k in ("BUPT_BASE_URL", "BUPT_API_KEY", "BUPT_MODEL"))
    fb = all(os.getenv(k) for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"))

    if primary in {"none", "off", "0", "false"}:
        record("WARN", "模型配置", "主用已禁用（ACPS_MODEL_PRIMARY=none），全部走 MiniMax",
               "这是合法配置；若本想用网关，去掉该行")
    elif not bupt:
        record("WARN", "模型配置", "主用已启用但 BUPT_* 不完整 → 实际只会走保底",
               "补 BUPT_BASE_URL / BUPT_API_KEY / BUPT_MODEL")
    else:
        extra = ""
        proxy = (os.getenv("BUPT_HTTP_PROXY") or "").strip()
        if proxy:
            extra = f"（经代理 {proxy}）"
        record("OK", "模型配置", f"主用={os.getenv('BUPT_MODEL')}{extra}")

    if not fb:
        record("FAIL", "模型配置", "保底（OPENAI_*）不完整：主用一挂就没有退路",
               "补 OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL")
    else:
        record("OK", "模型配置", f"保底={os.getenv('OPENAI_MODEL')}")


def check_model_live(do_call: bool) -> None:
    """真实探测模型可用性：先看连通性，必要时打一次极小的真实请求。"""
    sys.path.insert(0, str(CLUSTER))
    try:
        import model_router  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        record("FAIL", "模型连通", f"无法导入 model_router: {type(exc).__name__}")
        return

    targets = model_router.ordered_targets()
    if not targets:
        record("FAIL", "模型连通", "没有任何可用目标（BUPT_* 与 OPENAI_* 都不可用）")
        return

    for target in targets:
        label = target.label
        try:
            host, port = _host_port(target.base_url)
            with socket.create_connection((host, port), timeout=8):
                pass
            reachable = True
            err = ""
        except Exception as exc:  # noqa: BLE001
            reachable = False
            err = f"{type(exc).__name__}: {str(exc)[:80]}"

        if not reachable:
            level = "WARN" if target.name != "bupt" else "WARN"
            hint = ""
            if target.name == "bupt":
                hint = ("服务器不在校园网内时必然连不上。两条路：\n"
                        "1) 笔记本执行 deploy/start_campus_egress.sh，并设 BUPT_HTTP_PROXY=http://127.0.0.1:18888\n"
                        "2) 设 ACPS_MODEL_PRIMARY=none，全部走保底（不影响可用性）")
            record(level, "模型连通", f"{label} 不可达（{err}）", hint)
            continue

        record("OK", "模型连通", f"{label} TCP 可达 {host}:{port}")

        if not do_call:
            continue
        try:
            import asyncio  # noqa: PLC0415

            text, diag = asyncio.run(model_router.chat_via(
                target, "你是助手。", "只回复两个字：收到",
                temperature=0, max_tokens=2000, timeout=60))
            if text.strip():
                record("OK", "模型调用", f"{label} 真实调用成功（{diag.get('finish_reason')}）")
            else:
                record("WARN", "模型调用",
                       f"{label} 返回正文为空（finish={diag.get('finish_reason')}, "
                       f"thinking={diag.get('reasoning_chars')} 字符）",
                       "推理型模型 thinking 吃满 max_tokens；调用方需给足预算（项目用 24000）")
        except Exception as exc:  # noqa: BLE001
            record("WARN", "模型调用", f"{label} 调用失败：{type(exc).__name__}: {str(exc)[:100]}")


def _host_port(url: str) -> tuple[str, int]:
    from urllib.parse import urlparse  # noqa: PLC0415

    parsed = urlparse(url)
    return (parsed.hostname or "", int(parsed.port or (443 if parsed.scheme == "https" else 80)))


def _cert_cn(path: Path) -> str:
    """取证书 CN。

    注意：`openssl x509 -subject` 在长 DN 下会把输出折行成多行（"subject=...CN = \n<续行>"），
    早期实现按单行 split 会读不到 CN。这里先把整段输出压成一行再取。
    """
    import subprocess  # noqa: PLC0415

    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", str(path), "-noout", "-subject"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:  # noqa: BLE001
        return ""

    flat = " ".join(line.strip() for line in out.splitlines())
    flat = flat.replace("subject=", "").replace("Subject:", "")
    # 优先用 "CN = xxx" 形态（openssl 默认输出），兼容 "CN=xxx"
    import re  # noqa: PLC0415

    match = re.search(r"CN\s*=\s*([^,]+)", flat)
    return match.group(1).strip() if match else ""


def check_identity() -> None:
    """调用方证书 CN 必须等于平台登记的 AIC，否则对端会拒绝。"""
    ws = CLUSTER / "ws-supervisor"
    cert = ws / "acps" / "client" / "certificate.pem"
    if not cert.is_file():
        record("FAIL", "调用方身份", f"缺客户端证书 {cert}", "先在服务器上完成 supervisor 注册/领证")
        return

    cn = _cert_cn(cert)
    if not cn:
        record("WARN", "调用方身份", "读不出证书 CN（openssl 不可用？）")
        return

    # 期望值：平台覆盖表 > 本工作区 ACS
    expected = ""
    overlay = CLUSTER / "deploy" / "platform_identities.json"
    if overlay.is_file():
        try:
            expected = str((json.loads(overlay.read_text(encoding="utf-8-sig"))
                            .get("supervisor") or {}).get("aic") or "")
        except Exception:  # noqa: BLE001
            pass
    if not expected:
        acs = ws / "acps" / "acs.final.json"
        if acs.is_file():
            try:
                expected = str(json.loads(acs.read_text(encoding="utf-8-sig")).get("aic") or "")
            except Exception:  # noqa: BLE001
                pass

    if not expected:
        record("WARN", "调用方身份", f"CN={cn}；但查不到期望 AIC，无法核对",
               "检查 ws-supervisor/acps/enrollment.json 与 platform_identities.json")
        return

    if cn == expected:
        record("OK", "调用方身份", f"CN 与登记一致：{cn}")
        return

    dev = cn.startswith("1.2.156.3088.1.1.") and ".BUPT." not in cn
    hint = ("这是本地 dev CA 铸造的身份，平台不认。派发必须在持有平台证书的机器上跑"
            "（即运行 supervisor 的那台服务器）。")
    if not dev:
        hint = "CN 与平台登记不一致：对端会回 ReadError / 403。请核对两处 AIC。"
    record("FAIL", "调用方身份", f"CN={cn} ≠ 期望={expected}", hint)


def check_targets() -> None:
    """对每个目标做一次 TLS 握手并核对证书 CN，不发任何业务请求。"""
    sys.path.insert(0, str(CLUSTER))
    try:
        import feed_tasks  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        record("FAIL", "目标端点", f"无法导入 feed_tasks: {type(exc).__name__}")
        return

    agents = sorted({s["agent"] for s in feed_tasks.SPECS})
    bad: list[str] = []
    ok = 0
    for key in agents:
        aic = feed_tasks.aic_of(key)
        endpoint = feed_tasks.endpoint_of(key)
        if not aic or not endpoint:
            bad.append(f"{key}(无身份/端点)")
            continue
        host, port = _host_port(endpoint)
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, port), timeout=8) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    der = tls.getpeercert(binary_form=True)
            peer_cn = _peer_cn(der)
            if peer_cn != aic:
                bad.append(f"{key}(CN 不符: {peer_cn[-18:] or '?'})")
            else:
                ok += 1
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{key}({type(exc).__name__})")

    if bad:
        record("WARN", "目标端点", f"{ok}/{len(agents)} 可达且身份吻合；异常: {', '.join(bad)}",
               "不可达的 agent 派发会被跳过（记账为 exception），不影响其余")
    else:
        record("OK", "目标端点", f"{ok}/{len(agents)} 全部可达且证书 CN == 登记 AIC")


def _peer_cn(der: bytes) -> str:
    """从 DER 里取 CN：优先 cryptography，退到 openssl。"""
    try:
        from cryptography import x509  # noqa: PLC0415

        return x509.load_der_x509_certificate(der).subject.rfc4514_string().split("CN=")[-1].strip()
    except Exception:  # noqa: BLE001
        return ""


def check_amp_hooks() -> None:
    """每个 agent 模块是否已挂 amp_hooks（决定"调用是否被计数"）。"""
    import re  # noqa: PLC0415

    modules = [
        "supervisor", "partner_scout", "partner_synthesis", "partner_writer",
        "partner_critic", "partner_contradiction", "partner_frontier",
        "partner_experiment", "partner_gpu", "partner_cpu", "partner_eval",
        "partner_kb", "partner_model", "partner_citation",
    ]
    missing: list[str] = []
    present = 0
    for name in modules:
        path = CLUSTER / f"{name}.py"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^\s*amp_hooks\s*=", text, re.MULTILINE):
            present += 1
        else:
            missing.append(name)

    if missing:
        record("FAIL", "计量钩子", f"{present} 个已挂，{len(missing)} 个未挂: {', '.join(missing[:5])}"
                                   + ("…" if len(missing) > 5 else ""),
               "未挂的 agent 调用会成功但不计数（排行榜恒 0）\n"
               "修复：python3 deploy/patch_amp_access.py")
    else:
        record("OK", "计量钩子", f"{present} 个模块全部已挂 AmpAccessLoggingHooks")


def check_restart_cleanup() -> None:
    """启动时的 rm 通配符是否还会连 access 日志一起删。"""
    targets = [
        CLUSTER / "deploy" / "wit-yanshu@.service",
        CLUSTER / "deploy" / "run_supervisor.sh",
    ]
    offenders: list[str] = []
    for path in targets:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "logs/*.jsonl" in text and "amp_heartbeat_*.jsonl" not in text:
            offenders.append(path.name)
    if offenders:
        record("FAIL", "重启清理", f"{', '.join(offenders)} 仍会删除全部 *.jsonl",
               "每次重启会清空 access 证据（不可再生）\n"
               "修复：python3 deploy/patch_amp_access.py（会收窄为 amp_heartbeat_*.jsonl）")
    else:
        record("OK", "重启清理", "只清理 amp_heartbeat_*.jsonl，access 日志得以保留")


def check_kafka() -> None:
    try:
        import kafka  # noqa: F401, PLC0415

        record("OK", "Kafka 客户端", f"kafka-python 可用")
    except ImportError:
        record("FAIL", "Kafka 客户端", "缺 kafka-python：access 转发器无法投递",
               "/opt/wit/venv/bin/pip install kafka-python")


def check_state_dir() -> None:
    target = CLUSTER / ".feeder"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("OK", "账本目录", f"{target} 可写")
    except OSError as exc:
        record("FAIL", "账本目录", f"{target} 不可写: {exc}",
               "用 --state 指向可写目录（如 /var/lib/yanshu-feeder），否则账本无法持久化")


def check_script_encoding() -> None:
    """部署脚本必须是 LF：CRLF 的 .sh 在 Linux 上会报 `$'\\r': command not found`。

    这是本仓库既有问题（`deploy_cluster.sh` / `deploy_partner.sh` 由 Windows 侧编辑过），
    不影响本次上线链路（go_live/patch/sender 都是 LF），但会坑到以后用它的人。
    """
    offenders: list[str] = []
    scripts = sorted((CLUSTER / "deploy").glob("*.sh")) + sorted(CLUSTER.glob("*.sh"))
    for path in scripts:
        try:
            if b"\r\n" in path.read_bytes():
                offenders.append(path.name)
        except OSError:
            continue

    if offenders:
        record("WARN", "脚本换行", f"CRLF 换行: {', '.join(offenders)}",
               "在 Linux 上执行会报 $'\\r': command not found\n"
               "修复：sed -i 's/\\r$//' deploy/<文件名>.sh")
    else:
        record("OK", "脚本换行", f"{len(scripts)} 个 .sh 均为 LF")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="部署前自检（模型/身份/计量/端点）")
    ap.add_argument("--json", action="store_true", help="额外输出机器可读结果")
    ap.add_argument("--skip-model-call", action="store_true", help="不发起真实模型调用")
    ap.add_argument("--env-file", default=str(CLUSTER / "deploy" / ".env.production"))
    args = ap.parse_args(argv)

    print("=" * 66)
    print(" 研枢集群 · 部署前自检")
    print(f" 集群目录: {CLUSTER}")
    print(f" 环境文件: {args.env_file}")
    print("=" * 66)

    load_env_file(Path(args.env_file))

    check_files()
    check_env()
    check_model_live(do_call=not args.skip_model_call)
    check_identity()
    check_targets()
    check_amp_hooks()
    check_restart_cleanup()
    check_kafka()
    check_state_dir()
    check_script_encoding()

    fails = [r for r in RESULTS if r["level"] == "FAIL"]
    warns = [r for r in RESULTS if r["level"] == "WARN"]

    print("=" * 66)
    print(f" 结论: {len(RESULTS) - len(fails) - len(warns)} 通过 / "
          f"{len(warns)} 告警 / {len(fails)} 阻塞")
    if fails:
        print(" 有阻塞项，修好再跑（每条都标了怎么修）")
    elif warns:
        print(" 无阻塞项；告警项不拦路，但建议知情后再跑")
    else:
        print(" 全部通过，可以执行一键修复")
    print("=" * 66)

    if args.json:
        print(json.dumps({"results": RESULTS,
                          "fail": len(fails), "warn": len(warns)}, ensure_ascii=False, indent=2))

    return 1 if fails else (2 if warns else 0)


if __name__ == "__main__":
    raise SystemExit(main())
