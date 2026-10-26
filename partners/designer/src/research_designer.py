import argparse
import asyncio
import json
import math
import os
from pathlib import Path
from statistics import NormalDist
from typing import Any

from wit import (
    AcpsDirect,
    AmpAccessLoggingHooks,
    OpenAIChatProvider,
    WitAgent,
    WitRuntime,
    capability,
)


REQUIRED_PLAN_SECTIONS = {
    "research_question": "研究问题",
    "research_gap": "研究空白",
    "objectives": "研究目标",
    "hypotheses": "可检验假设",
    "variables": "变量设计",
    "data_requirements": "数据需求",
    "methodology": "研究方法",
    "evaluation_metrics": "评价指标",
    "baselines": "基线与对照",
    "ablation_studies": "消融实验",
    "risks_and_ethics": "风险与伦理",
    "reproducibility": "可复现计划",
    "success_and_failure_criteria": "成功与失败判据",
    "delegations": "后续协作任务",
}

REQUEST_FIELDS = {
    "task_id": "Leader 分配的任务标识",
    "research_question": "需要设计的研究问题",
    "literature_evidence": "检索智能体提供的可核验文献证据列表",
    "synthesis_summary": "综合智能体提供的研究现状与争议总结",
    "data_profile": "数据智能体提供的数据状态、规模和模态",
    "constraints": "Leader 提供的周期、算力和资源约束",
}


@capability(
    id="research.plan_template",
    description="返回指定研究类型所需的结构化研究设计模板和专项检查项。",
    public=True,
    acps_skill=True,
    tags=["科研", "研究设计", "实验规划"],
)
def get_plan_template(research_type: str = "empirical") -> str:
    normalized_type = research_type.strip().lower()
    specialized_checks = {
        "empirical": ["明确实验组和对照组", "说明抽样方法", "预注册主要指标"],
        "machine_learning": ["划分训练/验证/测试集", "防止数据泄漏", "报告计算预算"],
        "survey": ["说明量表来源", "评估信效度", "处理无响应偏差"],
        "qualitative": ["说明编码方案", "设置饱和度判据", "记录研究者反身性"],
        "systematic_review": ["定义检索式", "设置纳排标准", "记录筛选流程"],
    }
    checks = specialized_checks.get(
        normalized_type,
        ["明确研究对象", "说明数据来源", "定义评价与失败判据"],
    )
    return json.dumps(
        {
            "research_type": normalized_type,
            "required_sections": REQUIRED_PLAN_SECTIONS,
            "specialized_checks": checks,
        },
        ensure_ascii=False,
    )


@capability(
    id="research.validate_request",
    description=(
        "校验 Leader 或其他智能体提交的研究设计 JSON 请求，返回缺失信息、证据问题和"
        "可安全使用的规范化输入。"
    ),
    public=True,
    acps_skill=True,
    tags=["科研", "多智能体协作", "输入校验"],
)
def validate_request(request_json: str) -> str:
    try:
        request = json.loads(request_json)
    except json.JSONDecodeError as exc:
        return json.dumps(
            {"valid": False, "error": f"请求不是有效 JSON：{exc.msg}"},
            ensure_ascii=False,
        )

    if not isinstance(request, dict):
        return json.dumps(
            {"valid": False, "error": "请求必须是 JSON 对象。"},
            ensure_ascii=False,
        )

    missing_required = [
        field for field in ("task_id", "research_question") if not request.get(field)
    ]
    missing_context = [
        field
        for field in (
            "literature_evidence",
            "synthesis_summary",
            "data_profile",
            "constraints",
        )
        if not request.get(field)
    ]
    evidence_warnings: list[str] = []
    evidence = request.get("literature_evidence") or []
    if evidence and not isinstance(evidence, list):
        evidence_warnings.append("literature_evidence 必须是列表。")
    elif isinstance(evidence, list):
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                evidence_warnings.append(f"第 {index + 1} 条证据不是对象。")
                continue
            if not (item.get("doi") or item.get("source_url")):
                evidence_warnings.append(
                    f"第 {index + 1} 条证据缺少 doi 或 source_url，不能视为可核验证据。"
                )

    normalized = {field: request.get(field) for field in REQUEST_FIELDS}
    return json.dumps(
        {
            "valid": not missing_required and not evidence_warnings,
            "missing_required": missing_required,
            "missing_context": missing_context,
            "evidence_warnings": evidence_warnings,
            "normalized_request": normalized,
            "next_action": (
                "可以开始研究设计；缺失上下文必须保留为未知并生成委派任务。"
                if not missing_required and not evidence_warnings
                else "请 Leader 修复必填字段或证据格式后重试。"
            ),
        },
        ensure_ascii=False,
    )


