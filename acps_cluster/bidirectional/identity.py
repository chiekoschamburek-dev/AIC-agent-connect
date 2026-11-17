"""本机 AIP 身份与对端寻址（不依赖 wit 运行时）。

一个"工作区身份"= `ws-<key>/acps/` 下平台签发/本地铸造的这几件东西：

    acs.final.json         ACS 投影（aic / endPoints / skills）
    enrollment.json        注册记录（review.aic / acpsConfig.publicEndpoint|discoveryUrl）
    client/certificate.pem mTLS 客户端证书（CN/SAN = AIC）
    client/private-key.pem 私钥
    trust-bundle.pem       平台 CA 链

探针用这份身份**以该 agent 的名义**发起 AIP 任务——`senderId` 必须等于证书 AIC，
否则对端的身份绑定会拒（`sender does not match expected`）。因此本模块只读文件，
不生成、不复制、不打印私钥。
"""

from __future__ import annotations

import json
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acps_cluster.core import bootstrap  # noqa: F401  (import 即加载仓库根 .env)

DEFAULT_DISCOVERY_URL = "https://wt.ioa.pub/api/v1/discovery"  # 仅供提示，不作为静默默认


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _jsonrpc_endpoint(acs: dict[str, Any]) -> str:
    for endpoint in acs.get("endPoints") or []:
        if not isinstance(endpoint, dict):
            continue
        if str(endpoint.get("transport") or "").upper() == "JSONRPC":
            url = str(endpoint.get("url") or "").strip()
            if url:
                return url
    return ""


@dataclass(frozen=True)
class WorkspaceIdentity:
    """一个工作区的 AIP 身份（只读快照）。"""

    workspace: Path
    aic: str = ""
    endpoint: str = ""
    discovery_url: str = ""
    cert_file: Path | None = None
    key_file: Path | None = None
    trust_bundle: Path | None = None
    skills: tuple[str, ...] = ()

    @property
    def acps_dir(self) -> Path:
        return self.workspace / "acps"

    def missing(self) -> list[str]:
        """缺件清单（空列表 = 可用于真实调用）。"""
        problems: list[str] = []
        if not self.aic:
            problems.append("ACS/enrollment 里没有 AIC")
        if not self.endpoint:
            problems.append("ACS/enrollment 里没有 JSONRPC 端点")
        if self.cert_file is None:
            problems.append("缺 acps/client/certificate.pem")
        if self.key_file is None:
            problems.append("缺 acps/client/private-key.pem")
        if self.trust_bundle is None:
            problems.append("缺 acps/trust-bundle.pem")
        return problems

    def ssl_context(self) -> ssl.SSLContext:
        """mTLS 客户端上下文（平台 CA + 本机证书）。缺件时抛 FileNotFoundError。"""
        problems = self.missing()
        if problems:
            raise FileNotFoundError("; ".join(problems))
        context = ssl.create_default_context(cafile=str(self.trust_bundle))
        context.load_cert_chain(str(self.cert_file), str(self.key_file))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "aic": self.aic,
            "endpoint": self.endpoint,
            "discovery_url": self.discovery_url,
            "cert_file": str(self.cert_file) if self.cert_file else "",
            "trust_bundle": str(self.trust_bundle) if self.trust_bundle else "",
            "skills": list(self.skills),
            "missing": self.missing(),
        }


