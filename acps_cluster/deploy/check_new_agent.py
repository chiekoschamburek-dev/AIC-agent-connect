"""新增 agent 的自检工具：四件事一次查完，并给出该做什么。

检查项（每一对都是一个真实踩过的坑）：
1. 工作区是否有有效身份（enrollment.aic + 证书 CN/SAN 是否一致）
2. 平台记录是否 active（两个平台都查）
3. 证书是否与平台记录同一个 AIC（"服务自称新 AIC、证书却是旧的"这个坑）
4. 心跳是否在被转发器采集（文件名 glob 是否能匹配、新鲜度是否在窗口内）
5. 平台是否认为它在线（按 AIC 查 + aliveMap）

用法：
    python check_new_agent.py --ws ws-mynew [--key mynew]
    python check_new_agent.py --all          # 检查全部 ws-*
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

CLUSTER = Path(os.getenv("YANSHU_CLUSTER_DIR", "/opt/wit/my-project/acps_cluster"))
ACS_BASE = "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/"
DISCOVER = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"
MIRROR_DIR = Path("/var/log/acps-amp/yanshu")

# 已知的"非在跑身份"——自检时跳过并标注原因（避免 --all 常红，掩盖真问题）
SKIP_WORKSPACES: dict[str, str] = {
    "ws-partner": "arXiv 检索 Partner，已于 2026-09-27 退役（能力由 scout 承接）",
    "ws-gpu": "本地开发身份（dev CA 铸造，未在平台注册；AIC 非法定 OID）",
}


def is_temp_copy(ws: Path) -> str | None:
    """换身份过程留下的临时副本（-idswitch / .bak）——不是在用工作区。"""
    if ws.name.endswith("-idswitch"):
        return "换身份流程的临时副本（证书已搬到正式工作区后可删）"
    if ".bak" in ws.name:
        return "备份副本"
    return None


def cert_subject(cert: Path) -> str:
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-subject"],
                         capture_output=True, text=True).stdout
    return out.strip().replace("subject=CN = ", "").split(",")[0].strip()


def newest_heartbeat(ws: Path) -> tuple[Path | None, datetime | None, int]:
    files = sorted((ws / "logs").glob("amp_heartbeat_*.jsonl")) if (ws / "logs").is_dir() else []
    if not files:
        return None, None, 0
    f = files[-1]
    lines = f.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
    ts = None
    if lines:
        try:
            rec = json.loads(lines[-1])
            ts = datetime.fromisoformat(str(rec.get("timestamp", "")).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            ts = None
    return f, ts, len(lines)


def check(ws: Path, client: httpx.Client) -> bool:
    name = ws.name
    acps = ws / "acps"
    ok = True
    print(f"\n{'=' * 74}\n### {name}")

    enr_path, acs_path = acps / "enrollment.json", acps / "acs.final.json"
    if not enr_path.is_file():
        print("  ✘ 没有 enrollment.json —— 该工作区尚未完成注册（先跑 partner 脚本注册）")
        return False

    enr = json.loads(enr_path.read_text(encoding="utf-8"))
    acs = json.loads(acs_path.read_text(encoding="utf-8")) if acs_path.is_file() else {}
    aic_enroll = (enr.get("identity") or {}).get("ref", {}).get("aic")
    aic_acs = acs.get("aic")
    cert = acps / "client" / "certificate.pem"
    aic_cert = cert_subject(cert) if cert.is_file() else ""

    print(f"  1) 身份：enrollment={aic_enroll}")
    print(f"           acs.final ={aic_acs}")
    print(f"           证书 CN   ={aic_cert}")
    same = aic_enroll and aic_enroll == aic_acs == aic_cert
    if not same:
        ok = False
        print("     ✘ 三者不一致 —— 走「换身份」流程："
              "sign_cert_for_aic → switch_step4_apply_identity → 搬证书 → 重启")
    else:
        print("     ✔ 三者一致")

    if not aic_enroll:
        return False

    r = client.get(ACS_BASE + aic_enroll)
    if r.status_code == 200:
        j = r.json()
        print(f"  2) 平台记录：HTTP 200 active={j.get('active')} version={j.get('version')} "
              f"描述 {len(j.get('description') or '')} 字 skills={[s.get('id') for s in (j.get('skills') or [])]}")
        if not j.get("active"):
            ok = False
            print("     ✘ 记录未激活 —— 需要平台审核通过 / 或记录已被停用（要重新注册或换身份）")
        if len(j.get("description") or "") < 120:
            print("     ⚠ 描述偏短：自然语言问法（叮当）可能匹配不上，建议补门面文本后重新提交 ACS")
    else:
        ok = False
        print(f"  2) 平台记录：HTTP {r.status_code} {r.text[:110]}")
        print("     ✘ 平台没有这条有效记录（未注册 / 被停用 / AIC 写错）")

    hb, ts, lines = newest_heartbeat(ws)
    print(f"  3) 心跳文件：{hb.name if hb else '【无】'}  行数 {lines}")
    if not hb:
        ok = False
        print("     ✘ 没有 amp_heartbeat_*.jsonl —— 服务没跑起来，或日志目录不对")
    else:
        age = (datetime.now(timezone.utc) - ts).total_seconds() if ts else None
        print(f"     最后一条 {ts.isoformat() if ts else '?'}"
              f"{f'（{age:.0f}s 前）' if age is not None else ''}")
        if age is None or age > 300:
            ok = False
            print("     ✘ 超过镜像的 300s 新鲜度窗口 —— 该 agent 的心跳不会被转发（先确保服务在跑）")
        if hb.name.split("_")[-1].replace(".jsonl", "") != aic_enroll:
            ok = False
            print("     ✘ 心跳文件名里的 AIC 与 enrollment 不一致（旧进程残留？重启服务）")
        mirrored = MIRROR_DIR / f"amp_heartbeat_{name.removeprefix('ws-')}.jsonl"
        print(f"  4) 镜像目录：{mirrored}  {'✔ 已出现' if mirrored.is_file() else '✘ 还没有（等一轮，或检查 mirror 服务）'}")
        if not mirrored.is_file():
            ok = False

    r = client.post(DISCOVER, json={"type": "filtered", "limit": 5,
                                    "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic_enroll}]}})
    result = (r.json() or {}).get("result") or {}
    alive = (result.get("aliveMap") or {}).get(aic_enroll)
    n = len(result.get("acsMap") or {})
    print(f"  5) 平台在线：按 AIC 查 {n} 条；aliveMap alive={alive.get('alive') if alive else None}")
    if n == 0:
        ok = False
        print("     ✘ 平台还不认为它在线 —— 等 1–3 分钟；仍不行则查 wit-amp-sender 的 journal")
    else:
        print("     ✔ 平台认为在线")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", default=None, help="工作区目录名，如 ws-mynew")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    acps = CLUSTER / "ws-scout" / "acps"
    ctx = None
    try:
        import ssl  # noqa: PLC0415

        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(acps / "trust-bundle.pem"))
        ctx.load_cert_chain(certfile=str(acps / "client" / "certificate.pem"),
                            keyfile=str(acps / "client" / "private-key.pem"))
    except Exception as exc:  # noqa: BLE001
        print(f"（无法构造 mTLS 上下文：{exc}；发现查询可能失败）")

    targets = ([CLUSTER / args.ws] if args.ws
               else sorted(p for p in CLUSTER.glob("ws-*") if p.is_dir()))
    results = {}
    skipped: dict[str, str] = {}
    with httpx.Client(verify=ctx, timeout=30.0) as client:
        for ws in targets:
            if not (ws / "acps").is_dir():
                continue
            reason = SKIP_WORKSPACES.get(ws.name) or is_temp_copy(ws)
            if reason and not args.ws:
                skipped[ws.name] = reason
                continue
            results[ws.name] = check(ws, client)

    if skipped:
        print(f"\n{'=' * 74}\n跳过（已登记的非在跑身份）：")
        for name, reason in skipped.items():
            print(f"  - {name:24s} {reason}")
    print(f"\n{'=' * 74}")
    print("汇总：", {k: ("✔" if v else "✘") for k, v in results.items()})
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