@capability(
    id="research.validate_result",
    description="校验研究设计智能体返回给 Leader 的结构化 JSON 结果。",
    public=True,
    acps_skill=True,
    tags=["科研", "多智能体协作", "输出校验"],
)
def validate_result(result_json: str) -> str:
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError as exc:
        return json.dumps(
            {"valid": False, "errors": [f"结果不是有效 JSON：{exc.msg}"]},
            ensure_ascii=False,
        )

    if not isinstance(result, dict):
        return json.dumps(
            {"valid": False, "errors": ["结果必须是 JSON 对象。"]},
            ensure_ascii=False,
        )

    required = (
        "task_id",
        "agent_id",
        "status",
        "research_plan",
        "missing_inputs",
        "evidence_used",
        "assumptions",
        "delegations",
    )
    errors = [f"缺少字段：{field}" for field in required if field not in result]
    if result.get("agent_id") != "scinexus-research-designer":
        errors.append("agent_id 必须是 scinexus-research-designer。")
    allowed_statuses = {
        "complete",
        "needs_evidence",
        "needs_data",
        "needs_pilot",
        "needs_multiple_inputs",
    }
    if result.get("status") not in allowed_statuses:
        errors.append(
            "status 必须是 complete、needs_evidence、needs_data、needs_pilot 或 "
            f"needs_multiple_inputs；实际收到：{result.get('status')!r}。"
        )
    if "research_plan" in result and not isinstance(result["research_plan"], dict):
        errors.append("research_plan 必须是 JSON 对象。")
    for field in ("missing_inputs", "evidence_used", "assumptions", "delegations"):
        if field in result and not isinstance(result[field], list):
            errors.append(f"{field} 必须是列表。")
    delegations = result.get("delegations")
    if isinstance(delegations, list):
        for index, delegation in enumerate(delegations):
            if not isinstance(delegation, dict):
                errors.append(f"delegations 第 {index + 1} 项必须是对象。")
                continue
            missing = [
                field
                for field in ("target_role", "task", "reason")
                if not delegation.get(field)
            ]
            if missing:
                errors.append(
                    f"delegations 第 {index + 1} 项缺少字段：{', '.join(missing)}。"
                )

    return json.dumps(
        {"valid": not errors, "errors": errors, "normalized_result": result},
        ensure_ascii=False,
    )


