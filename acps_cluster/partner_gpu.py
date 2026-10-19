"""研枢-GPU-Partner：把本地 RTX 5060 封装成集群算力 agent（资源 agent 化第一个实例）。

三个能力（capability id 与名册 cluster_roster 对齐）：
- gpu.env    public=True          查卡况/镜像白名单/沙盒限制（便宜，公开喂 ADP 语义匹配）
- gpu.status public=True          查最近实验任务状态
- gpu.run    acps_skill=True      提交实验（**非公开**：可发现、但 PEP 视为非公开，
                                  未写 public ⇒ L1 白名单/L2 委托 token 才能调——这正是
                                  "可发现但受门槛"声明组合的落地，见 PEP-result.md）

运行：
  本地演示（不起 registry/CA，直连 mTLS）：
    python acps_cluster/partner_gpu.py --local
  生产（走 up_until_ready，需 ACPS_GPU_USERNAME/PASSWORD）：
    python acps_cluster/partner_gpu.py

身份由 deploy/mint_gpu_identity.py 一次性铸造（开发 Agent Intermediate CA 签发，
AIC=1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR）。首次启动会在 ws-gpu 生成
acs.draft/final.json（本地终稿），名册与发现缓存据此解析身份。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wit import AcpsDirect, WitAgent, WitRuntime, capability  # noqa: E402

import cluster_common  # noqa: E402
from discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
import gpu_runner  # noqa: E402
from aip_guard import AipPartnerGuardHooks
from delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402

WORKSPACE = Path(__file__).parent / "ws-gpu"
GPU_PORT_ENV = "ACPS_GPU_PORT"
GPU_PORT_DEFAULT = 8460


def _probe_gpu() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=10, text=True,
        ).stdout.strip().split(", ")
        name, total, used, driver = (out + ["?"] * 4)[:4]
        return {"name": name, "vramTotalMB": total, "vramUsedMB": used, "driver": driver}
    except (OSError, subprocess.TimeoutExpired):
        return {"name": "?", "error": "nvidia-smi 不可用"}


@capability(
    id="gpu.env",
    description="查询 GPU 计算节点环境：显卡型号/显存/驱动、可用镜像白名单、沙盒资源限制。公开只读。",
    public=True,
    tags=["gpu", "compute", "status"],
)
async def gpu_env() -> dict:
    return {
        "gpu": _probe_gpu(),
        "docker": gpu_runner.docker_available(),
        "images": list(gpu_runner.ALLOWED_IMAGES),
        "datasets": gpu_runner.list_datasets()["datasets"],
        "sandbox": {
            "network": "none（断网执行）",
            "memory": "7g", "cpus": "4", "shm": "2g", "gpus": "device=0",
            "filesystem": "只读根 + /workspace/output 可写 + /data 数据集只读",
            "timeLimit": f"{gpu_runner.DEFAULT_TIMEOUT_SECONDS}s（上限 {gpu_runner.MAX_TIMEOUT_SECONDS}s）",
            "inputLimit": f"单文件≤{gpu_runner.MAX_FILE_BYTES // 1024 // 1024}MB，"
                          f"总输入≤{gpu_runner.MAX_TOTAL_INPUT_BYTES // 1024 // 1024}MB",
        },
    }


@capability(
    id="gpu.status",
    description="查询最近的 GPU 实验任务（状态/时长/退出码/产物清单，不含任务内容）。",
    public=True,
    tags=["gpu", "compute", "status"],
)
async def gpu_status() -> dict:
    runs_root = gpu_runner.RUNS_DIR
    if not runs_root.is_dir():
        return {"runs": []}
    # 2026-10-06 审查 #9:只回元数据——此前整包 result.json 含 stdout/stderr/
    # artifacts 正文(跨租户泄露产物内容,与"不含任务内容"的描述不符)
    runs = []
    for run_dir in gpu_runner.recent_run_dirs(runs_root, 10):
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
        runs.append({"runId": run_dir.name.removeprefix("run-"), "status": "running-or-interrupted"})
    return {"runs": runs}


@capability(
    id="gpu.data.list",
    description="列出本 GPU 节点已托管的数据集（名称/文件数/大小/容器内挂载路径）。公开只读。",
    public=True,
    tags=["gpu", "compute", "dataset"],
)
async def gpu_data_list() -> dict:
    return gpu_runner.list_datasets()


@capability(
    id="gpu.data.fetch",
    description=(
        "把一个数据集从 URL 代下到本节点（宿主侧下载，沙盒外执行），之后实验代码可用"
        " /data/<name> 只读访问。仅 https（回环地址可 http）；可选 sha256 强校验；"
        "上限 2GB；zip/tar 自动安全解压。数据集托管属高成本操作，仅限被授权的伙伴调用。"
    ),
    acps_skill=True,
    tags=["gpu", "compute", "dataset"],
)
async def gpu_data_fetch(name: str, url: str, sha256: str | None = None) -> dict:
    return await gpu_runner.fetch_dataset(name, url, sha256)


@capability(
    id="gpu.run",
    description=(
        "在 GPU 沙盒容器（pytorch:cu128，断网，内存 7G/CPU 4 核/GPU device=0）中运行实验："
        "files 提供 {文件名: 内容}（至少含 main.py，输入数据可用相对 input 目录读取），"
        "entrypoint 缺省 ['python','/workspace/input/main.py']，产物写入 output 目录会随结果回传。"
        "高成本资源：仅限被授权的伙伴调用。"
    ),
    acps_skill=True,
    tags=["gpu", "compute", "experiment"],
)
async def gpu_run(files: dict, entrypoint: list | None = None, timeout_seconds: int = 600) -> dict:
    raw = {"files": files, "entrypoint": entrypoint, "timeout_seconds": timeout_seconds}
    result = await gpu_runner.run_sandbox(raw)
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


GPU_CAPABILITIES = [gpu_env, gpu_status, gpu_data_list, gpu_data_fetch, gpu_run]


def build_gpu_agent(provider) -> WitAgent:
    public_skills, has_non_public = capability_visibility(GPU_CAPABILITIES)
    pep_hooks = build_pep_hooks(
        capabilities=GPU_CAPABILITIES,
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[gpu] PEP 已启用: {pep_hooks[0].config.describe()}")
    return WitAgent(
        id="yanshu.gpu-partner",
        display_name="研枢-GPU-Partner",
        version="1.0.0",
        description=agent_discovery_description('yanshu.gpu-partner', "本地 GPU 计算节点：把 RTX 5060 以沙盒容器形式向集群提供 PyTorch 实验执行能力。"),
        instructions=(
            "你是研枢平台的 GPU 计算节点。收到实验任务后：把实验代码整理成 files"
            "（入口文件名用 main.py，读输入用相对路径 input/，产物写到 output/），"
            "调用一次 gpu.run 提交（timeout 默认 600 秒），然后把返回的 stdout/stderr 与"
            "产物原样转达；gpu.run 失败时如实报告错误，不得编造运行结果。"
            "需要节点上没有的数据集时，先用 gpu.data.list 查已有，缺则用 gpu.data.fetch"
            "代下（提供 name/url，可附 sha256），成功后实验代码用 /data/<name> 只读访问。"
            "查询类问题（卡况/任务状态）用 gpu.env / gpu.status。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(GPU_CAPABILITIES, agent_id="yanshu.gpu-partner"),
        hooks=[*pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        protocols=[AcpsDirect()],
    )


def _ensure_local_acs(runtime: WitRuntime) -> str | None:
    """保留旧名（demo_gpu.py 仍在调）；实现已收敛到 cluster_common.ensure_local_acs。"""
    return cluster_common.ensure_local_acs(runtime, WORKSPACE)


async def _serve_local() -> None:
    """本地直连模式（不经 registry/CA）。

    ⚠️ 要真正**接活**（收 AIP 任务 → 跑 agent → 调用 gpu.run 能力），必须有模型：
    只传 None 时准入与协议层能验证，但 agent 循环跑不起来、产不出 Product。
    `ACPS_GPU_LOCAL_NO_MODEL=1` 可退回"无模型"的纯准入演示（原行为）。
    """
    import agents  # 触发 .env 加载（build_acps_config 也需要其中的 ACPS_*_URL）

    port = int(os.getenv(GPU_PORT_ENV, str(GPU_PORT_DEFAULT)))
    provider = None if os.getenv("ACPS_GPU_LOCAL_NO_MODEL") else agents.build_provider()
    agent = build_gpu_agent(provider)
    runtime = WitRuntime(agent, workspace=WORKSPACE, acps=cluster_common.build_acps_config(port))
    _ensure_local_acs(runtime)
    try:
        status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
        aic = _ensure_local_acs(runtime) or "?"
        print(f"[gpu] 本地直连模式 AIC={aic} endpoint=https://localhost:{port}/rpc → {status.status}"
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
        build_gpu_agent,
        workspace=WORKSPACE,
        port_env=GPU_PORT_ENV,
        default_port=GPU_PORT_DEFAULT,
        creds_prefix="GPU",
    )


if __name__ == "__main__":
    asyncio.run(main())
