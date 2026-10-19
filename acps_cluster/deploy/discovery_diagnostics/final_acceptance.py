"""换身份 + 转发器就绪后的统一验收（只读）。

检查项：
A. 两个 agent 的本地身份 / 证书 / 平台记录三者是否一致
B. 平台"按 AIC 查"（filtered+aic）对新 AIC 的结果（心跳未开通前预期仍为 0）
C. 平台 aliveMap 是否覆盖新 AIC（预期未覆盖，直到心跳进平台）
D. 心跳文件与转发器 dry-run 状态
"""

from __future__ import annotations

import json
import ssl
import subprocess
from pathlib import Path

import httpx

CL = Path("/opt/wit/my-project/acps_cluster")
ACS = "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/"
DISC = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"

TARGETS = {
    "研枢-Scout-Partner": ("ws-scout", "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5"),
    "研枢-Supervisor": ("ws-supervisor", "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"),
}
OLD = {"ws-scout": "1.2.156.3088.1.BUPT.VTF97R.C6HTRB.5TNX0M.17SZ",
       "ws-supervisor": "1.2.156.3088.1.BUPT.VTF97R.GGWW9N.KYWRB7.0IC8"}

ACPS = CL / "ws-scout" / "acps"
ctx = ssl.create_default_context()
ctx.load_verify_locations(cafile=str(CL / "ws-scout" / "acps" / "trust-bundle.pem"))
ctx.load_cert_chain(certfile=str(CL / "ws-scout" / "acps" / "client" / "certificate.pem"),
                    keyfile=str(CL / "ws-scout" / "acps" / "client" / "private-key.pem"))

print("=" * 76)
print("A. 三方身份一致性")
for name, (ws, aic) in TARGETS.items():
    w = CL / ws / "acps"
    enr = json.loads((w / "enrollment.json").read_text(encoding="utf-8"))
    acs = json.loads((w / "acs.final.json").read_text(encoding="utf-8"))
    cert = subprocess.run(["openssl", "x509", "-in", str(w / "client" / "certificate.pem"),
                           "-noout", "-subject"], capture_output=True, text=True).stdout
    print(f"\n  {name}  ({ws})")
    print(f"    enrollment = {enr['identity']['ref']['aic']}")
    print(f"    acs.final  = {acs.get('aic')}  v{acs.get('version')}  描述 {len(acs.get('description') or '')} 字")
    print(f"    证书 CN    = {cert.strip().replace('subject=CN = ', '').split(',')[0]}")
    ok = (enr['identity']['ref']['aic'] == aic == acs.get('aic') and aic in cert)
    print(f"    → 三者一致：{'✔' if ok else '✘'}   期望 {aic}")

print()
print("=" * 76)
print("B/C. 平台侧：按 AIC 查 + aliveMap 覆盖")
with httpx.Client(verify=ctx, timeout=40.0) as c:
    for name, (ws, aic) in TARGETS.items():
        body = {"type": "filtered", "limit": 5,
                "filter": {"conditions": [{"field": "aic", "op": "eq", "value": aic}]}}
        r = c.post(DISC, json=body)
        n = len(((r.json() or {}).get("result") or {}).get("acsMap") or {}) if r.status_code == 200 else -1
        print(f"  [按 AIC 查] {name:20s} HTTP {r.status_code} 结果 {n} 条 "
              f"{'✔ 查得到' if n > 0 else '✘ 仍查不到（等心跳）'}")
    r = c.post(DISC, json={"type": "trending", "limit": 50})
    am = ((r.json() or {}).get("result") or {}).get("aliveMap") or {}
    for name, (ws, aic) in TARGETS.items():
        covered = aic in am
        print(f"  [aliveMap]  {name:20s} {'✔ 已覆盖' if covered else '✘ 未覆盖（等心跳）'}")

print()
print("=" * 76)
print("D. 心跳与转发器")
for ws in ("ws-scout", "ws-supervisor"):
    files = sorted((CL / ws / "logs").glob("amp_heartbeat_*.jsonl"))
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
        last = json.loads(lines[-1]) if lines else {}
        print(f"  {ws:16s} {f.name[:52]:52s} {len(lines):4d} 条  最后 {last.get('timestamp')}")
print("\n  转发器检查点文件：", "存在" if (CL / '.amp-forwarder-offsets.json').exists() else "不存在（dry-run 不写）")
