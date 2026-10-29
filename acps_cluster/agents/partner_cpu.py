"""研枢-CPU-Partner：本地 CPU 算力节点（资源 agent 化第二个实例，与 GPU 节点异构成对）。

能力面与 partner_gpu 对称：
- cpu.env    public=True      查核数/内存/Python 版本/沙盒限制（公开只读）
- cpu.status public=True      查最近任务状态
- cpu.run    acps_skill=True   提交 CPU 任务（非公开：PEP 准入后才能调）

执行层与 GPU 节点共用 gpu_runner 的八条硬边界沙盒，唯一区别是**不透传 GPU**
（run_sandbox(use_gpu=False)，由本伙伴代码侧决定，模型不可选）。任务工作区独立
（ws-cpu/runs），域章程与配额账本也独立（ws-cpu/acps/domains、grant-quota.sqlite3）。

运行：
  本地演示：python acps_cluster/agents/partner_cpu.py --local
  生产：    python acps_cluster/agents/partner_cpu.py
身份由 deploy/mint_identity.py 一次性铸造（开发 Agent Intermediate CA 签发）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wit import AcpsDirect, WitAgent, WitRuntime, capability  # noqa: E402

from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from acps_cluster.runtime import gpu_runner  # noqa: E402  执行层复用：同一套八条硬边界沙盒
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-cpu"
CPU_PORT_ENV = "ACPS_CPU_PORT"
CPU_PORT_DEFAULT = 8462
RUNS_ROOT = WORKSPACE / "runs"

try:
    import psutil as _psutil

    _TOTAL_MEM_GB: object = round(_psutil.virtual_memory().total / 1e9, 1)
except ImportError:
    _TOTAL_MEM_GB = "?"


def _probe_cpu() -> dict:
    return {
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpuCount": os.cpu_count() or "?",
        "totalMemGB": _TOTAL_MEM_GB,
    }


@capability(
    id="cpu.env",
    description="查询 CPU 计算节点环境：核数/内存/Python 版本、镜像白名单、沙盒资源限制。公开只读。",
    public=True,
    tags=["cpu", "compute", "status"],
)
async def cpu_env() -> dict:
    return {
        "cpu": _probe_cpu(),
        "docker": gpu_runner.docker_available(),
        "images": list(gpu_runner.ALLOWED_IMAGES),
        "sandbox": {
            "network": "none（断网执行）",
            "memory": "7g", "cpus": "4", "shm": "2g", "gpus": "无（纯 CPU 节点）",
            "filesystem": "只读根 + /workspace/output 可写",
            "timeLimit": f"{gpu_runner.DEFAULT_TIMEOUT_SECONDS}s（上限 {gpu_runner.MAX_TIMEOUT_SECONDS}s）",
            "inputLimit": f"单文件≤{gpu_runner.MAX_FILE_BYTES // 1024 // 1024}MB，"
                          f"总输入≤{gpu_runner.MAX_TOTAL_INPUT_BYTES // 1024 // 1024}MB",
        },
    }


@capability(
    id="cpu.status",
    description="查询最近的 CPU 节点任务（状态/时长/退出码/产物清单，不含任务内容）。公开只读。",
    public=True,
    tags=["cpu", "compute", "status"],
)
async def cpu_status() -> dict:
    """返回 CPU 资源的运行时状态与环境信息（公开能力）。"""
    if not RUNS_ROOT.is_dir():
        return {"runs": []}
    runs = []
    for run_dir in gpu_runner.recent_run_dirs(RUNS_ROOT, 10):
        result_file = run_dir / "result.json"
        if result_file.is_file():
            try:
                d = json.loads(result_file.read_text(encoding="utf-8"))
                runs.append({
                    "runId": d.get("run_id") or run_dir.name.removeprefix("run-"),
                    "ok": d.get("ok"),
                    "exitCode": d.get("exit_code"),
                    "timedOut": d.get("timed_out"),
                    "durationSeconds": d.get("duration_s"),
                    "artifacts": [a.get("name") for a in d.get("artifacts") or []],
                })
                continue
            except (OSError, ValueError):
                pass
        runs.append({"runId": run_dir.name.removeprefix("run-"),
                     "status": "running-or-interrupted"})
    return {"runs": runs}


@capability(
    id="cpu.run",
    description=(
        "在 CPU 沙盒容器（pytorch:cu128 镜像但不透传 GPU，断网，内存 7G/CPU 4 核）中运行任务："
        "files 提供 {文件名: 内容}（至少含 main.py），产物写入 output 目录会随结果回传。"
        "适合纯计算/数据处理/脚本类任务；高成本资源：仅限被授权的伙伴调用。"
    ),
    acps_skill=True,
    tags=["cpu", "compute", "task"],
)
async def cpu_run(files: dict, entrypoint: list | None = None, timeout_seconds: int = 600) -> dict:
    raw = {"files": files, "entrypoint": entrypoint, "timeout_seconds": timeout_seconds}
    result = await gpu_runner.run_sandbox(raw, runs_root=RUNS_ROOT, use_gpu=False)
    return {
        "ok": result.ok,
        "runId": result.run_id,
        "exitCode": result.exit_code,
        "timedOut": result.timed_out,
        "durationSeconds": result.duration_s,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "artifacts": result.artifacts,
        "error": result.error,
    }


CPU_CAPABILITIES = [cpu_env, cpu_status, cpu_run]


def build_cpu_agent(provider) -> WitAgent:
    """装配「研枢-CPU-Partner」agent：模型、工具与 hooks 的公开构建入口。"""
    public_skills, has_non_public = capability_visibility(CPU_CAPABILITIES)
    pep_hooks = build_pep_hooks(
        capabilities=CPU_CAPABILITIES,
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[cpu] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    return WitAgent(
        id="yanshu.cpu-partner",
        display_name="研枢-CPU-Partner",
        version="1.0.0",
        description=agent_discovery_description('yanshu.cpu-partner', "本地 CPU 计算节点：以断网加固沙盒形式向集群提供纯 CPU 任务执行能力。"),
        instructions=(
            "你是研枢平台的 CPU 计算节点。收到任务后：把代码整理成 files"
            "（入口文件名用 main.py，读输入用相对路径 input/，产物写到 output/），"
            "调用一次 cpu.run 提交（timeout 默认 600 秒），然后把返回的 stdout/stderr 与"
            "产物原样转达；cpu.run 失败时如实报告错误，不得编造运行结果。"
            "查询类问题（节点配置/任务状态）用 cpu.env / cpu.status。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(CPU_CAPABILITIES, agent_id="yanshu.cpu-partner"),
        hooks=[*pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def _serve_local() -> None:
    """本地直连模式（不经 registry/CA）。必须有模型才能真正接活（同 partner_gpu）。"""
    from acps_cluster.core import bootstrap  # 触发 .env 加载

    port = int(os.getenv(CPU_PORT_ENV, str(CPU_PORT_DEFAULT)))
    provider = None if os.getenv("ACPS_CPU_LOCAL_NO_MODEL") else bootstrap.build_provider()
    agent = build_cpu_agent(provider)
    runtime = WitRuntime(agent, workspace=WORKSPACE, acps=cluster_common.build_acps_config(port))
    cluster_common.ensure_local_acs(runtime, WORKSPACE)
    try:
        status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
        aic = cluster_common.ensure_local_acs(runtime, WORKSPACE) or "?"
        print(f"[cpu] 本地直连模式 AIC={aic} endpoint=https://localhost:{port}/rpc → {status.status}"
              f"（模型={'无' if provider is None else '已加载'}）", flush=True)
        await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))
    finally:
        if provider is not None:
            await provider.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="本地直连演示模式（不起 registry/CA）")
    args = parser.parse_args()
    if args.local:
        await _serve_local()
        return
    await cluster_common.serve_partner(
        build_cpu_agent,
        workspace=WORKSPACE,
        port_env=CPU_PORT_ENV,
        default_port=CPU_PORT_DEFAULT,
        creds_prefix="CPU",
    )


if __name__ == "__main__":
    asyncio.run(main())
