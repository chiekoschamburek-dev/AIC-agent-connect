"""AutoResearch 链路接线自检（离线：不起服务、不调 LLM、不碰 GPU）。

2026-10-05 那次空收尾（`finalAnswerChars: 0`）事后定位到两处接线断裂：
demo 的 leader 没挂 `build_run_config`（于是 `auto_research`/`wait_partner` 对模型
根本不存在，且步数预算退回 wit 默认的 8 步）、实验员侧没有 gpu 的寻址桩。
两者都是"跑起来才知道"的错——本文件把它们变成跑之前就能断言的不变量：

  ① 自研模式需要的本地闸门工具，必须在 RunConfig 里（含预算不变量）；
  ② 剧本引用的工具名必须真的被装配（防止散文与工具集漂移）；
  ③ gpu 寻址桩能被解析成 demo 实例的端点（exp.run 出站的先决条件）；
  ④ wait_partner 的桥在挂了 BridgeHooks 后确实就绪（否则会退化成死工具）；
  ⑤ demo 的 leader 装配不退回漏接线的旧写法。

    python -m unittest acps_cluster.tests.test_ar_wiring_offline -v
"""

import asyncio
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cluster_roster  # noqa: E402
import supervisor  # noqa: E402
from delegation import demo_autoresearch as demo  # noqa: E402
from delegation.bridge import BridgeHooks, current_runtime  # noqa: E402
from wit.acps.discovery import extract_endpoint_url, load_discovered_agent  # noqa: E402

# 自研模式跑起来必须存在的本地工具（剧本里逐个点名要求模型调用）
REQUIRED_TOOLS = [
    "auto_research", "deep_research", "wait_partner", "wait_many",
    "plan_gate", "review_gate", "product_gate", "submit_job", "check_job",
    "acps_delegate_public", "acps_delegate_authenticated",
]


def _tool_names(roster) -> set[str]:
    return {t.name for t in supervisor.build_run_config(roster).tools}


class RunConfigAssemblyTest(unittest.TestCase):
    def setUp(self):
        self.roster = cluster_roster.load_roster()
        self.cfg = supervisor.build_run_config(self.roster)

    def test_自研模式所需工具全部装配(self):
        names = {t.name for t in self.cfg.tools}
        self.assertLessEqual(set(REQUIRED_TOOLS), names,
                             f"缺失: {sorted(set(REQUIRED_TOOLS) - names)}")

    def test_步数与超时预算撑得起迭代环(self):
        # 一轮环 ≈ 5-6 步（激活/检索/委派/等待/打卡），两轮加收尾远超 wit 默认的 8 步
        self.assertGreaterEqual(self.cfg.max_steps, 100, "max_steps 退回默认值，AR 环必撞顶")
        # 不变量：必须大于 _WAIT_BUDGET_CAP(400)，否则等待工具被框架先砍掉抛异常
        self.assertGreater(self.cfg.tool_timeout_seconds, 400.0)


class PlaybookToolReferenceTest(unittest.TestCase):
    def test_剧本点名的工具都被真实装配(self):
        roster = cluster_roster.load_roster()
        playbook = supervisor._playbook(
            cluster_roster.render_roster_text(roster), ["frontier", "experiment", "gpu"])
        available = _tool_names(roster)
        dangling = [n for n in REQUIRED_TOOLS if n in playbook and n not in available]
        self.assertEqual(dangling, [], f"剧本引用了未装配的工具: {dangling}")


class GpuAddressingStubTest(unittest.TestCase):
    """③ 实验员能不能把 GPU_AIC 解析到本次 demo 的实例端点。"""

    def test_桩解析出demo端点且声明gpu_run(self):
        # 寻址桩从 ws-gpu 的 dev 身份/ACS 投影生成；dev 工作区按禁入清单不入库。
        if not (CLUSTER_DIR / "ws-gpu" / "acps" / "acs.final.json").is_file():
            self.skipTest("ws-gpu 未铸造（dev 工作区不入库）；干净克隆跳过寻址桩检查")
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "discovered"
            written = demo.prime_gpu_stub(cache)
            self.assertIsNotNone(written, "寻址桩没写成文件")
            entry = load_discovered_agent(cache, demo.GPU_AIC)
            self.assertIsNotNone(entry, f"按 AIC 取不回桩：{demo.GPU_AIC}")
            raw = entry.get("rawAcs") or {}
            self.assertEqual(extract_endpoint_url(raw),
                             f"https://localhost:{demo.PORT_GPU}/rpc")
            self.assertIn("gpu.run", {s.get("id") for s in (raw.get("skills") or [])})


