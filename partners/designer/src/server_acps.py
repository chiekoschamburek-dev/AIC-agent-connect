import argparse
import asyncio
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from wit import (
    AcpsConfig,
    AgentCertificateRequest,
    AgentProviderProfile,
    OpenAIChatProvider,
    RegistryPasswordCredentials,
    WitRuntime,
)

from research_designer import build_research_designer


WORKSPACE = Path(os.getenv("WIT_WORKSPACE", ".wit/scinexus-research-designer"))


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"缺少环境变量：{name}")
    return value


def _certificate_request(public_endpoint: str) -> AgentCertificateRequest:
    hostname = urlparse(public_endpoint).hostname
    if not hostname:
        raise SystemExit("ACPS_PUBLIC_ENDPOINT 必须是完整的 HTTPS 地址。")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return AgentCertificateRequest(dns=(hostname,))
    return AgentCertificateRequest(ip=(hostname,))


def build_server_acps_config() -> AcpsConfig:
    public_endpoint = _required("ACPS_PUBLIC_ENDPOINT")
    if not public_endpoint.startswith("https://"):
        raise SystemExit("正式 ACPs endpoint 必须使用 https://。")
    return AcpsConfig.custom(
        registry_url=_required("ACPS_REGISTRY_URL"),
        registry_mtls_url=os.getenv("ACPS_REGISTRY_MTLS_URL", "").strip() or None,
        ca_url=_required("ACPS_CA_URL"),
        discovery_url=_required("ACPS_DISCOVERY_URL"),
        public_endpoint=public_endpoint.rstrip("/"),
        provider=AgentProviderProfile(
            organization=_required("ACPS_ORGANIZATION"),
            country_code=os.getenv("ACPS_COUNTRY_CODE", "CN"),
        ),
        certificate=_certificate_request(public_endpoint),
        strict_interop=True,
    )


def _model_provider() -> OpenAIChatProvider:
    proxy_url = os.getenv("OPENAI_PROXY_URL", "").strip()
    transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
    return OpenAIChatProvider(
        model=_required("OPENAI_MODEL"),
        api_key=_required("OPENAI_API_KEY"),
        base_url=_required("OPENAI_BASE_URL"),
        transport=transport,
        timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "180")),
        max_retries=int(os.getenv("OPENAI_MAX_RETRIES", "0")),
        temperature=0,
    )


def _runtime(*, with_model: bool) -> tuple[WitRuntime, OpenAIChatProvider | None]:
    provider = _model_provider() if with_model else None
    runtime = WitRuntime(
        build_research_designer(provider, enable_acps=True),
        workspace=WORKSPACE,
        acps=build_server_acps_config(),
    )
    return runtime, provider


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Research Designer ACPs 服务器入口")
    parser.add_argument("action", choices=("draft", "enroll", "status", "serve"))
    parser.add_argument("--host", default=os.getenv("ACPS_BIND_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ACPS_BIND_PORT", "8590")))
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    runtime, provider = _runtime(with_model=args.action == "serve")
    try:
        if args.action == "draft":
            path = runtime.save_acs()
            print(json.dumps(runtime.acs_draft(), ensure_ascii=False, indent=2))
            print(f"ACS 草稿已保存：{path.resolve()}")
            return

        if args.action == "status":
            print(json.dumps(runtime.acps_status().to_dict(), ensure_ascii=False, indent=2))
            return

        if args.action == "enroll":
            credentials = RegistryPasswordCredentials.from_env()
            result = await runtime.up_until_ready(credentials=credentials)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return

        status = await runtime.serve(
            direct=True,
            group=False,
            host=args.host,
            port=args.port,
            amp=True,
            amp_periodic_logs=("heartbeat", "system"),
        )
        print(json.dumps(status.to_dict(), ensure_ascii=False, indent=2))
        if status.status != "serving":
            raise SystemExit("ACPs 服务未进入 serving 状态。")

        print(f"Research Designer 正在监听 {args.host}:{args.port}")
        print(f"AMP 日志由 WIT Runtime 写入：{(WORKSPACE / 'logs').resolve()}")
        await asyncio.Event().wait()
    finally:
        if provider is not None:
            await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
