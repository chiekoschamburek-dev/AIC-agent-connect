"""通用一次性工具：为本地开发 agent 铸造身份（AIC + 双证书 + 工作区）。

mint_gpu_identity.py 的通用版：AIC 各段、端口、agentId 均参数化，复用其密码学实现
（CRC 校验码按 ACPs-spec-AIC §4，证书镜像 ws-scout 扩展项、开发 Agent Intermediate CA 签发）。

用法（在 my-project 下，以实验员为例）：
    python acps_cluster/deploy/mint_identity.py \
        --ca-key ../ACPs-community/ca-server/certs/ca.key \
        --workspace ws-experiment --vendor EXPRMT --body LABEXE01 --entity RUN01 \
        --port 8461 --agent-id yanshu.experiment-partner
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEPLOY_DIR))

from mint_gpu_identity import (  # noqa: E402
    ALPHABET36, TEMPLATE_WS, aic_checksum, clear_registry_traces, load_ca, mint_certificate,
)
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ca-key", type=Path, required=True)
    parser.add_argument("--workspace", required=True, help="如 ws-experiment（须不存在）")
    parser.add_argument("--vendor", required=True, help="AIC 第 7 级，≤6 位大写字母数字")
    parser.add_argument("--body", required=True, help="AIC 第 8 级，≤9 位")
    parser.add_argument("--entity", required=True, help="AIC 第 9 级，≤9 位")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--agent-id", required=True)
    args = parser.parse_args()

    for name, value, limit in (("vendor", args.vendor, 6), ("body", args.body, 9), ("entity", args.entity, 9)):
        if not (1 <= len(value) <= limit) or not all(c in ALPHABET36 for c in value.upper()):
            raise SystemExit(f"AIC 段 {name}={value!r} 不合法（1..{limit} 位，字符集 0-9A-Z）")

    target_ws = TEMPLATE_WS.parent / args.workspace
    if target_ws.exists():
        raise SystemExit(f"{target_ws} 已存在；如需重铸请先手工删除")

    data_code = f"1.2.156.3088.1.1.{args.vendor}.{args.body}.{args.entity}"
    aic = f"{data_code}.{aic_checksum(data_code)}"

    ca_key, ca_cert = load_ca(args.ca_key)
    client_cert, client_key = mint_certificate(ca_key, ca_cert, aic, usage=ExtendedKeyUsageOID.CLIENT_AUTH)
    server_cert, server_key = mint_certificate(ca_key, ca_cert, aic, usage=ExtendedKeyUsageOID.SERVER_AUTH)

    shutil.copytree(TEMPLATE_WS / "acps", target_ws / "acps")

    def write_key(path: Path, key) -> None:
        path.write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption())
        )

    (target_ws / "acps" / "client" / "certificate.pem").write_bytes(
        client_cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    write_key(target_ws / "acps" / "client" / "private-key.pem", client_key)
    (target_ws / "acps" / "server" / "certificate.pem").write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    )
    write_key(target_ws / "acps" / "server" / "private-key.pem", server_key)

    account_dir = target_ws / "acps" / "account"
    for old in account_dir.glob("*.account-key.pem"):
        old.rename(account_dir / f"{aic.replace('.', '_')}.account-key.pem")
    for stale in (target_ws / "acps").glob("aip-tasks.sqlite3*"):
        stale.unlink()
    for stale in (target_ws / "acps").glob("acs.*.json"):
        stale.unlink()

    enrollment_path = target_ws / "acps" / "enrollment.json"
    data = json.loads(enrollment_path.read_text(encoding="utf-8"))

    def repath(value: str) -> str:
        return value.replace(str(TEMPLATE_WS), str(target_ws))

    data["agentId"] = args.agent_id
    data["acpsConfig"]["publicEndpoint"] = f"https://localhost:{args.port}/rpc"
    data["draftPath"] = repath(data["draftPath"])
    data["finalAcsPath"] = repath(data["finalAcsPath"])
    data["identity"]["ref"] = {"aic": aic, "provenance": "local-dev-ca"}
    data["identity"]["certificatePaths"] = {k: repath(v) for k, v in data["identity"]["certificatePaths"].items()}
    data["status"] = "active"
    data["certificateIssue"]["status"] = "issued"
    clear_registry_traces(data)  # 去掉从模板 ws 抄来的注册痕迹（见 mint_gpu_identity 内说明）
    enrollment_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"AIC       {aic}")
    print(f"工作区    {target_ws}")
    print(f"端口      {args.port} | agentId {args.agent_id}")


if __name__ == "__main__":
    sys.exit(main())
