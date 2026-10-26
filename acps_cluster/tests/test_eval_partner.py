"""评测服务离线单测：指标/校验/榜单/防泄题返回形态（模式 B 的真实沙盒在 demo E 系列覆盖）。

    python -m unittest acps_cluster.tests.test_eval_partner -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.runtime import eval_runner  # noqa: E402


class BenchmarkTest(unittest.TestCase):
    def setUp(self):
        self.bench = eval_runner.ensure_demo_benchmark(rows=20, seed=7)

    def test_基准生成确定性(self):
        again = eval_runner.ensure_demo_benchmark(rows=20, seed=7)
        self.assertEqual(
            (self.bench / "input.csv").read_text(encoding="utf-8"),
            (again / "input.csv").read_text(encoding="utf-8"),
        )

    def test_标签噪声使满分不可达(self):
        labels = eval_runner._labels_of("demo-cls")
        # 全 0 / 全 1 / 规则完美预测都不该是 1.0（8% 噪声）
        score0 = eval_runner.score_accuracy("demo-cls", [0] * len(labels))["score"]
        score1 = eval_runner.score_accuracy("demo-cls", [1] * len(labels))["score"]
        self.assertLess(max(score0, score1), 1.0)

    def test_预测数不符报错(self):
        with self.assertRaises(ValueError):
            eval_runner.score_accuracy("demo-cls", [0, 1])


class PredictionsValidationTest(unittest.TestCase):
    def setUp(self):
        eval_runner.ensure_demo_benchmark(rows=10, seed=1)

    def test_非数组拒绝(self):
        self.assertIsNotNone(eval_runner.validate_predictions("demo-cls", "0101"))
        self.assertIsNotNone(eval_runner.validate_predictions("demo-cls", []))

    def test_未知基准拒绝(self):
        self.assertIn("未知基准", eval_runner.validate_predictions("nope", [0, 1]))

    def test_非整数标签拒绝(self):
        self.assertIsNotNone(eval_runner.validate_predictions("demo-cls", ["a", "b"]))


class LeaderboardTest(unittest.TestCase):
    def test_榜单按分数排序且持久(self):
        import json

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "board.json"
            eval_runner.record_leaderboard("demo-cls", "alice", 0.8, path=path)
            board = eval_runner.record_leaderboard("demo-cls", "bob", 0.95, path=path)
            self.assertEqual([e["submitter"] for e in board[:2]], ["bob", "alice"])
            reloaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(reloaded[0]["submitter"], "bob")


class CodeValidationTest(unittest.TestCase):
    def setUp(self):
        eval_runner.ensure_demo_benchmark(rows=5, seed=2)

    def test_合法提交通过(self):
        spec, error = eval_runner.validate_code_submission(
            {"benchmark": "demo-cls", "files": {"main.py": "print(1)"}, "timeout_seconds": 60}
        )
        self.assertIsNone(error)
        self.assertEqual(spec["timeout_seconds"], 60)

    def test_缺main_py拒绝(self):
        _, error = eval_runner.validate_code_submission({"benchmark": "demo-cls", "files": {"run.py": "x"}})
        self.assertIn("main.py", error)

    def test_路径穿越拒绝(self):
        _, error = eval_runner.validate_code_submission(
            {"benchmark": "demo-cls", "files": {"../escape.py": "x", "main.py": "x"}}
        )
        self.assertIsNotNone(error)

    def test_超时钳制(self):
        spec, _ = eval_runner.validate_code_submission(
            {"benchmark": "demo-cls", "files": {"main.py": "x"}, "timeout_seconds": 99999}
        )
        self.assertEqual(spec["timeout_seconds"], eval_runner.EVAL_TIMEOUT_SECONDS)

    def test_未知基准拒绝(self):
        _, error = eval_runner.validate_code_submission(
            {"benchmark": "nope", "files": {"main.py": "x"}}
        )
        self.assertIn("未知基准", error)


class SandboxArgsTest(unittest.TestCase):
    def test_沙盒旗标含防泄题闸门(self):
        spec, _ = eval_runner.validate_code_submission(
            {"benchmark": "demo-cls", "files": {"main.py": "x"}}
        )
        args = eval_runner._eval_docker_args(spec, "t", Path("/in.csv"), Path("/code"), Path("/out"))
        joined = " ".join(args)
        for guard in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges",
                      "--user 1000:1000", "--pids-limit 128"):
            self.assertIn(guard, joined)
        self.assertIn(":/eval/input.csv:ro", joined)   # 特征只读挂载
        self.assertNotIn("/data", joined)               # 无共享数据集挂载
        self.assertNotIn("labels", joined)              # 标签永不挂载


class WiringTest(unittest.TestCase):
    def test_能力档位_两公开一门槛(self):
        from acps_cluster.agents import partner_eval
        from acps_cluster.delegation.pep import capability_visibility

        ids = [getattr(c, "capability", c).id for c in partner_eval.EVAL_CAPABILITIES]
        self.assertEqual(ids, ["eval.benchmark_info", "eval.leaderboard", "eval.submit"])
        public, has_non = capability_visibility(partner_eval.EVAL_CAPABILITIES)
        self.assertEqual(public, frozenset({"eval.benchmark_info", "eval.leaderboard"}))
        self.assertTrue(has_non)

    def test_submit的ACS带域可见性声明(self):
        from acps_cluster.agents import partner_eval

        skill = partner_eval.eval_submit.capability.to_acps_skill()
        self.assertEqual(skill.get("visibility"), "domain")
        self.assertIn("yanshu-eval", skill.get("domains", []))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ModelPartnerTest(unittest.TestCase):
    """模型 agent（进程内底座）：输入域校验与两档能力。"""

    def test_合法批量推理(self):
        import asyncio

        from acps_cluster.agents import partner_model as pm

        result = asyncio.run(pm.model_predict(rows=[{"x0": 1.0, "x1": 0.5}, {"x0": -2, "x1": 0.5}]))
        self.assertTrue(result["ok"])
        self.assertEqual([p["label"] for p in result["predictions"]], [1, 0])

    def test_超输入域拒绝(self):
        import asyncio

        from acps_cluster.agents import partner_model as pm

        result = asyncio.run(pm.model_predict(rows=[{"x0": 99.0, "x1": 0}]))
        self.assertFalse(result["ok"])
        self.assertIn("输入域", result["error"])

    def test_超批量上限拒绝(self):
        import asyncio

        from acps_cluster.agents import partner_model as pm

        rows = [{"x0": 0, "x1": 0}] * (pm.MAX_BATCH + 1)
        result = asyncio.run(pm.model_predict(rows=rows))
        self.assertFalse(result["ok"])

    def test_能力档位与域可见性声明(self):
        from acps_cluster.agents import partner_model as pm
        from acps_cluster.delegation.pep import capability_visibility

        public, has_non = capability_visibility(pm.MODEL_CAPABILITIES)
        self.assertEqual(public, frozenset({"model.info"}))
        self.assertTrue(has_non)
        skill = pm.model_predict.capability.to_acps_skill()
        self.assertEqual(skill.get("visibility"), "domain")
