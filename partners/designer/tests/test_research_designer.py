import json
import unittest

from research_designer import (
    assess_feasibility,
    estimate_sample_size,
    get_plan_template,
    validate_request,
    validate_result,
    validate_plan,
)


class ResearchDesignerCapabilitiesTest(unittest.TestCase):
    def test_machine_learning_template(self) -> None:
        result = json.loads(get_plan_template("machine_learning"))
        self.assertIn("防止数据泄漏", result["specialized_checks"])
        self.assertIn("hypotheses", result["required_sections"])

    def test_independent_sample_size_estimate(self) -> None:
        result = json.loads(estimate_sample_size(0.5, 0.05, 0.8, "independent"))
        self.assertEqual(result["estimated_per_group"], 63)
        self.assertEqual(result["estimated_total"], 126)

    def test_paired_sample_size_estimate(self) -> None:
        result = json.loads(estimate_sample_size(0.5, 0.05, 0.8, "paired"))
        self.assertEqual(result["estimated_pairs"], 32)
        self.assertEqual(result["estimated_total_observations"], 64)

    def test_binary_paired_requires_pilot_estimates(self) -> None:
        result = json.loads(
            estimate_sample_size(0.5, 0.05, 0.8, "paired", "binary")
        )
        self.assertEqual(result["status"], "needs_pilot_estimates")

    def test_feasibility_does_not_invent_missing_inputs(self) -> None:
        result = json.loads(assess_feasibility())
        self.assertEqual(result["status"], "insufficient_information")
        self.assertIn("timeline_weeks", result["missing_inputs"])

    def test_feasibility_reports_constraints(self) -> None:
        result = json.loads(assess_feasibility(False, 30, 100, 2, "low"))
        self.assertEqual(result["status"], "needs_revision")
        self.assertEqual(len(result["warnings"]), 4)

    def test_plan_validation_reports_missing_sections(self) -> None:
        result = json.loads(validate_plan('{"research_question": "示例问题"}'))
        self.assertFalse(result["valid"])
        self.assertIn("methodology", result["missing_sections"])

    def test_request_requires_task_and_question(self) -> None:
        result = json.loads(validate_request("{}"))
        self.assertFalse(result["valid"])
        self.assertEqual(
            result["missing_required"],
            ["task_id", "research_question"],
        )

    def test_request_rejects_unverifiable_evidence(self) -> None:
        request = {
            "task_id": "task-001",
            "research_question": "示例问题",
            "literature_evidence": [{"title": "无来源论文"}],
        }
        result = json.loads(validate_request(json.dumps(request, ensure_ascii=False)))
        self.assertFalse(result["valid"])
        self.assertEqual(len(result["evidence_warnings"]), 1)

    def test_minimal_request_is_valid_with_missing_context(self) -> None:
        request = {"task_id": "task-001", "research_question": "示例问题"}
        result = json.loads(validate_request(json.dumps(request, ensure_ascii=False)))
        self.assertTrue(result["valid"])
        self.assertIn("data_profile", result["missing_context"])

    def test_result_contract_accepts_structured_handoff(self) -> None:
        handoff = {
            "task_id": "task-001",
            "agent_id": "scinexus-research-designer",
            "status": "needs_evidence",
            "research_plan": {},
            "missing_inputs": ["literature_evidence"],
            "evidence_used": [],
            "assumptions": [],
            "delegations": [
                {
                    "target_role": "retrieval",
                    "task": "补充文献证据",
                    "reason": "研究空白尚未核验",
                }
            ],
        }
        result = json.loads(validate_result(json.dumps(handoff, ensure_ascii=False)))
        self.assertTrue(result["valid"])

    def test_result_contract_rejects_routing_aliases(self) -> None:
        handoff = {
            "task_id": "task-001",
            "agent_id": "research_design_agent",
            "status": "needs_evidence",
            "research_plan": {},
            "missing_inputs": [],
            "evidence_used": [],
            "assumptions": [],
            "delegations": [{"to": "retrieval_agent", "task": "检索"}],
        }
        result = json.loads(validate_result(json.dumps(handoff, ensure_ascii=False)))
        self.assertFalse(result["valid"])
        self.assertTrue(any("agent_id" in error for error in result["errors"]))
        self.assertTrue(any("target_role" in error for error in result["errors"]))

    def test_result_contract_rejects_markdown(self) -> None:
        result = json.loads(validate_result("# 研究计划"))
        self.assertFalse(result["valid"])

    def test_result_contract_accepts_multiple_missing_inputs(self) -> None:
        handoff = {
            "task_id": "task-001",
            "agent_id": "scinexus-research-designer",
            "status": "needs_multiple_inputs",
            "research_plan": {},
            "missing_inputs": ["literature_evidence", "pilot_estimates"],
            "evidence_used": [],
            "assumptions": [],
            "delegations": [],
        }
        result = json.loads(validate_result(json.dumps(handoff, ensure_ascii=False)))
        self.assertTrue(result["valid"])


if __name__ == "__main__":
    unittest.main()
