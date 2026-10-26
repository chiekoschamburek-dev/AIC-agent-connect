"""研枢-HPC-Partner：机构调度器网关（资源 agent 化第五实例——外部调度器底座）。

把机构现有 HPC（Slurm/K8s）不改造地接入互联网络：hpc_runner 的 real 后端对接 sbatch，
本地演示用 mock 后端（文件队列 + 沙盒执行——调度语义真实，"mock"指没有真 Slurm，
不是假执行）。异步作业语义：submit → job_id → 轮询 jobs → 终态取 result。

能力：
- hpc.info / hpc.jobs  public（橱窗）：后端/限额/队列状态——引流与可观测
- hpc.submit / hpc.result 非公开（保险柜）：提交（墙钟/文件数/大小/队列深度四重限额）
  与取结果（stdout 截选 + 产物元信息；产物全文留在网关本机，不搬运）

身份：ws-hpc（HPC001.AGENT01.RUN01.16Q7，端口 8466，mint_identity.py 铸造）。
运行：python acps_cluster/agents/partner_hpc.py --local
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wit import AcpsDirect, WitAgent, capability  # noqa: E402

from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from acps_cluster.runtime import hpc_runner  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-hpc"
HPC_PORT_ENV = "ACPS_HPC_PORT"
HPC_PORT_DEFAULT = 8466


@capability(
    id="hpc.info",
    description="网关信息卡：后端（real=机构Slurm / mock=本地队列+沙盒）、作业限额（墙钟/文件/大小/队列深度）、当前队列。",
    public=True,
    tags=["hpc", "scheduler"],
)
async def hpc_info() -> dict:
    return hpc_runner.info()


@capability(
    id="hpc.jobs",
    description="作业列表（squeue 等价）：id/名称/状态/墙钟/耗时/退出码——只有元数据，无脚本无产物。",
    public=True,
    tags=["hpc", "jobs"],
)
async def hpc_jobs() -> dict:
    return {"jobs": hpc_runner.list_jobs()}


@capability(
    id="hpc.submit",
    description=(
        "提交异步作业：name（1..40 位）、files（入口 main.py，≤5 文件 ≤2MB）、"
        "walltimeMinutes（1..30）。返回 jobId；轮询 hpc.jobs 等终态，再 hpc.result 取输出。"
        "队列深度上限 8（背压）。高成本能力：受配额约束。"
    ),
    acps_skill={"visibility": "domain", "domains": ["yanshu-hpc"]},
    tags=["hpc", "submit"],
)
async def hpc_submit(name: str, files: dict, walltimeMinutes: int = 10, submitter: str = "?") -> dict:
    who, verified = cluster_common.resolve_submitter(submitter)  # 归属以 mTLS 对端为准
    hpc_runner.ensure_worker()
    out = hpc_runner.submit(
        {"name": name, "files": files, "walltimeMinutes": walltimeMinutes}, submitter=who
    )
    return {**out, "submitter": who, "submitterVerified": verified}


@capability(
    id="hpc.result",
    description="取终态作业结果：stdout 截选（2KB）+ 产物元信息；产物全文留在网关本机 runs 目录（大产物不搬运）。",
    acps_skill={"visibility": "domain", "domains": ["yanshu-hpc"]},
    tags=["hpc", "result"],
)
async def hpc_result(job_id: str, submitter: str = "?") -> dict:
    who, _verified = cluster_common.resolve_submitter(submitter)
    return hpc_runner.result(job_id, submitter=who)


HPC_CAPABILITIES = [hpc_info, hpc_jobs, hpc_submit, hpc_result]


def build_hpc_agent(provider) -> WitAgent:
    public_skills, has_non_public = capability_visibility(HPC_CAPABILITIES)
    pep_hooks = build_pep_hooks(
        capabilities=HPC_CAPABILITIES,
        workspace=WORKSPACE, public_skills=public_skills, has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    return WitAgent(
        id="yanshu.hpc-partner",
        display_name="研枢-HPC-Partner",
        version="1.0.0",
        description=agent_discovery_description('yanshu.hpc-partner', "机构调度器网关：异步作业（提交→排队→沙盒执行→取结果），存量 HPC 不改造接入互联。"),
        instructions=(
            "你是 HPC 网关接口员。信息与队列查询用 hpc.info / hpc.jobs；计算任务调用"
            " hpc.submit（name/files/main.py/walltimeMinutes），拿到 jobId 后轮询 hpc.jobs"
            " 等终态，再调 hpc.result 把 stdout 与产物清单转达；作业失败如实报告，不得编造结果。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(HPC_CAPABILITIES, agent_id="yanshu.hpc-partner"),
        hooks=[AipDelegationHooks(), BridgeHooks(), *pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), cluster_common.PeerIdentityHooks(), *pep_aip_hooks],
        protocols=[AcpsDirect()],
    )


async def _serve_local() -> None:
    from wit import WitRuntime

    port = int(os.getenv(HPC_PORT_ENV, str(HPC_PORT_DEFAULT)))
    runtime = WitRuntime(build_hpc_agent(None), workspace=WORKSPACE, acps=cluster_common.build_local_acps_config(port))
    cluster_common.ensure_local_acs(runtime, WORKSPACE)
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[hpc] 本地直连模式 endpoint=https://localhost:{port}/rpc → {status.status}（backend={hpc_runner.backend()}）")
    await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if args.local:
        await _serve_local()
        return
    await cluster_common.serve_partner(
        build_hpc_agent,
        workspace=WORKSPACE,
        port_env=HPC_PORT_ENV,
        default_port=HPC_PORT_DEFAULT,
        creds_prefix="HPC",
    )


if __name__ == "__main__":
    asyncio.run(main())