def load_identity(workspace: str | Path) -> WorkspaceIdentity:
    """从工作区读出身份；文件缺失不抛异常（用 `missing()` 检查）。"""
    ws = Path(workspace)
    acps = ws / "acps"
    acs = _read_json(acps / "acs.final.json")
    enrollment = _read_json(acps / "enrollment.json")
    cfg = enrollment.get("acpsConfig") if isinstance(enrollment.get("acpsConfig"), dict) else {}
    review = enrollment.get("review") if isinstance(enrollment.get("review"), dict) else {}
    identity_ref = (enrollment.get("identity") or {}).get("ref") if isinstance(
        enrollment.get("identity"), dict
    ) else {}
    aic = (
        str(acs.get("aic") or "")
        or str(review.get("aic") or "")
        or str((identity_ref or {}).get("aic") or "")
        or os.getenv("ACPS_PEP_MY_AIC", "")
    )
    endpoint = _jsonrpc_endpoint(acs) or str(cfg.get("publicEndpoint") or "").strip()
    if endpoint and not endpoint.rstrip("/").endswith("/rpc"):
        endpoint = endpoint.rstrip("/") + "/rpc"
    discovery_url = (
        str(cfg.get("discoveryUrl") or "").strip()
        or str(os.getenv("ACPS_DISCOVERY_URL") or "").strip()
    )
    cert = acps / "client" / "certificate.pem"
    key = acps / "client" / "private-key.pem"
    trust = acps / "trust-bundle.pem"
    skills = tuple(
        str(skill.get("id"))
        for skill in (acs.get("skills") or [])
        if isinstance(skill, dict) and skill.get("id")
    )
    return WorkspaceIdentity(
        workspace=ws,
        aic=aic,
        endpoint=endpoint,
        discovery_url=discovery_url,
        cert_file=cert if cert.is_file() else None,
        key_file=key if key.is_file() else None,
        trust_bundle=trust if trust.is_file() else None,
        skills=skills,
    )


def peer_endpoint_from_local_workspaces(cluster_root: str | Path, aic: str) -> str:
    """在本机 `ws-*/acps/acs.final.json` 里按 AIC 找对端端点（自证场景常用）。

    找不到返回空串；不抛异常（调用方会回落到 discovery）。
    """
    if not aic:
        return ""
    root = Path(cluster_root)
    for acs_path in sorted(root.glob("ws-*/acps/acs.final.json")):
        acs = _read_json(acs_path)
        if str(acs.get("aic") or "") != aic:
            continue
        return _jsonrpc_endpoint(acs)
    return ""


def discover(
    identity: WorkspaceIdentity,
    query: str,
    *,
    limit: int = 10,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    """经平台 ADP 发现接口查候选（同步 httpx；缺依赖/缺地址时抛 RuntimeError）。

    返回 `[{aic, name, active, skills: [...]}]`；解析口径与 partners/wenji 的
    `aip_selftest.py` 一致（`acsMap` + `aliveMap`）。
    """
    if not identity.discovery_url:
        raise RuntimeError("工作区没有 discoveryUrl（enrollment.acpsConfig.discoveryUrl 为空）")
    try:
        import httpx  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 部署环境有 httpx
        raise RuntimeError("需要 httpx：pip install httpx") from exc

    payload = {"type": "explicit", "query": query, "limit": max(1, int(limit))}
    with httpx.Client(timeout=timeout) as client:
        response = client.post(identity.discovery_url, json=payload)
        response.raise_for_status()
        data = response.json()
    result = data.get("result") if isinstance(data, dict) else {}
    result = result if isinstance(result, dict) else {}
    acs_map = result.get("acsMap") if isinstance(result.get("acsMap"), dict) else {}
    alive_map = result.get("aliveMap") if isinstance(result.get("aliveMap"), dict) else {}
    agents: list[dict[str, Any]] = []
    for aic, acs in acs_map.items():
        acs = acs if isinstance(acs, dict) else {}
        alive = alive_map.get(aic) if isinstance(alive_map.get(aic), dict) else {}
        agents.append({
            "aic": str(aic),
            "name": str(acs.get("name") or ""),
            "active": bool(alive.get("alive", acs.get("active"))),
            "skills": [
                str(skill.get("id"))
                for skill in (acs.get("skills") or [])
                if isinstance(skill, dict) and skill.get("id")
            ],
            "endpoint": _jsonrpc_endpoint(acs),
        })
    return agents


__all__ = [
    "DEFAULT_DISCOVERY_URL",
    "WorkspaceIdentity",
    "discover",
    "load_identity",
    "peer_endpoint_from_local_workspaces",
]