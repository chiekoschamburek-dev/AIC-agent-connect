"""experiment-partner 离线单测：JSON 解析稳健性、能力可见性、名册注释态一致性。

真实 LLM 链路（代码生成→持 token 直连 GPU→分析报告）由 demo_gpu.py 的 G6 场景
覆盖（需 .env 的 LLM key 与 Docker Desktop）。本文件全部离线可跑：

    python -m unittest acps_cluster.tests.test_experiment_partner -v
"""

import json
import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import partner_experiment  # noqa: E402


class ExtractJsonTest(unittest.TestCase):
    """LLM 输出的 JSON 提取必须容忍围栏与前后废话（代码生成/报告分析共用）。"""

    def test_围栏json(self):
        text = '说明文字\n```json\n{"files": {"main.py": "print(1)"}}\n```\n尾声'
        self.assertEqual(partner_experiment._extract_json(text), {"files": {"main.py": "print(1)"}})

    def test_裸json带前后废话(self):
        text = '好的，以下是：{"verdict": "supported"} 以上。'
        self.assertEqual(partner_experiment._extract_json(text), {"verdict": "supported"})

    def test_嵌套与中文(self):
        text = '```json\n{"报告": {"指标": {"耗时ms": 12.5}}}\n```'
        self.assertEqual(partner_experiment._extract_json(text)["报告"]["指标"]["耗时ms"], 12.5)

    def test_无json抛错(self):
        with self.assertRaises(ValueError):
            partner_experiment._extract_json("没有任何结构化内容")


class DatasetChannelTest(unittest.TestCase):
    """2026-10-03：数据集走 gpu 宿主代下 → /data/<name>，**不**把 CSV 正文塞进模型上下文。

    教训：把 15KB CSV 内联进 main.py 要求两侧模型逐字转写两次，实测 6 次全部撞 300s 工具上限；
    而 gpu_runner 本来就设计了 `gpu.data.fetch`（宿主侧下载 → 容器只读 /data/<name>）。
    """

    URL = "https://www.openml.org/data/v1/download/28/optdigits.arff"

    def test_解析名称与URL(self):
        spec = partner_experiment._dataset_spec(f"optdigits | {self.URL}")
        self.assertEqual(spec["mode"], "fetch")
        self.assertEqual(spec["name"], "optdigits")
        self.assertEqual(spec["url"], self.URL)

    def test_只给URL时名字取末段(self):
        spec = partner_experiment._dataset_spec(self.URL)
        self.assertEqual(spec["mode"], "fetch")
        self.assertTrue(spec["name"])
        self.assertEqual(spec["url"], self.URL)

    def test_合成模式(self):
        for text in ("", "synthetic", "代码内合成、不读任何数据"):
            self.assertEqual(partner_experiment._dataset_spec(text)["mode"], "synthetic" if text else "none")

    def test_提示词给出_data路径而非正文(self):
        prompt = partner_experiment._dataset_for_prompt(partner_experiment._dataset_spec(f"optdigits | {self.URL}"))
        self.assertIn("/data/optdigits/", prompt)
        self.assertIn(self.URL, prompt)
        self.assertNotIn("必须原样内联", prompt)

    def test_闸门_读_data目录通过(self):
        spec = partner_experiment._dataset_spec("optdigits | " + self.URL)
        code = 'import csv\nrows = list(csv.reader(open("/data/optdigits/optdigits.arff")))\n'
        self.assertTrue(partner_experiment.dataset_followed(code, spec))
        # 按名字引用也算（代码自己拼 /data/<name>）
        self.assertTrue(partner_experiment.dataset_followed('p = "optdigits"', spec))
        # input 目录挂载也接受
        self.assertTrue(partner_experiment.dataset_followed('open("input/x.csv")', spec))

    def test_闸门_库自带数据集要打回(self):
        spec = partner_experiment._dataset_spec("optdigits | " + self.URL)
        self.assertFalse(partner_experiment.dataset_followed("from sklearn.datasets import load_digits", spec))
        self.assertFalse(partner_experiment.dataset_followed("import torchvision.datasets as d", spec))
        # 读都没读（纯合成）也要打回
        self.assertFalse(partner_experiment.dataset_followed("X = torch.randn(100, 64)", spec))

    def test_闸门_合成模式不拦(self):
        synthetic = partner_experiment._dataset_spec("synthetic")
        self.assertTrue(partner_experiment.dataset_followed("X = torch.randn(100, 64)", synthetic))
        none_spec = partner_experiment._dataset_spec("")
        self.assertTrue(partner_experiment.dataset_followed("import torch", none_spec))

    def test_实验员instructions要求传名字与URL(self):
        agent = partner_experiment.build_experiment_agent(None)
        self.assertIn("URL", agent.instructions)
        self.assertIn("不要把 CSV 正文", agent.instructions)