@capability(
    id="research.estimate_sample_size",
    description=(
        "根据标准化效应量、显著性水平和统计功效，估算配对设计或双独立样本设计的"
        "样本量。同一批样本接受两种条件时应选择 paired；不同样本分组时选择 independent。"
        "结果只用于早期规划，不能替代正式统计咨询。"
    ),
    public=True,
    acps_skill=True,
    tags=["科研", "样本量", "统计规划"],
)
def estimate_sample_size(
    effect_size: float = 0.5,
    alpha: float = 0.05,
    power: float = 0.8,
    design: str = "paired",
    outcome_type: str = "continuous",
    p_baseline_wrong_rag_right: float | None = None,
    p_baseline_right_rag_wrong: float | None = None,
) -> str:
    if effect_size <= 0:
        return json.dumps({"error": "effect_size 必须大于 0"}, ensure_ascii=False)
    if not 0 < alpha < 1:
        return json.dumps({"error": "alpha 必须位于 0 和 1 之间"}, ensure_ascii=False)
    if not 0 < power < 1:
        return json.dumps({"error": "power 必须位于 0 和 1 之间"}, ensure_ascii=False)

    normalized_design = design.strip().lower()
    if normalized_design not in {"paired", "independent"}:
        return json.dumps(
            {"error": "design 必须是 paired 或 independent"},
            ensure_ascii=False,
        )

    normalized_outcome = outcome_type.strip().lower()
    if normalized_outcome not in {"continuous", "binary"}:
        return json.dumps(
            {"error": "outcome_type 必须是 continuous 或 binary"},
            ensure_ascii=False,
        )

    normal = NormalDist()
    z_alpha = normal.inv_cdf(1 - alpha / 2)
    z_power = normal.inv_cdf(power)

    if normalized_design == "paired" and normalized_outcome == "binary":
        p01 = p_baseline_wrong_rag_right
        p10 = p_baseline_right_rag_wrong
        if p01 is None or p10 is None:
            return json.dumps(
                {
                    "status": "needs_pilot_estimates",
                    "required_inputs": [
                        "p_baseline_wrong_rag_right",
                        "p_baseline_right_rag_wrong",
                    ],
                    "note": "配对二元准确率应按 McNemar 设计估算，需先由试点数据估计两类不一致比例。",
                },
                ensure_ascii=False,
            )
        if not 0 <= p01 <= 1 or not 0 <= p10 <= 1 or p01 == p10:
            return json.dumps(
                {"error": "两类不一致比例须在 0 到 1 之间且不能相等"},
                ensure_ascii=False,
            )
        discordant = p01 + p10
        difference = abs(p01 - p10)
        numerator = (
            z_alpha * math.sqrt(discordant)
            + z_power * math.sqrt(discordant - difference**2)
        ) ** 2
        pairs = math.ceil(numerator / difference**2)
        return json.dumps(
            {
                "design": "paired_binary_mcnemar",
                "alpha": alpha,
                "power": power,
                "p_baseline_wrong_rag_right": p01,
                "p_baseline_right_rag_wrong": p10,
                "estimated_pairs": pairs,
                "note": "McNemar 近似值；正式研究需结合试点数据、缺失率和多重比较复核。",
            },
            ensure_ascii=False,
        )

    base_size = ((z_alpha + z_power) / effect_size) ** 2
    if normalized_design == "paired":
        pairs = math.ceil(base_size)
        size_fields = {
            "estimated_pairs": pairs,
            "estimated_total_observations": pairs * 2,
        }
    else:
        per_group = math.ceil(2 * base_size)
        size_fields = {
            "estimated_per_group": per_group,
            "estimated_total": per_group * 2,
        }
    return json.dumps(
        {
            "design": normalized_design,
            "outcome_type": normalized_outcome,
            "effect_size": effect_size,
            "alpha": alpha,
            "power": power,
            **size_fields,
            "note": "早期近似值；正式研究需按实际检验、失访率和设计效应复核。",
        },
        ensure_ascii=False,
    )


