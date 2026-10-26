"""一次性工具：为 gpu-partner 铸造本地开发身份（AIC + 双证书 + ws-gpu 工作区）。

与 ws-scout 等成员的身份同源：都用 ACPs-community 开发 CA（ca-server/certs/ca.key，
即 "ACPs Development Agent Intermediate CA"）签发，验证方 trust-bundle 无需任何改动。

AIC 按 ACPs-spec-AIC 构造：
- 第 5 级版本=1，第 6 级 ARSP=1（与集群其余成员相同，开发 ARSP）；
- 第 7 级供应商序号=GP5060，第 8 级本体序号=SANDBOX1，第 9 级实体序号=RUN01；
- 第 10 级校验码按规范 §4 实现：CRC-16/CCITT-FALSE（poly 0x1021, init 0xFFFF,
  不反射, xorout 0）over 大写数据码 ASCII + 盐值；Base36 编码 4 位。
  盐值取规范示例的 0x1234（本地开发 ARSP 自维护值；真实 ARSP 盐值不公开）。

用法（在 my-project 下）：
    python acps_cluster/deploy/mint_gpu_identity.py \
        --ca-key ../ACPs-community/ca-server/certs/ca.key

幂等：已存在 ws-gpu 时拒绝执行（先手工删除再重铸）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CLUSTER_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_WS = CLUSTER_DIR / "ws-scout"
TARGET_WS = CLUSTER_DIR / "ws-gpu"
GPU_PORT = 8460
GPU_AGENT_ID = "yanshu.gpu-partner"

# AIC 数据码各段（第 5/6 级沿用集群惯例：版本 1、开发 ARSP 序号 1）
AIC_VENDOR = "GP5060"  # 第 7 级（≤6 位）
AIC_BODY = "SANDBOX1"  # 第 8 级（≤9 位）
AIC_ENTITY = "RUN01"  # 第 9 级（≤9 位）
SALT = b"\x12\x34"  # 规范示例盐值（本地开发 ARSP）

ALPHABET36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def crc16_ccitt_false(data: bytes) -> int:
    """AUTOSAR CRC-16/CCITT-FALSE：poly 0x1021, init 0xFFFF, 不反射, xorout 0。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


def base36_4(value: int) -> str:
    digits = []
    for _ in range(4):
        value, rem = divmod(value, 36)
        digits.append(ALPHABET36[rem])
    return "".join(reversed(digits))


def aic_checksum(data_code: str) -> str:
    return base36_4(crc16_ccitt_false(data_code.upper().encode("ascii") + SALT))


def build_aic() -> str:
    data_code = f"1.2.156.3088.1.1.{AIC_VENDOR}.{AIC_BODY}.{AIC_ENTITY}"
    return f"{data_code}.{aic_checksum(data_code)}"


def load_ca(ca_key_path: Path) -> tuple[ed25519.Ed25519PrivateKey, x509.Certificate]:
    key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise SystemExit(f"CA 私钥不是 Ed25519: {type(key).__name__}")
    # 中级 CA 证书从任一成员的 trust-bundle 里按 CN 取（签发链要放进客户端证书文件）
    bundle = x509.load_pem_x509_certificates((TEMPLATE_WS / "acps" / "trust-bundle.pem").read_bytes())
    for cert in bundle:
        cns = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if cns and cns[0].value == "ACPs Development Agent Intermediate CA":
            return key, cert
    raise SystemExit("trust-bundle 里找不到 Agent Intermediate CA 证书")


def mint_certificate(
    ca_key: ed25519.Ed25519PrivateKey,
    ca_cert: x509.Certificate,
    aic: str,
    *,
    usage: x509.ObjectIdentifier,
) -> tuple[x509.Certificate, ed25519.Ed25519PrivateKey]:
    """签发一张与 ws-scout 同款扩展项的叶子证书（SAN 带 acps:// URI 与 DNS:localhost）。"""
    key = ed25519.Ed25519PrivateKey.generate()
    name = x509.Name(
        [x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"), x509.NameAttribute(NameOID.COMMON_NAME, aic)]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                     key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, key_cert_sign=False, crl_sign=False,
                                     encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=True)
        # RFC 5280：CA 签发的叶子必须带 SKI/AKI，缺 AKI 会在 TLS 链校验时被拒
        # （实测：httpx/openssl 报 "Missing Authority Key Identifier"）
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(f"acps://{aic}"), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(ca_key, None)
    )
    return cert, key