class ExperimentWiringTest(unittest.TestCase):
    def test_exp_run为唯一能力_公开_算力闸门在算力侧(self):
        from delegation.pep import capability_visibility

        ids = [getattr(c, "capability", c).id for c in partner_experiment.EXPERIMENT_CAPABILITIES]
        self.assertEqual(ids, ["exp.run"])
        public, has_non_public = capability_visibility(partner_experiment.EXPERIMENT_CAPABILITIES)
        # 2026-10-16 起重构：experiment 只做实验编排（公开可达，未指名入场即收窄到它），
        # 高成本闸门放在算力侧——gpu/cpu 的 PEP 按令牌放行；无令牌时本能力如实 awaiting。
        self.assertEqual(public, frozenset({"exp.run"}))
        self.assertFalse(has_non_public)

    def test_算力目标解析_gpu_cpu_及三类拒绝(self):
        import cluster_roster

        def member(key, caps, aic):
            return cluster_roster.RosterMember(
                key=key, name=f"研枢-{key}-Partner", role="", module="", workspace="",
                port_env="", default_port=0, declared_capabilities=tuple(caps), aic=aic)

        roster = [member("gpu", ("gpu.run", "gpu.data.fetch"), "1.2.3.gpu"),
                  member("cpu", ("cpu.run",), "1.2.3.cpu"),
                  member("scout", ("arxiv.search",), "1.2.3.scout"),
                  member("hpc", ("gpu.run",), None)]

        aic, skill, err = partner_experiment.resolve_compute_target(roster, "gpu")
        self.assertIsNone(err)
        self.assertEqual((aic, skill), ("1.2.3.gpu", "gpu.run"))
        aic, skill, err = partner_experiment.resolve_compute_target(roster, "研枢-cpu-Partner")
        self.assertIsNone(err)
        self.assertEqual((aic, skill), ("1.2.3.cpu", "cpu.run"))
        _, _, err = partner_experiment.resolve_compute_target(roster, "someone-else")
        self.assertIn("不在名册", err or "")
        _, _, err = partner_experiment.resolve_compute_target(roster, "scout")
        self.assertIn("不是算力 agent", err or "")
        _, _, err = partner_experiment.resolve_compute_target(roster, "hpc")
        self.assertIn("解析不到 AIC", err or "")

    def test_agent挂载委托钩子与PEP钩子(self):
        agent = partner_experiment.build_experiment_agent(None)
        hook_ids = [getattr(h, "id", "") for h in agent.aip_hooks]
        self.assertIn("yanshu.aip-partner-guard", hook_ids)
        self.assertIn("yanshu.delegation-token-scrub", hook_ids)
        self.assertIn("yanshu.pep", hook_ids)
        self.assertTrue(any(type(h).__name__ == "AipDelegationHooks" for h in agent.hooks))

    def test_身份已铸造且可自校验(self):
        import json

        from deploy.mint_gpu_identity import aic_checksum

        enrollment_path = CLUSTER_DIR / "ws-experiment" / "acps" / "enrollment.json"
        if not enrollment_path.is_file():
            self.skipTest("ws-experiment 未铸造")
        enrollment = json.loads(enrollment_path.read_text(encoding="utf-8"))
        aic = str(((enrollment.get("identity") or {}).get("ref") or {}).get("aic") or "")
        data_code, checksum = aic.rsplit(".", 1)
        self.assertEqual(aic_checksum(data_code), checksum)
        self.assertTrue(aic.startswith("1.2.156.3088.1.1.EXPRMT."))

    def test_名册experiment条目已启用(self):
        # 2026-10-02 平台注册通过后启用；未上线时以 available=False 呈现（ACS 缺失），不阻塞规划
        import cluster_roster

        roster = cluster_roster.load_roster()
        entries = [m for m in roster if m.key == "experiment"]
        self.assertEqual(len(entries), 1)
        member = entries[0]
        self.assertEqual(member.module, "partner_experiment")
        self.assertEqual(member.default_port, 8461)
        self.assertIn("exp.run", member.capabilities)