@capability(
    id="research.assess_feasibility",
    description="根据数据、样本量、时间和计算资源检查研究方案的基础可行性。",
    public=True,
    acps_skill=True,
    tags=["科研", "可行性", "风险评估"],
)
def assess_feasibility(
    data_ready: bool | None = None,
    available_samples: int | None = None,
    required_samples: int | None = None,
    timeline_weeks: int | None = None,
    compute_level: str | None = None,
) -> str:
    supplied = {
        "data_ready": data_ready,
        "available_samples": available_samples,
        "required_samples": required_samples,
        "timeline_weeks": timeline_weeks,
        "compute_level": compute_level,
    }
    missing_inputs = [name for name, value in supplied.items() if value is None]
    if missing_inputs:
        return json.dumps(
            {
                "status": "insufficient_information",
                "missing_inputs": missing_inputs,
                "next_action": "请 Leader 或用户补充缺失条件；不得用假设值代填。",
            },
            ensure_ascii=False,
        )

    warnings: list[str] = []
    if not data_ready:
        warnings.append("数据尚未就绪，需要先完成获取、授权或清洗。")
    if available_samples < required_samples:
        warnings.append(
            f"可用样本量 {available_samples} 低于建议值 {required_samples}。"
        )
    if timeline_weeks < 4:
        warnings.append("周期少于 4 周，应缩小研究范围或减少非关键实验。")
    if compute_level.strip().lower() == "low":
        warnings.append("计算资源较低，应优先使用轻量基线并限制搜索空间。")

    return json.dumps(
        {
            "status": "needs_revision" if warnings else "feasible",
            "warnings": warnings,
            "next_action": (
                "先处理警告项，再冻结实验方案。"
                if warnings
                else "可以进入实验排期和预注册。"
            ),
        },
        ensure_ascii=False,
    )


@capability(
    id="research.validate_plan",
    description="检查 JSON 研究计划是否包含完整章节，并返回缺失项和基础质量警告。",
    public=True,
    acps_skill=True,
    tags=["科研", "方案校验", "可复现性"],
)
def validate_plan(plan_json: str) -> str:
    try:
        plan = json.loads(plan_json)
    except json.JSONDecodeError as exc:
        return json.dumps(
            {"valid": False, "error": f"研究计划不是有效 JSON：{exc.msg}"},
            ensure_ascii=False,
        )

    if not isinstance(plan, dict):
        return json.dumps(
            {"valid": False, "error": "研究计划必须是 JSON 对象。"},
            ensure_ascii=False,
        )

    missing = [key for key in REQUIRED_PLAN_SECTIONS if not plan.get(key)]
    warnings: list[str] = []
    if not plan.get("evidence"):
        warnings.append("未提供文献证据或来源；研究空白仍需检索智能体核验。")
    if not plan.get("assumptions"):
        warnings.append("未显式列出假设条件。")

    return json.dumps(
        {
            "valid": not missing,
            "missing_sections": missing,
            "warnings": warnings,
            "section_count": len(plan),
        },
        ensure_ascii=False,
    )


def build_research_designer(
    model: Any = None,
    *,
    enable_acps: bool = False,
) -> WitAgent:
    return WitAgent(
        id="scinexus-research-designer",
        display_name="Yanshu Research Designer",
        version="0.1.0",
        description="把研究问题和已有证据转化为可执行、可评估、可复现的研究方案。",
        instructions=(
            "你是一名严谨的研究设计与实验规划智能体。收到 JSON 协作请求时，必须先调用"
            "research_validate_request；必填字段或证据格式无效时停止设计并请求 Leader 修复。"
            "随后识别研究问题、证据、数据条件和约束；调用 research_plan_template 获取模板。"
            "涉及双独立样本均值比较"
            "或配对比较时，调用 research_estimate_sample_size。同一批问题、样本或被试同时"
            "接受两种条件时必须优先使用 paired；只有两组样本相互独立时才使用 independent。"
            "主指标为正确/错误等配对二元结果时，outcome_type 必须使用 binary，并按 McNemar "
            "设计处理；缺少两类不一致比例时，只能请求试点估计，不能套用连续效应量结果。"
            "统计检验必须与设计匹配。调用可行性工具时，未知参数必须省略并保留为 None，"
            "绝对不得为了完成工具调用而编造样本、周期、数据或算力数值。形成计划后，"
            "必须调用 research_validate_plan 校验，并在必要时修订。证据不足时，明确向检索"
            "或综合智能体提出补充任务，不得虚构论文或实验结果。最终使用中文输出：研究问题、"
            "研究空白、目标、假设、变量、数据需求、方法、基线、指标、消融实验、风险伦理、"
            "可复现步骤、成功与失败判据，以及建议 Leader 委派的后续任务。清楚区分已知事实、"
            "设计建议和待验证假设。"
            "当输入是包含 task_id 的 JSON 协作请求时，最终答案必须只输出一个 JSON 对象，"
            "不得使用 Markdown 代码围栏；对象必须符合协作协议并包含 task_id、agent_id、"
            "status、research_plan、missing_inputs、evidence_used、assumptions、delegations。"
            "agent_id 必须精确填写 scinexus-research-designer。delegations 中每一项必须使用"
            "target_role、task、reason 三个字段，不得用 to 等别名。"
            "status 只能使用 complete、needs_evidence、needs_data、needs_pilot、"
            "needs_multiple_inputs 之一；同时存在两类或更多缺口时必须使用 needs_multiple_inputs，"
            "具体缺口全部写入 missing_inputs，不得自创组合状态。"
        ),
        model=model,
        capabilities=[
            validate_request,
            validate_result,
            get_plan_template,
            estimate_sample_size,
            assess_feasibility,
            validate_plan,
        ],
        protocols=[AcpsDirect()] if enable_acps else [],
        amp_hooks=[AmpAccessLoggingHooks()] if enable_acps else [],
    )


