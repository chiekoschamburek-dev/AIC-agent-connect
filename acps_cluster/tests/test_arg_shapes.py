"""工具入参数组宽容化的离线单测（2026-10-16 生产故障配套）。

现场症状：现网 MiniMax-M3.1-Flash-Preview 在一次会话里把 `plan_gate` 的
`needed_capabilities` / `steps` 送成非数组，严格 `isinstance(x, list)` 连拒两次，
leader 一步走不动（同一会话里 wit 内置 `acps_delegate` 的数组 `request` 也被投影层
拒掉，那条不在我们代码里，已另行上报）。这里覆盖：还原形态、内容仍照旧校验、
以及"垃圾字符串不能被当成数组放过"。
"""

import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.runtime import eval_runner  # noqa: E402
from acps_cluster.runtime import kb_runner  # noqa: E402
from acps_cluster.core.arg_shapes import as_list  # noqa: E402
from acps_cluster.core.cluster_roster import RosterMember, validate_plan  # noqa: E402
from acps_cluster.runtime.gpu_runner import validate_job  # noqa: E402


class AsListTest(unittest.TestCase):
    def test_已是数组原样返回(self):
        self.assertEqual(as_list(["a", "b"]), ["a", "b"])
        self.assertEqual(as_list([]), [])

    def test_json字符串数组被还原(self):
        self.assertEqual(as_list('["kb.schema"]'), ["kb.schema"])
        self.assertEqual(as_list('  [{"capability":"arxiv.search"}]  '),
                         [{"capability": "arxiv.search"}])
        self.assertEqual(as_list("[]"), [])

    def test_非数组形态一律不动(self):
        self.assertEqual(as_list("随便检索"), "随便检索")
        self.assertEqual(as_list("not json"), "not json")
        self.assertEqual(as_list("[1,2"), "[1,2")
        self.assertEqual(as_list('{"a": 1}'), '{"a": 1}')
        self.assertEqual(as_list({"a": 1}), {"a": 1})
        self.assertEqual(as_list(7), 7)
        self.assertEqual(as_list((1, 2)), (1, 2))
        self.assertIsNone(as_list(None))


class PlanGateCoercionTest(unittest.TestCase):
    """plan_gate 是本次故障的现场：字符串化数组要能过，内容不合格仍要拦。"""

    def setUp(self):
        self.roster = [RosterMember(
            key="scout", name="Scout", role="", module="partner_scout", workspace="ws-scout",
            port_env="ACPS_SCOUT_PORT", default_port=8445,
            declared_capabilities=("arxiv.search",), aic="AIC-SCOUT",
        )]

    def test_正常数组通过(self):
        out = validate_plan({"needed_capabilities": ["arxiv.search"],
                             "steps": [{"capability": "arxiv.search", "partner": "scout"}]},
                            self.roster)
        self.assertTrue(out["ok"], out["errors"])

    def test_字符串化数组同样通过(self):
        out = validate_plan({"needed_capabilities": '["arxiv.search"]',
                             "steps": '[{"capability":"arxiv.search","partner":"scout"}]'},
                            self.roster)
        self.assertTrue(out["ok"], out["errors"])
        self.assertEqual(len(out["resolved_steps"]), 1)

    def test_垃圾字符串仍按原口径拒绝(self):
        out = validate_plan({"needed_capabilities": "检索一下", "steps": ""}, self.roster)
        self.assertFalse(out["ok"])
        self.assertIn("needed_capabilities 必须是非空数组（列出本任务需要的能力 id）", out["errors"])
        self.assertIn("steps 必须是非空数组", out["errors"])

    def test_空数组仍是空(self):
        out = validate_plan({"needed_capabilities": "[]", "steps": "[]"}, self.roster)
        self.assertFalse(out["ok"])


class RunnerCoercionTest(unittest.TestCase):
    """四个干活 runner 的数组参数同样只宽容形态、不宽容内容。"""

    def test_gpu_entrypoint字符串化后进白名单校验(self):
        # 还原成功才会走到"首个可执行文件"这条——证明不再是"必须是非空数组"
        _spec, err = validate_job({"entrypoint": '["nvcc", "main.py"]'}, use_gpu=False)
        self.assertIsNotNone(err)
        self.assertIn("首个可执行文件只允许", err)

    def test_kb_records字符串化后进逐条校验(self):
        self.assertEqual(kb_runner.validate_ingest('["不是对象"]'), "records[0] 必须是对象")
        self.assertEqual(kb_runner.validate_ingest("随便写"), "records 必须是非空数组")

    def test_eval_predictions字符串化后不再报数组错(self):
        msg = eval_runner.validate_predictions("b1", '["1", "0"]')
        self.assertNotEqual(msg, "predictions 必须是非空数组（0/1 标签按 input.csv 行序）")

if __name__ == "__main__":
    unittest.main()
