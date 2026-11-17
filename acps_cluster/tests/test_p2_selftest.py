"""P2 一键自证的离线单测（不联网、不签真实证书）。

覆盖：产物 JSON 提取、预检缺件、状态轮询状态机（假 probe）、CLI 解析。
需要 cryptography（p2_selftest 间接导入 entry_grant）；缺依赖时整组跳过。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from acps_cluster.tools import p2_selftest
    _IMPORT_ERROR = ""
except Exception as _exc:  # noqa: BLE001
    p2_selftest = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


@unittest.skipUnless(p2_selftest is not None, f"p2_selftest 不可导入：{_IMPORT_ERROR}")
class ExtractJsonTest(unittest.TestCase):
    def test_带自动提交前缀(self):
        text = "（本产物由研枢-Supervisor 在任务收尾时自动提交）\n\n" + json.dumps(
            {"ok": True, "root_id": "rt-1"}, ensure_ascii=False)
        self.assertEqual(p2_selftest._extract_json(text), {"ok": True, "root_id": "rt-1"})  # noqa: SLF001

    def test_非JSON开头也找得到(self):
        text = "说明文字 {\"a\": {\"b\": 1}} 尾巴"
        self.assertEqual(p2_selftest._extract_json(text), {"a": {"b": 1}})  # noqa: SLF001

    def test_没有JSON返回None(self):
        self.assertIsNone(p2_selftest._extract_json("纯文本"))  # noqa: SLF001

    def test_纯文本回执也能取root与job号(self):
        text = ("【受理回执】作业号：job-20261117-203321-5d9c\n"
                "根任务号：rt-20261117-203321-0643\n")
        self.assertEqual(
            p2_selftest._ids_from_text(text),  # noqa: SLF001
            ("rt-20261117-203321-0643", "job-20261117-203321-5d9c"),
        )

    def test_从产物预览取回执(self):
        result = {"product_previews": [
            {"id": "p1", "text": "前缀 " + json.dumps({"root_id": "rt-9"})},
        ]}
        self.assertEqual(p2_selftest._receipt_of(result)["root_id"], "rt-9")  # noqa: SLF001


@unittest.skipUnless(p2_selftest is not None, f"p2_selftest 不可导入：{_IMPORT_ERROR}")
class PreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="p2-pre-"))
        self.sup = self.tmp / "ws-supervisor"
        self.peer = self.tmp / "ws-kb"
        for ws in (self.sup, self.peer):
            (ws / "acps" / "client").mkdir(parents=True)
        (self.tmp / "deploy").mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _checks(self, *, submit: bool = False) -> dict[str, dict]:
        checks = p2_selftest.preflight(
            supervisor_workspace=self.sup, peer_workspace=self.peer,
            env_file=self.tmp / "deploy" / ".env.production", submit=submit,
        )
        return {item["id"]: item for item in checks}

    def test_缺证书与信任链会报错(self):
        checks = self._checks()
        self.assertFalse(checks["supervisor-cert"]["ok"])
        self.assertFalse(checks["peer-trust"]["ok"])
        self.assertFalse(checks["supervisor-endpoint"]["ok"])
        self.assertTrue(checks["existing-charter"]["ok"])  # 没有 domains 目录 = 可新建

    def test_文件齐全且PEP开启时预检通过(self):
        for ws in (self.sup, self.peer):
            acps = ws / "acps"
            (acps / "acs.final.json").write_text(json.dumps({
                "aic": "AIC-X",
                "endPoints": [{"transport": "JSONRPC", "url": "https://127.0.0.1:8444/rpc"}],
            }), encoding="utf-8")
            (acps / "client" / "certificate.pem").write_text("x", encoding="utf-8")
            (acps / "client" / "private-key.pem").write_text("x", encoding="utf-8")
            (acps / "trust-bundle.pem").write_text("x", encoding="utf-8")
        (self.tmp / "deploy" / ".env.production").write_text("ACPS_PEP_ENABLED=1\n", encoding="utf-8")
        checks = self._checks(submit=True)
        for key in ("supervisor-acs", "peer-cert", "supervisor-trust", "pep-enabled"):
            self.assertTrue(checks[key]["ok"], key)
        self.assertTrue(checks["supervisor-endpoint"]["ok"])
        # 端口不可达只影响 --submit 的运行期检查，预检本身仍通过
        self.assertFalse(checks["supervisor-reachable"]["ok"])


@unittest.skipUnless(p2_selftest is not None, f"p2_selftest 不可导入：{_IMPORT_ERROR}")
class PollTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="p2-poll-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _poll(self, states: list[dict]) -> dict:
        calls = {"n": 0}

        def fake_status(**kwargs):  # noqa: ANN003
            index = min(calls["n"], len(states) - 1)
            calls["n"] += 1
            return {
                "task_id": f"t-{calls['n']}",
                "product_previews": [{"text": json.dumps(states[index])}],
            }

        with mock.patch.object(p2_selftest, "status_once", fake_status), \
                mock.patch.object(p2_selftest.time, "sleep", lambda _s: None):
            return p2_selftest.poll_until_done(
                identity_workspace=self.tmp, supervisor_aic="AIC-S",
                endpoint="https://peer.example/rpc", root_id="rt-1",
                grant_path=str(self.tmp / "grant.json"), out_dir=self.tmp,
                interval_s=5, overall_timeout_s=600,
            )

    def test_轮询到completed(self):
        result = self._poll([{"state": "running"}, {"state": "completed", "result_chars": 10}])
        self.assertEqual(result["receipt"]["state"], "completed")
        self.assertEqual(result["calls"], 2)
        self.assertTrue((self.tmp / "status-01.json").is_file())

    def test_unknown_root提前结束(self):
        result = self._poll([{"error": "unknown-root", "state": ""}])
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["receipt"]["error"], "unknown-root")


@unittest.skipUnless(p2_selftest is not None, f"p2_selftest 不可导入：{_IMPORT_ERROR}")
class CliTest(unittest.TestCase):
    def test_默认不执行submit(self):
        args = p2_selftest.build_parser().parse_args([])
        self.assertFalse(args.submit)
        self.assertEqual(args.supervisor_workspace, "acps_cluster/ws-supervisor")
        args2 = p2_selftest.build_parser().parse_args(["--submit", "--request-id", "r1"])
        self.assertTrue(args2.submit)

    def test_摘要包含关键字段(self):
        text = p2_selftest._summary_markdown({  # noqa: SLF001
            "generated_at": "2026-11-17T20:00:00+08:00", "verdict": "passed",
            "supervisor_aic": "S", "peer_aic": "P",
            "charter": {"mode": "minted", "revision": 1, "signers": ["S", "P"]},
            "grant": {"jti": "j", "expiresAt": 1, "maxUses": 5, "path": "g.json"},
            "submit": {"task_id": "T1", "final_state": "completed",
                        "root_id": "rt-1", "job_id": "job-1"},
            "status": {"calls": 2, "receipt": {"state": "completed", "result_chars": 42}},
            "checks": [{"id": "x", "ok": True, "detail": "d"}],
        })
        for token in ("PASSED", "T1", "rt-1", "job-1", "jti=`j`"):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)