def clear_registry_traces(data: dict) -> None:
    """去掉从模板 ws（TEMPLATE_WS=ws-scout）抄来的"已注册"痕迹。

    留着这些字段会让 wit 以为本 agent 已经提交/通过过，从而在首次真启动时按
    "已提交后指纹失配"（`acs_fingerprint_changed_after_submission`）处理。
    - `registrationId` / `review`：模板(scout)的注册记录 → 删 / 置 {}；
    - `acsFingerprint`：模板的指纹，且**本地铸造时点算不出真实的**（终稿 ACS 要等首次启动才生成）
      → 置 null，由 `cluster_common.ensure_local_acs` 维持 null（实测 wit 的 `_fingerprint` /
      `_final_acs_fingerprint` 在 4 种输入 × 2 种形态共 16 种组合下都复现不出已注册 agent 的
      存档值——基线是提交时刻在 provisioner 内部算的，写个"看起来合法"的错值更危险）；
    - `acpsConfigFingerprint`：置 null，由 `ensure_local_acs` 用
      `_fingerprint(_safe_acps_config(cfg))` 重算回填（该算法可精确复现存档值）；
    - `finalAcsSource`：模板写 "registry"（假的）→ 改 "local-dev-ca"。
    """
    data.pop("registrationId", None)
    data["review"] = {}
    data["acsFingerprint"] = None
    data["acpsConfigFingerprint"] = None
    data["finalAcsSource"] = "local-dev-ca"


def rewrite_enrollment(aic: str) -> None:
    path = TARGET_WS / "acps" / "enrollment.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    def repath(value: str) -> str:
        return value.replace(str(TEMPLATE_WS), str(TARGET_WS))

    data["agentId"] = GPU_AGENT_ID
    data["acpsConfig"]["publicEndpoint"] = f"https://localhost:{GPU_PORT}/rpc"
    data["draftPath"] = repath(data["draftPath"])
    data["finalAcsPath"] = repath(data["finalAcsPath"])
    identity = data["identity"]
    identity["ref"] = {"aic": aic, "provenance": "local-dev-ca"}
    identity["certificatePaths"] = {k: repath(v) for k, v in identity["certificatePaths"].items()}
    data["status"] = "active"
    data["certificateIssue"]["status"] = "issued"
    clear_registry_traces(data)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ca-key", type=Path, required=True, help="开发 Agent Intermediate CA 私钥 PEM")
    args = parser.parse_args()

    if TARGET_WS.exists():
        raise SystemExit(f"{TARGET_WS} 已存在；如需重铸请先手工删除")

    aic = build_aic()
    checksum_ok = aic_checksum("1.2.156.3088.1.1.34C2.478BDF.3GF546") == "0JU4"  # 规范 §4.1 示例自检
    if not checksum_ok:
        raise SystemExit("校验码实现未通过规范示例自检（§4.1: …3GF546 + SALT 0x1234 → 0JU4）")

    ca_key, ca_cert = load_ca(args.ca_key)
    client_cert, client_key = mint_certificate(ca_key, ca_cert, aic, usage=ExtendedKeyUsageOID.CLIENT_AUTH)
    server_cert, server_key = mint_certificate(ca_key, ca_cert, aic, usage=ExtendedKeyUsageOID.SERVER_AUTH)

    shutil.copytree(TEMPLATE_WS / "acps", TARGET_WS / "acps")

    def write_key(path: Path, key: ed25519.Ed25519PrivateKey) -> None:
        path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        # 2026-10-21：私钥只对属主可读（Linux 生效；Windows 仅置只读位）。
        try:
            path.chmod(0o600)
        except OSError:
            pass

    # 客户端证书文件按惯例带 leaf + 中级 CA 链（服务端/客户端 TLS 各取所需）
    (TARGET_WS / "acps" / "client" / "certificate.pem").write_bytes(
        client_cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    write_key(TARGET_WS / "acps" / "client" / "private-key.pem", client_key)
    (TARGET_WS / "acps" / "server" / "certificate.pem").write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM)
        + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    write_key(TARGET_WS / "acps" / "server" / "private-key.pem", server_key)

    # 账号密钥文件名带 AIC（本地演示不联网，内容仅占位）
    account_dir = TARGET_WS / "acps" / "account"
    for old in account_dir.glob("*.account-key.pem"):
        old.rename(account_dir / f"{aic.replace('.', '_')}.account-key.pem")

    # 任务库清零；ACS 草稿/终稿由 partner_gpu 首次启动时按真实能力生成
    for stale in (TARGET_WS / "acps").glob("aip-tasks.sqlite3*"):
        stale.unlink()
    for stale in (TARGET_WS / "acps").glob("acs.*.json"):
        stale.unlink()

    rewrite_enrollment(aic)
    print(f"AIC       {aic}")
    print(f"工作区    {TARGET_WS}")
    print(f"证书签发  Agent Intermediate CA（客户端 clientAuth + 服务端 serverAuth，SAN 含 DNS:localhost）")
    print("下一步    python acps_cluster/agents/partner_gpu.py --local   （本地直连模式起服务并生成 ACS）")


if __name__ == "__main__":
    sys.exit(main())
