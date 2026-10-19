"""全量验收：8 个 agent 的服务/身份/心跳/镜像/平台在线/门面文本，一次查完（只读）。"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import httpx

CL = Path(os.getenv("YANSHU_CLUSTER_DIR", "/opt/wit/my-project/acps_cluster"))
ACPS = CL / "ws-scout" / "acps"
D = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"
ACS = "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/"
MIRROR = Path("/var/log/acps-amp/yanshu")
SNAP = CL / "_platform_snapshot"   # 本地快照（可能不含最新），仅作参考


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ACPS / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(ACPS / "client" / "certificate.pem"),
                        keyfile=str(ACPS / "client" / "private-key.pem"))
    return ctx


def cert_cn(p: Path) -> str:
    if not p.is_file():
        return ""
    out = subprocess.run(["openssl", "x509", "-in", str(p), "-noout", "-subject"],
                         capture_output=True, text=True).stdout
    return out.strip().replace("subject=CN = ", "").split(",")[0].strip()


ctx = ssl_ctx()
hdr = (f"{'工作区':18s} {'服务':8s} {'身份一致':8s} {'心跳':8s} {'镜像':5s} "
       f"{'平台在线':8s} {'平台版本':9s} {'描述':6s} {'examples':8s}")
print(hdr)
print("-" * len(hdr))
bad = []
with httpx.Client(verify=ctx, timeout=40.0) as c, httpx.Client(timeout=25.0) as plain:
    for ws in sorted(CL.glob("ws-*")):
        acps = ws / "acps"
        if not acps.is_dir() or not (acps / "enrollment.json").is_file():
            continue
        name = ws.name
        if name in ("ws-gpu", "ws-partner") or name.endswith("-idswitch"):
            continue
        enr = json.loads((acps / "enrollment.json").read_text(encoding="utf-8"))
        aic = (enr.get("identity") or {}).get("ref", {}).get("aic") or ""
        if not aic:
            continue

        svc = "wit-yanshu@" + name.removeprefix("ws-") + ".service"
        active = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True).stdout.strip()

        identity_ok = cert_cn(acps / "client" / "certificate.pem") == aic

        hb = sorted((ws / "logs").glob("amp_heartbeat_*.jsonl")) if (ws / "logs").is_dir() else []
        if hb:
            lines = hb[-1].read_text(encoding="utf-8", errors="ignore").strip().splitlines()
            last = json.loads(lines[-1]) if lines else {}
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                    str(last.get("timestamp")).replace("Z", "+00:00"))).total_seconds()
            except Exception:  # noqa: BLE001
                age = -1
            hb_ok = str(last.get("aic")) == aic and 0 <= age <= 300
            hb_txt = f"{age:.0f}s" if age >= 0 else "?"
        else:
            hb_ok, hb_txt = False, "无"

        mirrored = MIRROR / f"amp_heartbeat_{name.removeprefix('ws-')}.jsonl"

        r = c.post(D, json={"type": "filtered", "limit": 5,
                            "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}})
        n = len((((r.json() or {}).get("result") or {}).get("acsMap") or {}))
        pr = plain.get(ACS + aic)
        if pr.status_code == 200:
            pj = pr.json()
            ver = pj.get("version")
            desc = len(pj.get("description") or "")
            ex = sum(len(s.get("examples") or []) for s in (pj.get("skills") or []))
        else:
            ver, desc, ex = f"HTTP{pr.status_code}", -1, -1

        flags = []
        if active != "active":
            flags.append("服务未起")
        if not identity_ok:
            flags.append("证书与 enrollment 不一致")
        if not hb_ok:
            flags.append(f"心跳异常({hb_txt})")
        if not mirrored.is_file():
            flags.append("镜像缺")
        if n == 0:
            flags.append("平台不在线")
        if flags:
            bad.append((name, flags))

        print(f"{name:18s} {active:8s} {str(identity_ok):8s} {hb_txt:8s} "
              f"{'✔' if mirrored.is_file() else '✘':5s} {('✔' if n else '✘'):8s} "
              f"{str(ver):9s} {desc:6d} {ex:<8d}")
        if flags:
            print(f"    ✘ {' / '.join(flags)}")

print("-" * len(hdr))
print(f"有问题的工作区：{len(bad)}")
for name, flags in bad:
    print(f"  {name}: {' / '.join(flags)}")

print()
print("=== 心跳链路服务 ===")
for s in ("wit-amp-mirror", "wit-amp-sender"):
    st = subprocess.run(["systemctl", "is-active", s + ".service"],
                        capture_output=True, text=True).stdout.strip()
    print(f"  {s:16s} {st}")

print()
print("=== 平台 alive 总盘 ===")
try:
    with httpx.Client(timeout=15.0) as c:
        r = c.get("https://wt.ioa.pub/discovery/admin/alive-sync/status")
        j = r.json()
        print(f"  aliveCount={j.get('aliveCount')} knownCount={j.get('knownCount')} "
              f"lastDelta={j.get('lastDeltaAppliedAt')}")
except Exception as exc:  # noqa: BLE001
    print(f"  查询失败：{exc}")
