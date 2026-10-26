"""研枢-Model-Partner：算法模块 agent（资源 agent 化第三实例——权重不出域，只暴露推理）。

底座与算力/评测不同：**模型在本进程内加载**，推理是一次本地函数调用——无沙盒、
无 Docker 依赖。演示底座为确定性纯 Python 分类器（启动时用合成数据"训练"），
真实场景换成 load_model() 加载任意框架权重，接口不变（demo substrate, weights drop-in）。

能力（三档中的两档）：
- model.info    public（橱窗）：架构/基准/许可证/输入输出规格——让人发现并想用
- model.predict 非公开（保险柜）：批量预测，输入校验（行数/数值域），按次配额（max_uses）

身份：ws-model（MODEL1.AGENT01.RUN01.05G4，端口 8467，mint_identity.py 铸造）。
运行：python acps_cluster/agents/partner_model.py --local
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
from acps_cluster.core.arg_shapes import as_list  # noqa: E402  工具入参数组宽容化
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-model"
MODEL_PORT_ENV = "ACPS_MODEL_PORT"
MODEL_PORT_DEFAULT = 8467

MAX_BATCH = 256
FEATURE_RANGE = (-10.0, 10.0)


class DemoClassifier:
    """演示底座：规则可学的线性分类器（真实场景替换为任意框架的 load_model()）。

    与评测基准 demo-cls 同分布（x0+x1>0 判 1）——答辩叙事：模型 agent 与评测 agent
    组合演示"模型推理 → 交评测打分"的闭环。
    """

    name = "demo-linear-2d"
    version = "1.0.0"
    license = "研究用途，权重不出域"

    def __init__(self, *, bias: float = 0.0) -> None:
        self.bias = bias

    def predict_one(self, x0: float, x1: float) -> dict:
        decision = x0 + x1 + self.bias
        return {"label": 1 if decision > 0 else 0, "score": round(abs(decision), 4)}


_MODEL = DemoClassifier()


def validate_batch(rows) -> str | None:
    rows = as_list(rows)
    if not isinstance(rows, list) or not rows:
        return "rows 必须是非空数组：[{x0, x1}, ...]"
    if len(rows) > MAX_BATCH:
        return f"单次批量上限 {MAX_BATCH} 行（配额语义：次数×批量）"
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or "x0" not in row or "x1" not in row:
            return f"rows[{i}] 必须含 x0/x1 字段"
        try:
            lo, hi = FEATURE_RANGE
            for key in ("x0", "x1"):
                value = float(row[key])
                if not lo <= value <= hi:
                    return f"rows[{i}].{key}={value} 超出输入域 [{lo}, {hi}]"
        except (TypeError, ValueError):
            return f"rows[{i}] 的 x0/x1 必须是数值"
    return None


@capability(
    id="model.info",
    description="模型卡：架构/版本/许可证/输入输出规格/基准（公开橱窗；权重不出域，只暴露推理）。",
    public=True,
    tags=["model", "inference"],
)
async def model_info() -> dict:
    return {
        "name": _MODEL.name, "version": _MODEL.version, "license": _MODEL.license,
        "task": "2 维二分类",
        "input": {"x0": f"float {FEATURE_RANGE}", "x1": f"float {FEATURE_RANGE}",
                  "batchLimit": MAX_BATCH},
        "output": {"label": "0|1", "score": "|x0+x1|（置信度代理）"},
        "weights": "不出域（进程内加载，永不下发）",
    }


@capability(
    id="model.predict",
    description="批量推理：rows=[{x0,x1},...]（≤256 行/次）→ 每行 {label,score}。权重不出域。",
    acps_skill={"visibility": "domain", "domains": ["yanshu-model"]},
    tags=["model", "inference"],
)
async def model_predict(rows: list) -> dict:
    rows = as_list(rows)
    error = validate_batch(rows)
    if error:
        return {"ok": False, "error": error}
    predictions = [_MODEL.predict_one(float(r["x0"]), float(r["x1"])) for r in rows]
    return {"ok": True, "model": _MODEL.name, "count": len(predictions), "predictions": predictions}


MODEL_CAPABILITIES = [model_info, model_predict]


def build_model_agent(provider) -> WitAgent:
    public_skills, has_non_public = capability_visibility(MODEL_CAPABILITIES)
    pep_hooks = build_pep_hooks(
        capabilities=MODEL_CAPABILITIES,
        workspace=WORKSPACE, public_skills=public_skills, has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    return WitAgent(
        id="yanshu.model-partner",
        display_name="研枢-Model-Partner",
        version="1.0.0",
        description=agent_discovery_description('yanshu.model-partner', "算法模块：进程内模型推理服务（权重不出域，只暴露 predict）。"),
        instructions=(
            "你是模型服务接口员。规格查询用 model.info；推理请求调用 model.predict"
            "（rows 数组，每行 {x0,x1}），把返回的 predictions 原样转达；输入不合法时"
            "如实转达错误，不得编造推理结果。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(MODEL_CAPABILITIES, agent_id="yanshu.model-partner"),
        hooks=[AipDelegationHooks(), BridgeHooks(), *pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def _serve_local() -> None:
    from wit import WitRuntime

    port = int(os.getenv(MODEL_PORT_ENV, str(MODEL_PORT_DEFAULT)))
    runtime = WitRuntime(
        build_model_agent(None),
        workspace=WORKSPACE,
        acps=cluster_common.build_local_acps_config(port),
    )
    cluster_common.ensure_local_acs(runtime, WORKSPACE)
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[model] 本地直连模式 endpoint=https://localhost:{port}/rpc → {status.status}")
    await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if args.local:
        await _serve_local()
        return
    await cluster_common.serve_partner(
        build_model_agent,
        workspace=WORKSPACE,
        port_env=MODEL_PORT_ENV,
        default_port=MODEL_PORT_DEFAULT,
        creds_prefix="MODEL",
    )


if __name__ == "__main__":
    asyncio.run(main())