def _provider_from_environment() -> OpenAIChatProvider:
    required = ("OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_BASE_URL")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise SystemExit(f"缺少环境变量：{', '.join(missing)}")
    return OpenAIChatProvider(
        model=os.environ["OPENAI_MODEL"],
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
        temperature=0,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Yanshu Research Designer")
    parser.add_argument("question", nargs="*", help="研究问题")
    parser.add_argument("--context", type=Path, help="包含综述或背景材料的 UTF-8 文本文件")
    parser.add_argument("--request", type=Path, help="多智能体协作请求 JSON 文件")
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()
    if args.request and (args.question or args.context):
        raise SystemExit("--request 不能与研究问题或 --context 同时使用。")

    if args.request:
        prompt = args.request.read_text(encoding="utf-8").strip()
        validation = json.loads(validate_request(prompt))
        if not validation["valid"]:
            issues = []
            if validation.get("error"):
                issues.append(validation["error"])
            issues.extend(
                f"缺少必填字段：{field}"
                for field in validation.get("missing_required", [])
            )
            issues.extend(validation.get("evidence_warnings", []))
            details = "；".join(issues)
            raise SystemExit(f"协作请求无效：{details}")
        prompt = (
            "以下是多智能体协作请求。完成研究设计后，只返回符合协作协议的 JSON 对象，"
            "不要输出 Markdown 标题、解释文字或代码围栏。\n\n" + prompt
        )
    else:
        question = " ".join(args.question).strip()
        if not question:
            question = input("请输入研究问题：").strip()
        if not question:
            raise SystemExit("研究问题不能为空。")

        context = ""
        if args.context:
            context = args.context.read_text(encoding="utf-8").strip()
        prompt = question
        if context:
            prompt = f"研究问题：\n{question}\n\n已有综述或背景材料：\n{context}"

    provider = _provider_from_environment()
    runtime = WitRuntime(build_research_designer(provider))
    try:
        result = await runtime.run(prompt)
        if args.request:
            raw_result = result.final_text.strip()
            if raw_result.startswith("```json") and raw_result.endswith("```"):
                raw_result = raw_result[7:-3].strip()
            result_validation = json.loads(validate_result(raw_result))
            if not result_validation["valid"]:
                details = "；".join(result_validation["errors"])
                raise SystemExit(f"智能体返回结果不符合协作协议：{details}")
            print(json.dumps(result_validation["normalized_result"], ensure_ascii=False, indent=2))
        else:
            print("\n研究设计方案：\n")
            print(result.final_text)
    finally:
        await provider.aclose()


if __name__ == "__main__":
    asyncio.run(main())
