"""在工作区副本上验证：能否为"已在平台注册、但本地没有身份"的 AIC 直接签发证书。

不触碰线上工作区：全程在 <workspace>-idswitch 副本里操作。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import sys
from pathlib import Path

import httpx

CLUSTER = Path("/opt/wit/my-project/acps_cluster")
TARGET_AIC = os.getenv("TARGET_AIC", "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5")  # scout 新记录
SOURCE_WS = Path(os.getenv("SOURCE_WS", str(CLUSTER / "ws-scout")))
TEST_WS = Path(os.getenv("TEST_WS", str(CLUSTER / "ws-scout-idswitch")))
REGISTRY = os.getenv("ACPS_REGISTRY_URL", "https://wt.ioa.pub/registry-server")
CA_URL = os.getenv("ACPS_CA_URL", "https://wt.ioa.pub/ca-server")
PUBLIC_ENDPOINT = os.getenv("ACPS_PUBLIC_ENDPOINT", "https://192.0.2.10:8445/rpc")

sys.path.insert(0, "/opt/wit/venv/lib/python3.12/site-packages")


def env_creds() -> tuple[str, str]:
    p = CLUSTER / "deploy" / ".env.production"
    env = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env["ACPS_SCOUT_USERNAME"], env["ACPS_SCOUT_PASSWORD"]


async def main() -> int:
    from wit import AcpsConfig, AgentProviderProfile
    from wit.acps.ca import HttpCaClient
    from wit.acps.registry import HttpRegistryClient, RegistryPasswordCredentials

    print(f"目标 AIC      : {TARGET_AIC}")
    print(f"源工作区      : {SOURCE_WS}")
    print(f"测试工作区    : {TEST_WS}")
    print(f"对外端点      : {PUBLIC_ENDPOINT}\n")

    user, pwd = env_creds()
    print(f"平台账号      : {user}（口令已设置）")

    # 1) 准备测试工作区（复制源工作区的 acps 布局）
    if TEST_WS.exists():
        shutil.rmtree(TEST_WS)
    TEST_WS.mkdir(parents=True)
    shutil.copytree(SOURCE_WS / "acps", TEST_WS / "acps", dirs_exist_ok=True)
    print(f"已复制工作区骨架到 {TEST_WS}")

    provider = AgentProviderProfile(
        name="wit-research-lab",
        organization=os.getenv("ACPS_PROVIDER_ORG", "北京邮电大学"),
        email=os.getenv("ACPS_PROVIDER_EMAIL", "huaibin2025@outlook.com"),
        country_code=os.getenv("ACPS_COUNTRY_CODE", "CN"),
    )
    acps_cfg = AcpsConfig.custom(
        registry_url=REGISTRY,
        ca_url=CA_URL,
        discovery_url="https://wt.ioa.pub/discovery/acps-adp-v2/discover",
        public_endpoint=PUBLIC_ENDPOINT,
        certificate=None,
        provider=provider,
    )

    registry = HttpRegistryClient(acps=acps_cfg)
    ca = HttpCaClient(acps=acps_cfg)

    print("\n[1/3] 登录平台（账号口令）…")
    token = await registry.login(RegistryPasswordCredentials(user, pwd))
    print(f"      ✔ 拿到 token（{len(token.access_token)} 字符）")

    print("[2/3] 为目标 AIC 取 EAB 凭据 …")
    try:
        eab = await registry.fetch_eab_credential(TARGET_AIC, token)
        keys = sorted(eab.keys())
        print(f"      ✔ EAB 字段：{keys}")
    except Exception as exc:  # noqa: BLE001
        print(f"      ✘ 失败：{type(exc).__name__}: {exc}")
        return 1

    print("[3/3] 向平台 CA 申请证书（keyAuth/ACME + EAB）…")
    # 用平台记录里的权威 ACS 作为 config（含 altNames 等）
    try:
        auth_acs = await registry.fetch_authoritative_acs(TARGET_AIC, token)
        print(f"      权威 ACS：name={auth_acs.get('name')} version={auth_acs.get('version')} "
              f"endpoints={[e.get('url') for e in (auth_acs.get('endPoints') or [])]}")
    except Exception as exc:  # noqa: BLE001
        print(f"      权威 ACS 拉取失败：{type(exc).__name__}: {exc}")
        return 1

    # AcpsEnrollmentConfig 需要工作区内的路径集合（与 provisioner 内部构造一致）
    try:
        from wit import AcpsEnrollmentConfig
    except ImportError:
        from wit.acps.provisioner import AcpsEnrollmentConfig  # type: ignore

    acps_dir = TEST_WS / "acps"
    secrets_dir = acps_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    cfg = AcpsEnrollmentConfig(
        acps=acps_cfg,
        state_dir=acps_dir,
        token_path=secrets_dir / "registry-token.json",
        client_certificate_dir=acps_dir / "client",
        server_certificate_dir=acps_dir / "server",
        final_acs_path=acps_dir / "acs.final.json",
        aip_task_store_path=acps_dir / "aip-tasks.sqlite3",
        aip_group_store_path=acps_dir / "aip-groups.sqlite3",
    )
    print(f"      已构造 AcpsEnrollmentConfig（state_dir={acps_dir}）")

    try:
        result = await ca.issue_certificate_pair(
            aic=TARGET_AIC,
            eab_credential=eab,
            config=cfg,
            public_endpoint=PUBLIC_ENDPOINT,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"      ✘ 签发失败：{type(exc).__name__}: {exc}")
        return 1

    paths = result.certificate_paths
    print("      ✔ 签发成功，证书路径：")
    for attr in ("client_certificate", "client_private_key", "server_certificate",
                 "server_private_key", "trust_bundle", "clientCertificate",
                 "clientPrivateKey", "serverCertificate", "serverPrivateKey", "trustBundle"):
        if hasattr(paths, attr):
            print(f"          {attr} = {getattr(paths, attr)}")

    # 校验新证书里的 AIC
    import subprocess

    cert = (getattr(paths, "client_certificate", None)
            or getattr(paths, "clientCertificate", None))
    if cert and Path(cert).is_file():
        out = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-noout", "-subject", "-issuer",
             "-dates", "-ext", "subjectAltName"],
            capture_output=True, text=True,
        ).stdout
        print("      新证书内容：")
        for line in out.strip().splitlines():
            print("        ", line.strip())
    else:
        print(f"      （未找到客户端证书路径，paths={paths}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
