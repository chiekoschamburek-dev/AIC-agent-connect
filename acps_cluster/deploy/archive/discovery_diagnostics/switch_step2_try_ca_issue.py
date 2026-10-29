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


def _load_env_file() -> None:
    """把 deploy/.env.production + .env.workspace 读进 os.environ（显式 env 优先）。

    脚本通常由 runbook 手动执行，未必已经 set -a; . .env.production；之前只有
    env_creds() 读文件，ACPS_PUBLIC_HOST/PORT 等仍取默认值，workspace 会拿到
    scout 的 8445 端点。provider 等 workspace 专属项放 .env.workspace，避免
    影响其他伙伴的 acpsConfig 指纹。
    """
    for name in (".env.production", ".env.workspace"):
        p = CLUSTER / "deploy" / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env_file()
CREDS_PREFIX = os.getenv("CREDS_PREFIX", "SCOUT").strip().upper() or "SCOUT"
TARGET_AIC = os.getenv("TARGET_AIC", "").strip()
SOURCE_WS = Path(os.getenv("SOURCE_WS", str(CLUSTER / f"ws-{CREDS_PREFIX.lower()}")))
TEST_WS = Path(os.getenv("TEST_WS", str(SOURCE_WS) + "-idswitch"))
REGISTRY = os.getenv("ACPS_REGISTRY_URL", "https://wt.ioa.pub/registry-server")
CA_URL = os.getenv("ACPS_CA_URL", "https://wt.ioa.pub/ca-server")
PUBLIC_HOST = os.getenv("ACPS_PUBLIC_HOST", "localhost")
PORT = os.getenv(f"ACPS_{CREDS_PREFIX}_PORT", "8470" if CREDS_PREFIX == "WORKSPACE" else "8445")
PUBLIC_ENDPOINT = os.getenv("ACPS_PUBLIC_ENDPOINT", f"https://{PUBLIC_HOST}:{PORT}/rpc")

sys.path.insert(0, "/opt/wit/venv/lib/python3.12/site-packages")
sys.path.insert(0, str(CLUSTER))
try:
    # 触发 cluster_common 里的 wit CSR SAN 补丁；否则 IP 端点领证必报
    # server_public_endpoint_san_mismatch（见 docs/issue-wit-2.1.0-ip-endpoint-san.md）
    from acps_cluster.core import cluster_common  # noqa: F401
except Exception:  # noqa: BLE001
    pass


def env_creds() -> tuple[str, str]:
    return (
        os.environ[f"ACPS_{CREDS_PREFIX}_USERNAME"],
        os.environ[f"ACPS_{CREDS_PREFIX}_PASSWORD"],
    )


async def main() -> int:
    from wit import AcpsConfig, AgentProviderProfile
    from wit.acps.ca import HttpCaClient
    from wit.acps.registry import HttpRegistryClient, RegistryPasswordCredentials

    if not TARGET_AIC:
        print("✘ 必须设置 TARGET_AIC（平台 active 的那条 AIC）；workspace 示例：")
        print("  CREDS_PREFIX=WORKSPACE TARGET_AIC=1.2.156.3088.1.BUPT.VTF97R.ABKIJE.U8KHPP.149W \\")
        print("  python deploy/discovery_diagnostics/switch_step2_try_ca_issue.py")
        return 1

    try:
        from wit.acps import ca as _ca

        if not getattr(_ca._generate_csr, "_yanshu_san_patch", False):
            print("✘ CSR SAN 补丁未生效（cluster_common 未成功导入）；IP 端点会继续被")
            print("  server_public_endpoint_san_mismatch 拒绝，先修导入再领证。")
            return 1
    except Exception as exc:  # noqa: BLE001
        print(f"✘ 无法检查 CSR SAN 补丁：{type(exc).__name__}: {exc}")
        return 1

    print(f"目标 AIC      : {TARGET_AIC}")
    print(f"凭据前缀      : {CREDS_PREFIX}")
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
    # 源工作区若带着旧身份的证书材料（如路线 B 本地铸造：SAN=localhost），
    # 必须清掉再签发——否则平台 CA 校验旧材料，报 server_public_endpoint_san_mismatch
    # （2026-10-25 workspace 切换实测）。新证书由本脚本按目标 AIC/新 SAN 重新签发。
    for _rel in ("client/certificate.pem", "client/private-key.pem",
                 "server/certificate.pem", "server/private-key.pem", "trust-bundle.pem",
                 # 旧 ACS 也要清：材料生成器按 final ACS 的 endPoints 推 server SAN，
                 # 本地残留（endPoint=localhost）与公网端点比对必报 san_mismatch
                 "acs.final.json", "acs.draft.json", "acs.candidate.json"):
        (TEST_WS / "acps" / _rel).unlink(missing_ok=True)
    print(f"已复制工作区骨架到 {TEST_WS}（旧证书材料已清除）")

    # SAN 请求：与 cluster_common.build_certificate 同源（ACPS_PUBLIC_HOST + ACPS_CERT_SAN_KIND）
    from wit import AgentCertificateRequest
    _host = os.getenv("ACPS_PUBLIC_HOST", "localhost")
    cert_request = (
        AgentCertificateRequest(dns=(_host,), ip=(_host,))
        if os.getenv("ACPS_CERT_SAN_KIND", "dns").lower() == "ip"
        else AgentCertificateRequest(dns=(_host,), ip=(_host,))
    )
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
        # 证书请求必须带公网端点的 SAN（IP 或 dns 按 ACPS_CERT_SAN_KIND），
        # 否则平台 CA 校验 server_public_endpoint_san_mismatch 拒签
        # （2026-10-25 workspace 切换实测；与 cluster_common.build_certificate 同源）。
        certificate=cert_request,
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

    # 把平台权威 ACS 预写进测试工作区的 acs.final.json：材料生成器按 final ACS 的
    # endPoints 推导 server SAN——没有它就会用本地残留（endPoint=localhost），
    # 与 public_endpoint 比对必报 server_public_endpoint_san_mismatch
    # （2026-10-25 workspace 切换实测；runbook §2 第 4 步本就要写权威 ACS，这里提前）。
    (TEST_WS / "acps" / "acs.final.json").write_text(
        json.dumps(auth_acs, ensure_ascii=False, indent=2), encoding="utf-8")
    print("      ✔ 权威 ACS 已预写进 acs.final.json（供材料生成器推导 SAN）")

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