class WaitPartnerBridgeTest(unittest.TestCase):
    """④ BridgeHooks 在位时 wait_partner 必须真的可用（不是"桥未就绪"死工具）。"""

    def test_桥就绪后等待工具能取到终态(self):
        class FakeRuntime:
            async def aip_task(self, state, *, action, task_ref):
                return {"state": "completed", "products": [{"name": "weights.json"}],
                        "summary": "acc=0.91"}

        async def scenario():
            hook = BridgeHooks()
            await hook.on_run_start(SimpleNamespace(
                context=SimpleNamespace(runtime=FakeRuntime(), state=object())))
            self.assertIsNotNone(current_runtime(), "BridgeHooks 未把 runtime 写进桥")
            tool = next(t for t in supervisor.build_run_config(
                cluster_roster.load_roster()).tools if t.name == "wait_partner")
            return await tool.handler({"task_ref": "task_test"})

        outcome = asyncio.run(scenario())
        self.assertTrue(outcome.get("ok"), f"等待工具未就绪或行为异常: {outcome}")
        self.assertEqual(outcome.get("state"), "completed")


class DemoWiringRegressionTest(unittest.TestCase):
    """⑤ 防止 demo 退回 2026-10-05 的断线写法。"""

    def setUp(self):
        self.src = inspect.getsource(demo.main)

    def test_leader走真runconfig与桥(self):
        self.assertIn("supervisor.build_run_config(roster)", self.src)
        self.assertIn("BridgeHooks()", self.src)

    def test_实验员侧写了gpu寻址桩(self):
        self.assertIn("prime_gpu_stub(", self.src)

    def test_不再依赖无效的端点环境变量(self):
        # ACPS_CITATION_TARGET_ENDPOINT 只有 citation_verify.py 读，对 exp→gpu 无意义
        self.assertNotIn("ACPS_CITATION_TARGET_ENDPOINT", inspect.getsource(demo))


class BenchmarkLockTest(unittest.TestCase):
    """独立评分前必须确认两侧基准逐字节一致——标签错位会让结论整批作废。"""

    def test_两侧一致时放行并给出哈希(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.csv", Path(td) / "b.csv"
            a.write_text("x0,x1\n1.0,2.0\n", encoding="utf-8")
            b.write_text("x0,x1\n1.0,2.0\n", encoding="utf-8")
            ok, info = demo.benchmark_pair_ok(a, b)
            self.assertTrue(ok, info)
            self.assertEqual(len(info), 12)

    def test_任一侧被改动即拒绝(self):
        with tempfile.TemporaryDirectory() as td:
            a, b = Path(td) / "a.csv", Path(td) / "b.csv"
            a.write_text("x0,x1\n1.0,2.0\n", encoding="utf-8")
            b.write_text("x0,x1\n9.9,9.9\n", encoding="utf-8")
            ok, info = demo.benchmark_pair_ok(a, b)
            self.assertFalse(ok)
            self.assertIn("不一致", info)

    def test_文件缺失即拒绝(self):
        ok, info = demo.benchmark_pair_ok(Path("nope-a.csv"), Path("nope-b.csv"))
        self.assertFalse(ok)
        self.assertIn("缺失", info)

    def test_当前仓库里的demo_cls两侧一致(self):
        # datasets/ 与 eval-private/ 按禁入清单不入库（评测私有标签不出域亦不出库）；
        # 干净克隆没有基准文件时跳过，而不是把"文件缺失"误报成"两侧不一致"。
        published = demo.CLUSTER / "datasets" / demo.BENCH_ID / "input.csv"
        gold = demo.CLUSTER / "eval-private" / demo.BENCH_ID / "input.csv"
        if not published.is_file() or not gold.is_file():
            self.skipTest("datasets/eval-private 不入库；干净克隆跳过基准一致性检查")
        ok, info = demo.benchmark_pair_ok()
        self.assertTrue(ok, f"datasets 与 eval-private 的 demo-cls 已不一致：{info}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
