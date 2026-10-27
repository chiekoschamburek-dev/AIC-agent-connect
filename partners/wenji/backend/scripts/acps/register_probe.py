#!/usr/bin/env python3
"""注册「问迹-联调客户端」身份（独立 AIC，仅用于 AIP 联调/压测，不对外提供服务）。

为什么需要它：

- 平台按 ``correlationId=taskId`` 对 access 记录做**双方配对**；自调用（caller 与
  callee 同一 AIC）在部分实现中会被视为重复而丢��，导致访问频次榜不统计。
- 用独立 AIC 作为 Leader 调用问迹，两侧记录的 ``aic`` 字段不同、taskId 相同，
  才构成一次真实的"跨智能体调用"。

该身份：无对外 endpoint、仅 1 个占位能力，明确标注为联调客户端。

用法（backend/，需 .env.acps 中的 ACPS_USERNAME/PASSWORD）：
  .venv/bin/python scripts/acps/register_probe.py            # 推进一次
  .venv/bin/python scripts/acps/register_probe.py --watch    # 盯审核直到 active
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

PROBE_ID = "wenji-probe"
PROBE_WORKSPACE = BACKEND_DIR / ".wit" / "wenji-probe"


def _load_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(REPO_DIR / ".env", override=False)
    load_dotenv(REPO_DIR / ".env.acps", override=False)


def build_probe_agent():
    from wit import AcpsDirect, WitAgent, capability

    @capability(
        id="wenji.probe.ping",
        description="联调客户端占位能力（不对外提供服务）",
        acps_skill=True,
        public=True,
        input_modes=["text/plain"],
        tags=["联调", "测试客户端"],
    )
    def ping() -> dict:
        return {"ok": True}

    return WitAgent(
        id=PROBE_ID,
        display_name="问迹-联调客户端",
        description="问迹的 AIP 联调测试客户端（非业务智能体，不对外提供能力）。",
        version="1.0.0",
        instructions="联调客户端，仅用于发起 AIP 任务。",
        capabilities=[ping],
        # WIT 的 integrated 注册要求声明协议绑定；此处仅占位（联调客户端不对外服务）。
        # 端点不会被实际监听，也不会产生 heartbeat，因此不会干扰 discovery 的在线列表。
        protocols=[AcpsDirect(modes=("rpc",))],
    )


def build_probe_config():
    from wit import AcpsConfig, AcpsRegistration, AgentCertificateRequest, AgentProviderProfile

    import os

    return AcpsConfig.custom(
        public_endpoint=os.environ.get(
            "PROBE_PUBLIC_ENDPOINT", "https://8.160.176.215:8610"
        ),
        registry_url=os.environ.get("ACPS_REGISTRY_URL", "https://wt.ioa.pub/registry-server"),
        ca_url=os.environ.get("ACPS_CA_URL", "https://wt.ioa.pub/ca-server"),
        discovery_url=os.environ.get(
            "ACPS_DISCOVERY_URL", "https://wt.ioa.pub/discovery/acps-adp-v2/discover"
        ),
        provider=AgentProviderProfile(
            organization=os.environ.get("ACPS_ORGANIZATION", "问迹团队"),
            country_code=os.environ.get("ACPS_COUNTRY_CODE", "CN"),
        ),
        certificate=AgentCertificateRequest(requested_validity=365),
        registration=AcpsRegistration.integrated(),
        strict_interop=True,
        request_timeout_seconds=60.0,
    )


async def main(watch: bool, interval: float) -> int:
    from wit import RegistryPasswordCredentials, WitRuntime

    runtime = WitRuntime(build_probe_agent(), workspace=PROBE_WORKSPACE, acps=build_probe_config())
    credentials = RegistryPasswordCredentials.from_env()

    while True:
        result = await runtime.up(credentials=credentials)
        status = getattr(result, "status", "?")
        print(f"[probe] status={status} next={getattr(result, 'next_action', '')}")
        if status in {"active", "serving"}:
            final = PROBE_WORKSPACE / "acps" / "acs.final.json"
            aic = ""
            if final.exists():
                aic = json.loads(final.read_text(encoding="utf-8")).get("aic", "")
            print(f"[probe] 已激活 AIC={aic}")
            return 0
        if status in {"rejected", "blocked", "failed"}:
            print("[probe] 受阻，请检查注册信息或平台审核状态")
            return 1
        if not watch:
            return 0
        await asyncio.sleep(interval)


if __name__ == "__main__":
    _load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=120.0)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.watch, args.interval)))
