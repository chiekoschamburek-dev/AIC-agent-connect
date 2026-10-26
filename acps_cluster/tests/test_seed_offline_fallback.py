"""发现缓存预热的离线兜底与可观测性单测（2026-10-15 审查 #1/#2 配套）。

不联网：平台侧一律用打桩失败模拟（现网 10-15 平台叶子证书过期就是这种局面），
验证三件事——外部成员掉到库内脱敏副本、本集群成员不许用这条兜底、缺项必须让 rc≠0
并打印缺的是谁；以及父进程把子进程输出逐行转发进日志（此前是 DEVNULL，全哑）。
"""

import asyncio
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.path.insert(0, str(CLUSTER_DIR / "deploy"))
import seed_discovery  # noqa: E402


class OfflineAcsFallbackTest(unittest.TestCase):
    """offline_acs：只对外部成员放行，且副本身份必须仍然现役。"""

    def setUp(self):
        self.err = OSError("CERTIFICATE_VERIFY_FAILED: certificate has expired")

    def test_外部成员平台不可达时用库内副本(self):
        acs, source = seed_discovery.offline_acs("designer", self.err)
        self.assertIsNotNone(acs)
        self.assertEqual(acs["aic"], seed_discovery.FALLBACK_AICS["designer"])
        self.assertIn("库内脱敏副本", source)
        self.assertTrue(acs.get("endPoints"), "副本必须含可寻址端点")

    def test_本集群成员不走这条兜底(self):
        # 本集群成员的权威来源是本地终稿/平台，库内副本不该存在——缺就是缺
        self.assertEqual(seed_discovery.offline_acs("kb", self.err), (None, ""))

    def test_副本身份与兜底号不一致时拒绝使用(self):
        with mock.patch.dict(seed_discovery.FALLBACK_AICS,
                             {"designer": "1.2.156.3088.1.BUPT.999999.NNNNNN.NNNNNN.9999"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seed_discovery.offline_acs("designer", self.err), (None, ""))

    def test_副本文件缺失时不抛异常(self):
        missing = CLUSTER_DIR / "deploy" / "no_such_external_members.json"
        with mock.patch.object(seed_discovery, "_EXTERNAL_PATH", missing), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(seed_discovery.offline_acs("designer", self.err), (None, ""))


class MainMissingAccountingTest(unittest.TestCase):
    """main()：缺项要进 rc，也要说清缺的是谁。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seed-offline-"))
        (self.tmp / "acps" / "discovered").mkdir(parents=True)
        self.out = io.StringIO()

    def _run_main(self, members):
        with mock.patch.object(seed_discovery, "WS", self.tmp), \
                mock.patch.object(seed_discovery, "_local_acs", lambda *a, **k: None), \
                mock.patch.dict(seed_discovery.MEMBERS, members, clear=True), \
                mock.patch("httpx.Client", side_effect=OSError("certificate has expired")):
            with contextlib.redirect_stdout(self.out):
                return seed_discovery.main()

    def test_外部成员离线成_本集群成员缺_则rc非零(self):
        rc = self._run_main({"designer": "Designer", "kb": "KB"})
        text = self.out.getvalue()
        self.assertEqual(rc, 1, "有缺项就不能再报成全量成功")
        self.assertIn("库内脱敏副本", text)
        self.assertIn("缺 1 项", text)
        self.assertIn("kb", text)

    def test_外部成员全部离线可成时rc为零(self):
        self.assertEqual(self._run_main({"designer": "Designer"}), 0)


class SeedDiscoveryLoggingTest(unittest.TestCase):
    """_seed_discovery：子进程输出必须逐行进日志，缺项时按 rc 分档告警。"""

    def _drive(self, code: str):
        from acps_cluster.agents import partner_supervisor

        script = self.tmp / "fake_seed.py"
        script.write_text(code, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            asyncio.run(partner_supervisor._seed_discovery(script))
        return out.getvalue()

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="seed-log-"))

    def test_子进程输出被转发且rc为零(self):
        text = self._drive("print('  KB -> 平台公开接口')\n"
                           "print('缓存预热完成：9/9')\n")
        self.assertIn("[seed]   KB -> 平台公开接口", text)
        self.assertIn("预热完成（rc=0", text)
        self.assertIn("9/9", text)

    def test_rc非零时打出不完整告警(self):
        text = self._drive("print('缓存预热完成：8/9')\n"
                           "print('  ⚠ 缺 1 项：designer（委派这些伙伴会解析不到端点）')\n"
                           "raise SystemExit(1)\n")
        self.assertIn("⚠ 发现缓存预热不完整（rc=1", text)
        self.assertIn("designer", text)


if __name__ == "__main__":
    unittest.main()
