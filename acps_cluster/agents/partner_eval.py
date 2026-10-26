"""研枢-Eval-Partner：评测服务 agent（资源 agent 化第二实例——数据集藏在背后，只发分数）。

三能力（可见性档位即商业语义）：
- eval.benchmark_info  public（橱窗）：基准清单/指标/规则——引流
- eval.leaderboard     public（橱窗）：榜单 Top-N
- eval.submit          非公开（保险柜）：两种提交模式
    mode="predictions"：交预测数组，host 侧打分
    mode="code"：交代码，沙盒内读隐藏特征产出预测（标签永不进容器），
                 stdout/产物不回传（防泄题三闸门，见 eval_runner）

身份：ws-eval（EVAL01.AGENT01.RUN01.0ZBR，端口 8464，mint_identity.py 铸造）。
运行：python acps_cluster/partner_eval.py --local
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
from acps_cluster.runtime import eval_runner  # noqa: E402
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-eval"
EVAL_PORT_ENV = "ACPS_EVAL_PORT"
EVAL_PORT_DEFAULT = 8464


@capability(
    id="eval.benchmark_info",
    description="列出可用评测基准：指标、样本数、提交规则（标签由评测方持有，不随特征下发）。",
    public=True,
    tags=["eval", "benchmark"],
)
async def eval_benchmark_info() -> dict:
    return {"benchmarks": eval_runner.list_benchmarks(), "metric": "accuracy",
            "modes": ["predictions（交预测数组）", "code（交代码，沙盒内读隐藏特征）"]}


@capability(
    id="eval.leaderboard",
    description="评测榜单 Top-N（提交者/分数/时间，不含任何预测内容）。",
    public=True,
    tags=["eval", "leaderboard"],
)
async def eval_leaderboard() -> dict:
    path = eval_runner.LEADERBOARD_PATH
    board = []
    if path.is_file():
        try:
            board = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            board = []
    return {"leaderboard": eval_runner._public_board(list(board)[:10])}


@capability(
    id="eval.submit",
    description=(
        "提交评测：mode='predictions' 交预测数组（按基准特征行序的 0/1 标签）；"
        "mode='code' 交实验代码（files 含 main.py，容器内读 /eval/input.csv，"
        "把 predictions.json 写到输出目录）。只返回分数与榜单——stdout 与产物不回传，"
        "标签永不进入容器（防泄题）。高成本能力：提交次数受配额约束。"
    ),
    acps_skill={"visibility": "domain", "domains": ["yanshu-eval"]},
    tags=["eval", "submit"],
)
async def eval_submit(mode: str = "predictions", benchmark: str = "demo-cls",
                      predictions: list | None = None, files: dict | None = None,
                      timeout_seconds: int = 120, submitter: str = "?") -> dict:
    # 归属以 mTLS 已验证对端为准；自报 submitter 只在没有对端时兜底（本机直调/离线演示），
    # 且响应里用 submitterVerified 标出这一点——否则榜单可被任意署名（2026-10-07 审查修复）
    who, verified = cluster_common.resolve_submitter(submitter)
    if mode == "predictions":
        out = eval_runner.submit_predictions(benchmark, predictions or [], submitter=who)
    elif mode == "code":
        out = await eval_runner.submit_code(
            {"benchmark": benchmark, "files": files or {}, "timeout_seconds": timeout_seconds},
            submitter=who,
        )
    else:
        return {"ok": False, "error": f"未知模式 {mode!r}（predictions | code）"}
    return {**out, "submitter": who, "submitterVerified": verified}


EVAL_CAPABILITIES = [eval_benchmark_info, eval_leaderboard, eval_submit]


def build_eval_agent(provider) -> WitAgent:
    public_skills, has_non_public = capability_visibility(EVAL_CAPABILITIES)
    pep_hooks = build_pep_hooks(
        capabilities=EVAL_CAPABILITIES,
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[eval] PEP 已启用: {pep_hooks[0].config.describe()}")
    return WitAgent(
        id="yanshu.eval-partner",
        display_name="研枢-Eval-Partner",
        version="1.0.0",
        description=agent_discovery_description('yanshu.eval-partner', "评测服务：数据集不出的基准打分——交预测或交代码，只拿分数。"),
        instructions=(
            "你是评测服务的接口员。查询类问题用 eval.benchmark_info / eval.leaderboard。"
            "提交类任务调用 eval.submit（predictions 模式传预测数组；code 模式传"
            " main.py 代码，规则：容器断网、只读 /eval/input.csv、predictions.json "
            "写到输出目录）。把返回的分数与榜单原样转达；提交失败如实报告，不得编造分数。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(EVAL_CAPABILITIES, agent_id="yanshu.eval-partner"),
        hooks=[AipDelegationHooks(), BridgeHooks(), *pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), cluster_common.PeerIdentityHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def _serve_local() -> None:
    from wit import WitRuntime

    eval_runner.ensure_demo_benchmark()
    port = int(os.getenv(EVAL_PORT_ENV, str(EVAL_PORT_DEFAULT)))
    runtime = WitRuntime(build_eval_agent(None), workspace=WORKSPACE, acps=cluster_common.build_local_acps_config(port))
    # 本地终稿 ACS（与 partner_gpu 同款）
    cluster_common.ensure_local_acs(runtime, WORKSPACE)
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[eval] 本地直连模式 endpoint=https://localhost:{port}/rpc → {status.status}")
    await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if args.local:
        await _serve_local()
        return
    await cluster_common.serve_partner(
        build_eval_agent,
        workspace=WORKSPACE,
        port_env=EVAL_PORT_ENV,
        default_port=EVAL_PORT_DEFAULT,
        creds_prefix="EVAL",
    )


if __name__ == "__main__":
    asyncio.run(main())