class AdmissionDenialTest(unittest.TestCase):
    """算力侧 admission 被拒必须折成"缺一枚新令牌"，不能记成实验结果。"""

    def test_拒绝折成awaiting并指名现签(self):
        out = partner_experiment.admission_denial(
            {"state": "rejected", "task_ref": "task_abc", "summary": "Invalid access token"})
        self.assertEqual(out["need"], "delegation.token")
        self.assertEqual(out["skill"], "gpu.run")
        self.assertTrue(out["blocking"])
        self.assertIn("Invalid access token", out["reason"])
        self.assertIn("重新签发", out["reason"])
        self.assertEqual(out["taskRef"], "task_abc")

    def test_无详情时也不抛且保留缺件语义(self):
        out = partner_experiment.admission_denial({"state": "rejected"})
        self.assertEqual(out["need"], "delegation.token")
        self.assertIn("无详情", out["reason"])


class 令牌通道自检Test(unittest.TestCase):
    """PEP 未启用时令牌会被受托侧静默丢弃——这个状态必须自己报出来。"""

    def test_桥未就绪时给出通道诊断(self):
        out = partner_experiment.token_channel_hint()
        self.assertEqual(out.get("channel"), "bridge-not-ready")
        self.assertIn("DelegationTokenScrubHooks", out.get("check", ""))

    def test_桥就绪时不再报通道问题(self):
        import asyncio
        from types import SimpleNamespace

        from delegation.bridge import BridgeHooks

        async def scenario():
            await BridgeHooks().on_run_start(
                SimpleNamespace(context=SimpleNamespace(runtime=object(), state=object())))
            return partner_experiment.token_channel_hint()

        # ContextVar 只在同一个 run 上下文里可见，故设桥与断言必须同处一个 asyncio.run
        self.assertEqual(asyncio.run(scenario()), {})


class 产物逐字提取Test(unittest.TestCase):
    """leader 要能拿到那 60 个 0/1 原值，而不是模型转述——提取必须结构化且宁缺勿造。"""

    def test_从内联artifacts提取(self):
        got = partner_experiment.harvest_artifact_contents(
            [{"artifacts": [{"name": "output/predictions.csv", "sizeBytes": 126,
                             "content": "label\n0\n1\n0\n"}]}])
        self.assertEqual(got, {"predictions.csv": "label\n0\n1\n0\n"})

    def test_产物以JSON文本回传时也能提取(self):
        payload = json.dumps({"ok": True, "artifacts": [
            {"name": "weights.json", "content": '{"w":[1,1]}'}]})
        got = partner_experiment.harvest_artifact_contents([{"type": "text", "text": payload}])
        self.assertEqual(got, {"weights.json": '{"w":[1,1]}'})

    def test_二进制与超大文件不内联(self):
        self.assertEqual(partner_experiment.harvest_artifact_contents(
            [{"artifacts": [{"name": "fig.png", "content": "AAAC", "encoding": "base64"}]}]), {})

    def test_没有内容时返回空而非编造(self):
        self.assertEqual(partner_experiment.harvest_artifact_contents(
            [{"artifacts": [{"name": "predictions.csv", "sizeBytes": 126}]}]), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
