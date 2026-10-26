"""换身份第 4 步：把工作区身份切到新 AIC（带 dry-run、可回滚）。

做的事（全部在目标工作区内，其他成员不受影响）：
1. enrollment.json：identity.ref.aic / review / acpsConfig / 指纹 → 平台记录，
   certificatePaths 指向目标工作区，finalAcsPath/draftPath 同步；
2. 用**平台权威 ACS** 覆盖 acs.final.json（内容 = 平台记录，含增强后的门面文本）；
3. acs.draft.json 同步为新 ACS（下次启动不会用旧草稿覆盖终稿）。

2026-10-25 workspace 切换修正：
- 本地 dev 工作区的 enrollment 里 `review` 是空对象。旧版只在
  `review.authoritativeAcs` **已存在**时才覆盖，也不补 metadata/registrationId，
  启动时 `_review_active` 会直接报 "Registry review active state is missing"。
  现在无条件构造平台 review（metadata.active/approvalStatus、authoritativeAcs、
  registrationId），并按平台配置重算 acpsConfig/acs 指纹。
- 拉平台 ACS 时用 ACPS_PUBLIC_HOST + ACPS_{PREFIX}_PORT，不再写死 scout 的 8445。

用法：
    CREDS_PREFIX=WORKSPACE TARGET_AIC=... python switch_step4_apply_identity.py \
        --ws ws-workspace --aic <AIC> --creds-prefix WORKSPACE --port 8470
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

CLUSTER = Path(os.getenv("YANSHU_CLUSTER_DIR", "/opt/wit/my-project/acps_cluster"))
REGISTRY = os.getenv("ACPS_REGISTRY_URL", "https://wt.ioa.pub/registry-server")
CA_URL = os.getenv("ACPS_CA_URL", "https://wt.ioa.pub/ca-server")
DISCOVERY = os.getenv("ACPS_DISCOVERY_URL", "https://wt.ioa.pub/discovery/acps-adp-v2/discover")

sys.path.insert(0, str(CLUSTER))
sys.path.insert(0, "/opt/wit/venv/lib/python3.12/site-packages")


def load_env_file() -> dict[str, str]:
    """读 .env.production + .env.workspace 并 setdefault 到 os.environ（显式 env 优先）。"""
    env: dict[str, str] = {}
    for name in (".env.production", ".env.workspace"):
        p = CLUSTER / "deploy" / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                env[k] = v
                os.environ.setdefault(k, v)
    return env


def env_creds(prefix: str) -> tuple[str, str]:
    env = load_env_file()
    return env[f"ACPS_{prefix}_USERNAME"], env[f"ACPS_{prefix}_PASSWORD"]


async def fetch_authoritative_acs_and_id(
    aic: str, user: str, pwd: str, *, port: int,
) -> tuple[dict, str]:
    from wit import AcpsConfig, AgentProviderProfile
    from wit.acps.registry import HttpRegistryClient, RegistryPasswordCredentials

    env = load_env_file()
    host = env.get("ACPS_PUBLIC_HOST", "localhost")
    cfg = AcpsConfig.custom(
        registry_url=env.get("ACPS_REGISTRY_URL", REGISTRY),
        ca_url=env.get("ACPS_CA_URL", CA_URL),
        discovery_url=env.get("ACPS_DISCOVERY_URL", DISCOVERY),
        public_endpoint=f"https://{host}:{port}/rpc",
        certificate=None,
        provider=AgentProviderProfile(
            name=env.get("ACPS_PROVIDER_NAME", "wit-research-lab"),
            organization=env.get("ACPS_PROVIDER_ORG", "北京邮电大学"),
            email=env.get("ACPS_PROVIDER_EMAIL", "huaibin2025@outlook.com"),
            country_code=env.get("ACPS_COUNTRY_CODE", "CN"),
        ),
    )
    client = HttpRegistryClient(acps=cfg)
    token = await client.login(RegistryPasswordCredentials(user, pwd))
    acs = await client.fetch_authoritative_acs(aic, token)
    registration_id = ""
    # registrationId 是 provisioner 对账用的；公开 ACS 接口不返回它，用客户端
    # 的按 name/version 查找补一次。查不到不影响 dry-run，但启动前最好补上。
    try:
        agent = await client._find_agent_by_name_version(
            name=str(acs.get("name") or ""), version=str(acs.get("version") or ""), token=token,
        )
        registration_id = str((agent or {}).get("id") or "")
    except Exception as exc:  # noqa: BLE001
        print(f"      ⚠ 查询 registrationId 失败（可手工 --registration-id 补）：{exc}")
    return acs, registration_id


def build_platform_review(
    enrollment: dict, *, aic: str, acs: dict, registration_id: str = "",
) -> dict:
    """把平台记录转成 wit 期望的 review 结构（纯函数，便于单测）。

    `_review_active` 读的是 `review.metadata.active`（布尔），不是 `review.status`；
    本地 dev enrollment 的 `review={}` 必须补齐这一层。
    """
    review = enrollment.setdefault("review", {})
    meta = dict(review.get("metadata") or {})
    meta.update({
        "active": bool(acs.get("active")),
        "approvalStatus": "APPROVED",
        "isDeleted": False,
        "isDisabled": False,
    })
    review["metadata"] = meta
    review["status"] = "approved"
    review["aic"] = aic
    review["authoritativeAcs"] = acs
    if registration_id:
        review["registrationId"] = registration_id
        # provisioner 用**顶层** registrationId 判断"提交态能否对账"；
        # 只写 review.registrationId 会在启动时报
        # changed_registration_missing_remote_id。
        enrollment["registrationId"] = registration_id
    review["reconciledAt"] = datetime.now(timezone.utc).isoformat()
    enrollment["status"] = "active" if acs.get("active") else "failed"
    enrollment["finalAcsSource"] = "registry"
    return enrollment


def apply_platform_identity(
    enrollment: dict,
    *,
    ws: Path,
    aic: str,
    acs: dict,
    acps_config: dict,
    acps_config_fingerprint: str,
    acs_fingerprint: str,
    registration_id: str = "",
) -> dict:
    acps = ws / "acps"
    identity = enrollment.setdefault("identity", {})
    identity.setdefault("ref", {})
    identity["ref"]["aic"] = aic
    identity["ref"]["provenance"] = "registry+acme"
    identity["status"] = "active"
    identity.setdefault("certificatePaths", {})
    identity["certificatePaths"].update({
        "clientCertificate": str(acps / "client" / "certificate.pem"),
        "clientPrivateKey": str(acps / "client" / "private-key.pem"),
        "serverCertificate": str(acps / "server" / "certificate.pem"),
        "serverPrivateKey": str(acps / "server" / "private-key.pem"),
        "trustBundle": str(acps / "trust-bundle.pem"),
    })
    ca_url = os.environ.get("ACPS_CA_URL", CA_URL)
    identity["metadata"] = {
        "caAtrUrl": f"{ca_url}/acps-atr-v2",
        "caBaseUrl": ca_url,
        "certificateKeyAlgorithm": str(acps_config.get("certificateKeyAlgorithm") or "ed25519"),
        "ontologyAic": None,
        "registrationKind": str((enrollment.get("registration") or {}).get("kind") or "integrated"),
        "strictInterop": True,
        "usages": ["clientAuth", "serverAuth"],
    }
    build_platform_review(enrollment, aic=aic, acs=acs, registration_id=registration_id)
    # 清掉上一次失败/候选态；否则即使补了 registrationId，provisioner 仍按
    # failed 状态走重新注册分支。
    for key in (
        "failure", "newAcsFingerprint", "newAcpsConfigFingerprint",
        "previousAcsFingerprint", "previousAcpsConfigFingerprint",
        "candidateDraftPath", "remoteRegistration", "reconciliation",
    ):
        enrollment.pop(key, None)
    enrollment["acpsConfig"] = acps_config
    enrollment["acpsConfigFingerprint"] = acps_config_fingerprint
    enrollment["acsFingerprint"] = acs_fingerprint
    enrollment["finalAcsPath"] = str(acps / "acs.final.json")
    enrollment["draftPath"] = str(acps / "acs.draft.json")
    return enrollment


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", required=True, help="工作区目录名，如 ws-workspace")
    ap.add_argument("--aic", required=True, help="目标 AIC（平台上 active 的那条）")
    ap.add_argument("--creds-prefix", required=True, help="凭据前缀，如 WORKSPACE")
    ap.add_argument("--port", type=int, default=0, help="服务端口；缺省读 ACPS_<PREFIX>_PORT")
    ap.add_argument("--registration-id", default="", help="平台 registrationId（可选，自动查）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    ws = CLUSTER / args.ws
    acps = ws / "acps"
    if not acps.is_dir():
        print(f"✘ 工作区不存在：{acps}")
        return 1

    prefix = args.creds_prefix.strip().upper()
    env = load_env_file()
    port = args.port or int(env.get(f"ACPS_{prefix}_PORT", "8470" if prefix == "WORKSPACE" else "8445"))
    user, pwd = env_creds(prefix)
    print(f"目标工作区 : {ws}")
    print(f"目标 AIC   : {args.aic}")
    print(f"凭据前缀   : {prefix}（端口 {port}）")
    print(f"平台账号   : {user}\n")

    print("[1/4] 拉取平台权威 ACS 与 registrationId …")
    acs, remote_reg_id = asyncio.run(
        fetch_authoritative_acs_and_id(args.aic, user, pwd, port=port)
    )
    if not acs.get("active"):
        print("✘ 平台记录不是 active，拒绝切换")
        return 1
    print(f"      name={acs.get('name')} version={acs.get('version')} "
          f"描述 {len(acs.get('description') or '')} 字 "
          f"skills={[s.get('id') for s in (acs.get('skills') or [])]}")
    print(f"      registrationId={args.registration_id or remote_reg_id or '（未取到）'}")

    print("[2/4] 计算平台配置与指纹 …")
    from cluster_common import build_acps_config
    from wit.acps.provisioner import (
        _fingerprint,
        _safe_acps_config,
        normalize_registration_candidate_acs,
    )

    cfg = build_acps_config(port)
    acps_config = _safe_acps_config(cfg)
    acps_config_fingerprint = _fingerprint(acps_config)
    acs_fingerprint = _fingerprint(normalize_registration_candidate_acs(acs))
    print(f"      acpsConfig.publicEndpoint={acps_config.get('publicEndpoint')}")
    print(f"      acsFingerprint={acs_fingerprint[:16]}… "
          f"acpsConfigFingerprint={acps_config_fingerprint[:16]}…")

    print("[3/4] 更新 enrollment.json …")
    enr_path = acps / "enrollment.json"
    enr = json.loads(enr_path.read_text(encoding="utf-8"))
    before = {
        "identity.ref.aic": (enr.get("identity") or {}).get("ref", {}).get("aic"),
        "review.metadata.active": ((enr.get("review") or {}).get("metadata") or {}).get("active"),
        "acsFingerprint": enr.get("acsFingerprint"),
    }
    apply_platform_identity(
        enr,
        ws=ws,
        aic=args.aic,
        acs=acs,
        acps_config=acps_config,
        acps_config_fingerprint=acps_config_fingerprint,
        acs_fingerprint=acs_fingerprint,
        registration_id=args.registration_id or remote_reg_id,
    )
    after = {
        "identity.ref.aic": (enr.get("identity") or {}).get("ref", {}).get("aic"),
        "review.metadata.active": ((enr.get("review") or {}).get("metadata") or {}).get("active"),
        "acsFingerprint": enr.get("acsFingerprint"),
    }
    for k in before:
        print(f"        {k}: {before[k]}\n{'':26s}→ {after[k]}")

    print("[4/4] 写回文件 …")
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
