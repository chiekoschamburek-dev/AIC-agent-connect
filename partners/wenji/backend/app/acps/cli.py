"""问迹 ACPs 接入命令行（在 backend/ 目录下运行）：

    python -m app.acps.cli check                   # 本地校验（声明/ACS/配置，无网络）
    python -m app.acps.cli show-acs [--output P]   # 生成 ACS 草稿（注册平台上传/人工检查）
    python -m app.acps.cli register                # 推进可信注册（登录→草稿→提交→审核）
    python -m app.acps.cli serve                   # 注册激活后启动 AIP Direct Partner
    python -m app.acps.cli status                  # 查看本地注册/服务状态

环境变量统一从仓库根目录 .env 加载（ACPS_* 见 .env.acps.example）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parents[3]


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_DIR / ".env", override=False)
        load_dotenv(REPO_DIR / ".env.acps", override=False)
    except Exception:  # pragma: no cover - .env 可选
        pass


def _build() -> "AcpsRuntime":  # noqa: F821 - 延迟导入
    from .runtime import AcpsRuntime
    from .settings import AcpsSettings

    return AcpsRuntime(AcpsSettings.from_env())


def _print_enrollment(result: Any) -> None:
    payload: dict[str, Any] = {}
    for key in ("status", "next_action", "registration_id"):
        value = getattr(result, key, None)
        if value:
            payload[key] = value
    for key in ("ready", "waiting_for_review", "blocked"):
        value = getattr(result, key, None)
        if value is not None:
            payload[key] = bool(value)
    summary = getattr(result, "summary", None)
    if callable(summary):
        payload["summary"] = summary()
    if not payload and hasattr(result, "to_dict"):
        payload = result.to_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _public_endpoint_warning(endpoint: str) -> str | None:
    """注册前提醒：公网端点不能是回环/私有地址。"""
    from urllib.parse import urlparse

    host = (urlparse(endpoint).hostname or "").lower()
    if not endpoint:
        return "ACPS_PUBLIC_ENDPOINT 未配置：注册前必须填写平台/队友可访问的 HTTPS 地址"
    if "<" in host or ">" in host:
        return f"ACPS_PUBLIC_ENDPOINT 仍是占位符（{host}）：请填真实域名或 IP 后再注册"
    if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return f"ACPS_PUBLIC_ENDPOINT 指向本机（{host}）：平台与其它智能体将无法回调"
    if host.startswith(("10.", "192.168.", "169.254.")) or host.startswith("172."):
        parts = host.split(".")
        if len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
            return f"ACPS_PUBLIC_ENDPOINT 是私有地址（{host}）：仅同网段可访问"
        if host.startswith(("10.", "192.168.", "169.254.")):
            return f"ACPS_PUBLIC_ENDPOINT 是私有地址（{host}）：仅同网段可访问"
    return None


# ----------------------------------------------------------------- commands

def cmd_check(args: argparse.Namespace) -> int:
    rt = _build()
    print("== ACPs 配置 ==")
    print(json.dumps(rt.describe(), ensure_ascii=False, indent=2))
    warning = _public_endpoint_warning(rt.settings.public_endpoint_or_default())
    if warning:
        print(f"\n[!] {warning}\n", file=sys.stderr)
    validation = asyncio.run(rt.validate())
    print("== 声明校验 ==")
    print(json.dumps(validation, ensure_ascii=False, indent=2, default=str))
    draft = rt.acs_draft()
    skills = [s.get("id") for s in draft.get("skills", [])]
    print(f"== ACS 草稿 == skills={skills}")
    endpoints = [e.get("url") for e in draft.get("endPoints", [])]
    print(f"   endpoints={endpoints}")
    return 0 if validation.get("valid") else 1


def cmd_show_acs(args: argparse.Namespace) -> int:
    rt = _build()
    draft = rt.acs_draft()
    warning = _public_endpoint_warning(rt.settings.public_endpoint_or_default())
    if warning:
        print(f"[!] {warning}\n", file=sys.stderr)
    output = Path(args.output) if args.output else REPO_DIR / "deploy" / "acps" / "acs.wenji.draft.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(draft, ensure_ascii=False, indent=2))
    print(f"\n[saved] {output}", file=sys.stderr)
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    rt = _build()

    async def _loop():
        while True:
            result = await rt.enroll(serve=False)
            print("== 注册状态 ==", flush=True)
            _print_enrollment(result)
            if getattr(result, "ready", False) or getattr(result, "blocked", False):
                return result
            if not args.watch:
                return result
            print(f"[watch] 等待审核，{args.interval}s 后重试（Ctrl+C 退出）…", flush=True)
            await asyncio.sleep(args.interval)

    result = asyncio.run(_loop())
    if args.serve and getattr(result, "ready", False):
        return cmd_serve(args)
    if getattr(result, "status", "") == "needs_login":
        print(
            "\n[hint] 未检测到平台账号凭据：请在 .env.acps 填写 ACPS_USERNAME / ACPS_PASSWORD"
            "（即注册平台登录账号），保存后重跑本命令；\n"
            "       若平台要求网页上传 ACS，请用 show-acs 的输出在注册平台提交，"
            "审核通过后再回来继续。",
            file=sys.stderr,
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    rt = _build()

    async def _run() -> None:
        from wit import RegistryPasswordCredentials
        import os

        credentials = None
        if os.environ.get("ACPS_USERNAME"):
            credentials = RegistryPasswordCredentials.from_env()
        result = await rt.runtime.up_until_ready(
            serve=True,
            host=rt.settings.listen_host,
            port=rt.settings.listen_port,
            credentials=credentials,
        )
        print("== 服务状态 ==")
        _print_enrollment(result)
        if not getattr(result, "ready", False):
            raise SystemExit("身份未就绪，请先完成注册/审核（python -m app.acps.cli register）")
        print(f"[serving] AIP Direct: {rt.settings.listen_host}:{rt.settings.listen_port}")
        print(f"[endpoint] {rt.settings.public_endpoint_or_default()}")
        print(f"[health]   http(s)://{rt.settings.listen_host}:{rt.settings.listen_port}/health")
        await asyncio.Event().wait()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n[stop] 收到中断，正在停止服务 …")
        asyncio.run(rt.stop())
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    rt = _build()
    status = rt.runtime.acps_status()
    print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    """本地端到端冒烟：不经 AIP 直接调用桥接层（需 DB；问答需 LLM key）。"""
    from . import bridge

    try:
        overview = bridge.schema_overview(max_tables=3)
        print(f"[ok] schema: {overview['table_count']} tables, 样例={[t['name'] for t in overview['tables']]}")
        if args.question:
            result = bridge.answer_question(args.question)
            print(json.dumps({k: result.get(k) for k in ("answer", "intent", "status", "latency_ms")},
                             ensure_ascii=False, indent=2))
        if args.doc_question:
            result = bridge.doc_qa(args.doc_question)
            print(json.dumps({k: result.get(k) for k in ("answer", "status", "citations", "latency_ms")},
                             ensure_ascii=False, indent=2))
    except Exception as exc:  # 冒烟失败时给出人类可读提示
        print(f"[fail] 冒烟未通过：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("[hint] 先启动依赖：./deploy/quick_start.sh（DB/知识库），再试 smoke", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    parser = argparse.ArgumentParser(prog="app.acps.cli", description="问迹 ACPs 接入工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="本地校验（不访问网络）")
    p_check.set_defaults(func=cmd_check)

    p_acs = sub.add_parser("show-acs", help="生成 ACS 草稿")
    p_acs.add_argument("--output", default="", help="输出路径（默认 deploy/acps/acs.wenji.draft.json）")
    p_acs.set_defaults(func=cmd_show_acs)

    p_reg = sub.add_parser("register", help="推进可信注册")
    p_reg.add_argument("--serve", action="store_true", help="注册就绪后直接启动服务")
    p_reg.add_argument("--watch", action="store_true", help="未就绪时按间隔轮询（盯审核）")
    p_reg.add_argument("--interval", type=float, default=120.0, help="轮询间隔秒数（默认 120）")
    p_reg.set_defaults(func=cmd_register)

    p_serve = sub.add_parser("serve", help="启动 AIP Direct Partner 服务")
    p_serve.set_defaults(func=cmd_serve)

    p_status = sub.add_parser("status", help="查看本地注册状态")
    p_status.set_defaults(func=cmd_status)

    p_smoke = sub.add_parser("smoke", help="本地端到端冒烟（不经 AIP）")
    p_smoke.add_argument("--question", default="", help="跑一次综合问答")
    p_smoke.add_argument("--doc-question", default="", help="跑一次文档问答")
    p_smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
