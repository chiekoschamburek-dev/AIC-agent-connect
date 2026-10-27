"""WIT Runtime 生命周期封装：注册状态机 + AIP Direct 服务 + AMP。

对外只暴露四个动作：

- ``acs_draft()/save_acs()``：生成 ACS 草稿（可上传注册平台/人工检查）；
- ``enroll()``：推进一次可信注册（登录 → 草稿 → 提交 → 等待审核 → 证书 → active）；
- ``serve()``：在 active 身份上启动 AIP Direct Partner 监听（含 mTLS 与 AMP）；
- ``stop()``：停止服务。

注意：``workspace`` 目录内含私钥与证书材料，已在 .gitignore 中排除，切勿提交。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .agent import build_agent
from .settings import AcpsSettings


def build_acps_config(settings: AcpsSettings):
    """构造 ``AcpsConfig``（wt=custom / ioa_pub / custom 三种形态）。"""
    from wit import AcpsConfig, AcpsRegistration, AgentCertificateRequest, AgentProviderProfile

    provider = AgentProviderProfile(
        organization=settings.organization,
        country_code=settings.country_code,
    )
    certificate = AgentCertificateRequest(requested_validity=settings.cert_validity_days)
    common: dict[str, Any] = {
        "provider": provider,
        "certificate": certificate,
        "public_endpoint": settings.public_endpoint_or_default(),
        "strict_interop": settings.strict_interop,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "registration": AcpsRegistration.integrated(),
    }

    if settings.profile == "ioa_pub":
        return AcpsConfig.ioa_pub(**common)

    # wt（梧桐平台）与 self-hosted custom 都是显式服务地址
    if not settings.discovery_url:
        raise RuntimeError(
            "ACPS_DISCOVERY_URL 未配置：custom/wt profile 需要完整的发现端点"
        )
    return AcpsConfig.custom(
        registry_url=settings.registry_url,
        registry_mtls_url=settings.registry_mtls_url or None,
        ca_url=settings.ca_url,
        discovery_url=settings.discovery_url,
        **common,
    )


class AcpsRuntime:
    """问迹的 ACPs 运行时门面。"""

    def __init__(self, settings: AcpsSettings | None = None):
        from wit import RegistryPasswordCredentials, WitRuntime

        self.settings = settings or AcpsSettings.from_env()
        self._credentials = None
        if self.settings.workspace is not None:
            self.settings.workspace.mkdir(parents=True, exist_ok=True)
        self.runtime = WitRuntime(
            build_agent(self.settings),
            workspace=Path(self.settings.workspace),
            acps=build_acps_config(self.settings),
        )
        # 任务级 access 钩子需要运行时句柄（serve 进程内最后构造的运行时生效）
        from .task_access import RUNTIME_HOLDER

        RUNTIME_HOLDER.set(self)
        self._credentials_cls = RegistryPasswordCredentials

    # ------------------------------------------------------------------ ACS

    def acs_draft(self) -> dict[str, Any]:
        return self.runtime.acs_draft()

    def save_acs(self, path: str | Path | None = None) -> Path:
        return self.runtime.save_acs(path)

    # ------------------------------------------------------------ enrollment

    async def validate(self) -> dict[str, Any]:
        """本地静态校验：声明、协议绑定、ACS 投影（不访问网络）。"""
        return await self.runtime.validate()

    async def enroll(self, *, serve: bool = False):
        """推进一次注册状态机；到 pending_review/active/blocked 即返回。"""
        credentials = None
        if self.settings.profile in {"wt", "custom"}:
            import os

            if os.environ.get("ACPS_USERNAME"):
                credentials = self._credentials_cls.from_env()
        return await self.runtime.up(
            serve=serve,
            host=self.settings.listen_host,
            port=self.settings.listen_port,
            credentials=credentials,
        )

    # ----------------------------------------------------------------- serve

    async def serve(self):
        """启动 AIP Direct Partner 监听 + AMP（要求身份已 active）。"""
        return await self.runtime.serve_direct_partner(
            host=self.settings.listen_host,
            port=self.settings.listen_port,
            amp=True,
            amp_periodic_logs=self.settings.amp_periodic_logs,
        )

    async def stop(self) -> None:
        await self.runtime.stop_serving()

    def describe(self) -> dict[str, Any]:
        """给 CLI/日志用的配置摘要（不含凭据）。"""
        return {
            "agent_id": self.settings.agent_id,
            "display_name": self.settings.display_name,
            "version": self.settings.version,
            "profile": self.settings.profile,
            "registry_url": self.settings.registry_url,
            "ca_url": self.settings.ca_url,
            "discovery_url": self.settings.discovery_url,
            "public_endpoint": self.settings.public_endpoint_or_default(),
            "listen": f"{self.settings.listen_host}:{self.settings.listen_port}",
            "workspace": str(self.settings.workspace),
            "delegate_enabled": self.settings.delegate_enabled,
            "strict_interop": self.settings.strict_interop,
        }
