"""换身份第 4 步：把工作区身份切到新 AIC（带 dry-run、可回滚）。

做的事（全部在目标工作区内，其他成员不受影响）：
1. enrollment.json：identity.ref.aic / review.aic / review.authoritativeAcs.aic → 新 AIC，
   certificatePaths 指向目标工作区，finalAcsPath/draftPath 同步；
2. 用**平台权威 ACS** 覆盖 acs.final.json（内容 = 平台记录，含增强后的门面文本）；
3. acs.draft.json 同步为新 ACS（下次启动不会用旧草稿覆盖终稿）。

用法：
    python switch_step4_apply_identity.py --ws ws-scout --aic <新AIC> [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

CLUSTER = Path("/opt/wit/my-project/acps_cluster")
REGISTRY = "https://wt.ioa.pub/registry-server"
CA_URL = "https://wt.ioa.pub/ca-server"

sys.path.insert(0, str(CLUSTER))
sys.path.insert(0, "/opt/wit/venv/lib/python3.12/site-packages")


def env_creds(prefix: str) -> tuple[str, str]:
    p = CLUSTER / "deploy" / ".env.production"
    env = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env[f"ACPS_{prefix}_USERNAME"], env[f"ACPS_{prefix}_PASSWORD"]


async def fetch_authoritative_acs(aic: str, user: str, pwd: str) -> dict:
    from wit import AcpsConfig, AgentProviderProfile
    from wit.acps.registry import HttpRegistryClient, RegistryPasswordCredentials

    cfg = AcpsConfig.custom(
        registry_url=REGISTRY, ca_url=CA_URL,
        discovery_url="https://wt.ioa.pub/discovery/acps-adp-v2/discover",
        public_endpoint="https://192.0.2.10:8445/rpc",
        certificate=None,
        provider=AgentProviderProfile(name="wit-research-lab", organization="北京邮电大学",
                                      email="huaibin2025@outlook.com", country_code="CN"),
    )
    client = HttpRegistryClient(acps=cfg)
    token = await client.login(RegistryPasswordCredentials(user, pwd))
    return await client.fetch_authoritative_acs(aic, token)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", required=True, help="工作区目录名，如 ws-scout")
    ap.add_argument("--aic", required=True, help="目标 AIC（平台上 active 的那条）")
    ap.add_argument("--creds-prefix", required=True, help="凭据前缀，如 SCOUT")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    ws = CLUSTER / args.ws
    acps = ws / "acps"
    if not acps.is_dir():
        print(f"✘ 工作区不存在：{acps}")
        return 1

    user, pwd = env_creds(args.creds_prefix)
    print(f"目标工作区 : {ws}")
    print(f"目标 AIC   : {args.aic}")
    print(f"平台账号   : {user}\n")

    print("[1/3] 拉取平台权威 ACS …")
    acs = asyncio.run(fetch_authoritative_acs(args.aic, user, pwd))
    print(f"      name={acs.get('name')} version={acs.get('version')} "
          f"描述 {len(acs.get('description') or '')} 字 "
          f"skills={[s.get('id') for s in (acs.get('skills') or [])]}")

    print("[2/3] 更新 enrollment.json …")
    enr_path = acps / "enrollment.json"
    enr = json.loads(enr_path.read_text(encoding="utf-8"))
    before = {
        "identity.ref.aic": (enr.get("identity") or {}).get("ref", {}).get("aic"),
        "review.aic": (enr.get("review") or {}).get("aic"),
        "review.authoritativeAcs.aic": ((enr.get("review") or {}).get("authoritativeAcs") or {}).get("aic"),
    }
    enr.setdefault("identity", {}).setdefault("ref", {})["aic"] = args.aic
    enr["identity"].setdefault("certificatePaths", {})
    enr["identity"]["certificatePaths"].update({
        "clientCertificate": str(acps / "client" / "certificate.pem"),
        "clientPrivateKey": str(acps / "client" / "private-key.pem"),
        "serverCertificate": str(acps / "server" / "certificate.pem"),
        "serverPrivateKey": str(acps / "server" / "private-key.pem"),
        "trustBundle": str(acps / "trust-bundle.pem"),
    })
    review = enr.setdefault("review", {})
    if "aic" in review:
        review["aic"] = args.aic
    if isinstance(review.get("authoritativeAcs"), dict):
        review["authoritativeAcs"]["aic"] = args.aic
        review["authoritativeAcs"] = acs          # 权威 ACS 用平台记录覆盖
    review["reconciledAt"] = datetime.now(timezone.utc).isoformat()
    enr["status"] = "active"
    enr["finalAcsPath"] = str(acps / "acs.final.json")
    enr["draftPath"] = str(acps / "acs.draft.json")

    print("      AIC 字段变更：")
    after = {
        "identity.ref.aic": (enr.get("identity") or {}).get("ref", {}).get("aic"),
        "review.aic": (enr.get("review") or {}).get("aic"),
        "review.authoritativeAcs.aic": ((enr.get("review") or {}).get("authoritativeAcs") or {}).get("aic"),
    }
    for k in before:
        print(f"        {k}: {before[k]}\n{'':26s}→ {after[k]}")

    print("[3/3] 写回文件 …")
    if args.dry_run:
        print("      （--dry-run：不写盘）")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in ("enrollment.json", "acs.final.json", "acs.draft.json"):
        src = acps / name
        if src.is_file():
            shutil.copy2(src, acps / f"{name}.pre-idswitch-{stamp}")
    enr_path.write_text(json.dumps(enr, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      ✔ enrollment.json 已更新（原件备份为 *.pre-idswitch-{stamp}）")

    body = json.dumps(acs, ensure_ascii=False, indent=2)
    (acps / "acs.final.json").write_text(body, encoding="utf-8")
    (acps / "acs.draft.json").write_text(body, encoding="utf-8")
    print("      ✔ acs.final.json / acs.draft.json 已替换为平台权威 ACS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
