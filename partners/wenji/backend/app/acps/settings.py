"""ACPs / AIP 接入配置 — 全部来自环境变量，仓库内不保存任何凭据。

约定：

- 平台验证（梧桐智能体互联平台）使用 ``ACPS_PROFILE=wt``（默认）；
- 自建 ACPs 基础设施使用 ``ACPS_PROFILE=custom`` + ``ACPS_REGISTRY_URL`` 等；
- ``ACPS_PROFILE=ioa_pub`` 使用 WIT 内置的 ioa.pub 默认基础设施（www.ioa.pub）；
- 未完成注册前 ``ACPS_ENABLED=0``：既有后端 API 照常运行，AIP 能力不对外服务。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 梧桐平台基础设施（参赛必选验证场景，见 docs/aic/README.md）
WT_REGISTRY_URL = "https://wt.ioa.pub/registry-server"
WT_CA_URL = "https://wt.ioa.pub/ca-server"
WT_DISCOVERY_URL = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value.strip() else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AcpsSettings:
    """一次 AIP 接入所需的全部运行参数（冻结，跨线程安全）。"""

    enabled: bool = False
    profile: str = "wt"                       # wt | ioa_pub | custom
    agent_id: str = "wenji-aip-agent"
    display_name: str = "问迹·可解释问数问答智能体"
    description: str = (
        "面向结构化数据库与非结构化文档的可解释问数/问答智能体："
        "NL2SQL 精准问数、RAG 带引用问答、跨源多跳与公式计算，"
        "全链路 Trace 可追溯，通过 AIP 国标对外提供能力。"
    )
    version: str = "1.0.0"
    organization: str = "问迹团队"             # ACS provider.organization
    country_code: str = "CN"
    workspace: Path = field(default_factory=lambda: Path(".wit/wenji-aip-agent"))
    listen_host: str = "0.0.0.0"
    listen_port: int = 8443
    public_endpoint: str = ""                 # 对外可访问基地址（AIP Direct）
    cert_validity_days: int = 365
    strict_interop: bool = True

    # Leader 侧：在平台上发现并委派给其它智能体（队友的 agent）
    delegate_enabled: bool = True
    delegate_delivery: str = "rpc"

    # custom profile 基础设施地址
    registry_url: str = WT_REGISTRY_URL
    registry_mtls_url: str = ""
    ca_url: str = WT_CA_URL
    discovery_url: str = WT_DISCOVERY_URL
    request_timeout_seconds: float = 60.0

    # AMP 访问日志归属（平台按 correlationId=taskId 统计调用次数）
    amp_periodic_logs: tuple[str, ...] = ("heartbeat", "metrics", "system")

    @classmethod
    def from_env(cls) -> "AcpsSettings":
        profile = _env_str("ACPS_PROFILE", "wt").lower()
        if profile not in {"wt", "ioa_pub", "custom"}:
            profile = "wt"
        discovery_default = WT_DISCOVERY_URL if profile == "wt" else ""
        return cls(
            enabled=_env_bool("ACPS_ENABLED", False),
            profile=profile,
            agent_id=_env_str("ACPS_AGENT_ID", "wenji-aip-agent"),
            display_name=_env_str("ACPS_DISPLAY_NAME", "问迹·可解释问数问答智能体"),
            version=_env_str("ACPS_AGENT_VERSION", "1.0.0"),
            organization=_env_str("ACPS_ORGANIZATION", "问迹团队"),
            country_code=_env_str("ACPS_COUNTRY_CODE", "CN"),
            workspace=Path(_env_str("ACPS_WORKSPACE", ".wit/wenji-aip-agent")),
            listen_host=_env_str("ACPS_LISTEN_HOST", "0.0.0.0"),
            listen_port=_env_int("ACPS_LISTEN_PORT", 8443),
            public_endpoint=_env_str("ACPS_PUBLIC_ENDPOINT", ""),
            cert_validity_days=_env_int("ACPS_CERT_VALIDITY_DAYS", 365),
            strict_interop=_env_bool("ACPS_STRICT_INTEROP", True),
            delegate_enabled=_env_bool("ACPS_DELEGATE_ENABLED", True),
            delegate_delivery=_env_str("ACPS_DELEGATE_DELIVERY", "rpc"),
            registry_url=_env_str("ACPS_REGISTRY_URL", WT_REGISTRY_URL),
            registry_mtls_url=_env_str("ACPS_REGISTRY_MTLS_URL", ""),
            ca_url=_env_str("ACPS_CA_URL", WT_CA_URL),
            discovery_url=_env_str("ACPS_DISCOVERY_URL", discovery_default),
            request_timeout_seconds=float(_env_int("ACPS_REQUEST_TIMEOUT_SECONDS", 60)),
        )

    # ------------------------------------------------------------------ helpers

    def public_endpoint_or_default(self) -> str:
        """未显式配置时，用监听地址给出一个仅本地可用提示值（注册前必须替换）。"""
        if self.public_endpoint:
            return self.public_endpoint
        host = self.listen_host if self.listen_host not in {"", "0.0.0.0"} else "127.0.0.1"
        return f"https://{host}:{self.listen_port}/acps/aip/v1"
