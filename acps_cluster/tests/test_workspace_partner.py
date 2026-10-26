"""workspace-partner 离线单测：路径逃逸防线、配额、读写回路、克隆闸门、名册接线。

全部离线可跑（克隆用本地 file:// 仓库 + 显式开关）：

    python -m pytest acps_cluster/tests/test_workspace_partner.py -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.agents import partner_workspace as pw  # noqa: E402


class WorkspaceCoreTest(unittest.TestCase):
    """ws.* 核心逻辑（模块级纯函数，直接以临时根调用）。"""

    TID = "task-demo-1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-partner-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ── 基本回路 ──

    def test_创建_写入_读取回路(self):
        out = pw.ws_create(self.root, self.TID)
        self.assertTrue(out["ok"])
        again = pw.ws_create(self.root, self.TID)
        self.assertTrue(again["ok"])
        self.assertIn("复用", again["note"])
        w = pw.ws_write(self.root, self.TID, "shared/摘要.md", "# 摘要\n正文")
        self.assertTrue(w["ok"])
        r = pw.ws_read(self.root, self.TID, "shared/摘要.md")
        self.assertTrue(r["ok"])
        self.assertEqual(r["content"], "# 摘要\n正文")
        self.assertFalse(r["truncated"])

    def test_未创建就写被拒(self):
        out = pw.ws_write(self.root, "ghost", "shared/a.txt", "x")
        self.assertEqual(out["error"], "no-such-workspace")

    def test_base64回路(self):
        pw.ws_create(self.root, self.TID)
        raw = bytes(range(256))
        w = pw.ws_write(self.root, self.TID, "shared/blob.bin",
                        base64.b64encode(raw).decode("ascii"), encoding="base64")
        self.assertTrue(w["ok"])
        r = pw.ws_read(self.root, self.TID, "shared/blob.bin")
        self.assertEqual(r["encoding"], "base64")
        self.assertEqual(base64.b64decode(r["content"]), raw)

    # ── task_id 与路径防线 ──

    def test_task_id白名单(self):
        for bad in ("../escape", "", "a/b", "a b", "x" * 65, ".hidden"):
            out = pw.ws_create(self.root, bad)
            self.assertEqual(out.get("error"), "invalid-task-id", bad)

    def test_路径逃逸被拒(self):
        pw.ws_create(self.root, self.TID)
        for bad in ("../escape.txt", "..\\escape.txt", "/etc/passwd", "a/../../b"):
            out = pw.ws_write(self.root, self.TID, bad, "x", actor="scout")
            # 分区闸在前：非四分区路径一律 zone-required（逃逸路径到不了解析层）
            self.assertEqual(out.get("error"), "zone-required", bad)
        # 纯解析层的逃逸防线仍然单测（shared/ 下构造逃逸）
        # shared/../ 走廊由分区包含校验拦下（zone-escape）；真正的任务区逃逸（../../）
        # 由解析层拦截（path-rejected）
        for bad, expected in (("shared/../escape.txt", "zone-escape"),
                              ("shared/a/../../b", "zone-escape"),
                              ("shared/../../escape.txt", "path-rejected")):
            out = pw.ws_write(self.root, self.TID, bad, "x", actor="scout")
            self.assertEqual(out.get("error"), expected, bad)
        # 逃逸目标确实没有被创建
        self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_symlink逃逸被拒(self):
        pw.ws_create(self.root, self.TID)
        outside = self.root.parent / "outside-secret.txt"
        outside.write_text("机密", encoding="utf-8")
        link = self.root / self.TID / "shared" / "link.txt"
        try:
            (self.root / self.TID / "shared").mkdir(parents=True, exist_ok=True)
            os.symlink(outside, link)
        except OSError:
            self.skipTest("Windows 无 symlink 特权（生产服务器为 Linux，照常覆盖）")
        out = pw.ws_write(self.root, self.TID, "shared/link.txt", "覆盖", actor="scout")
        self.assertEqual(out.get("error"), "path-rejected")
        self.assertEqual(outside.read_text(encoding="utf-8"), "机密")
        outside.unlink()

    # ── 配额与截断 ──

    def test_配额超限拒绝(self):
        old = os.environ.get("ACPS_WS_TASK_QUOTA_MB")
        os.environ["ACPS_WS_TASK_QUOTA_MB"] = "1"
        try:
            pw.ws_create(self.root, self.TID)
            big = "x" * (1500 * 1024)   # > 1MB 配额
            out = pw.ws_write(self.root, self.TID, "shared/big.bin", big, actor="scout")
            self.assertEqual(out.get("error"), "quota-exceeded")
            self.assertFalse((self.root / self.TID / "shared" / "big.bin").exists())
        finally:
            if old is None:
                os.environ.pop("ACPS_WS_TASK_QUOTA_MB", None)
            else:
                os.environ["ACPS_WS_TASK_QUOTA_MB"] = old

    def test_读取截断如实标注(self):
        pw.ws_create(self.root, self.TID)
        pw.ws_write(self.root, self.TID, "shared/long.txt", "x" * 10000, actor="scout")
        r = pw.ws_read(self.root, self.TID, "shared/long.txt", max_bytes=1024)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["encoding"], "text")
        self.assertLessEqual(len(r["content"]), 1024)

    def test_列目录与回收(self):
        pw.ws_create(self.root, self.TID)
        pw.ws_write(self.root, self.TID, "scratch/scout/a/b.txt", "1", actor="scout")
        listing = pw.ws_list(self.root, self.TID, depth=4)
        self.assertTrue(listing["ok"])
        paths = {e["path"] for e in listing["entries"]}
        self.assertIn("scratch/scout/a/b.txt", paths)
        g = pw.ws_gc(self.root, self.TID)
        self.assertTrue(g["ok"])
        self.assertFalse((self.root / self.TID).exists())

    def test_按天回收(self):
        import time

        old_tid = "task-old"
        pw.ws_create(self.root, old_tid)
        d = self.root / old_tid
        past = time.time() - 30 * 86400
        os.utime(d, (past, past))
        pw.ws_create(self.root, "task-new")
        out = pw.ws_gc(self.root, older_than_days=7)
        self.assertTrue(out["ok"])
        self.assertFalse(d.exists())
        self.assertTrue((self.root / "task-new").exists())

    def test_根目录不存在时批量回收不报错(self):
        out = pw.ws_gc(self.root / "missing-root", older_than_days=7)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], [])

    # ── 克隆闸门 ──

    def test_克隆_scheme与凭据闸门(self):
        pw.ws_create(self.root, self.TID)
        for bad, err in (("http://github.com/a/b.git", "bad-url"),
                         ("ftp://github.com/a/b.git", "bad-url"),
                         ("file:///tmp/x", "bad-url"),
                         ("https://user:pw@github.com/a/b.git", "bad-url"),
                         ("https://127.0.0.1/x.git", "private-host"),
                         ("https://localhost/x.git", "private-host")):
            out = pw.ws_clone_repo(self.root, self.TID, bad)
            self.assertEqual(out.get("error"), err, bad)

    def test_克隆本地file仓库_显式开关(self):
        old = os.environ.get("ACPS_WS_CLONE_ALLOW_FILE")
        os.environ["ACPS_WS_CLONE_ALLOW_FILE"] = "1"
        try:
            src = Path(self._tmp.name) / "src-repo"
            src.mkdir(parents=True)
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            def git(*args: str) -> None:
                subprocess.run(["git", *args], cwd=src, env=env,
                               capture_output=True, text=True, check=True)
            git("init", "-q")
            git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
            (src / "README.md").write_text("# demo 仓库\n用于克隆测试", encoding="utf-8")
            git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
            git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
            pw.ws_create(self.root, self.TID)
            out = pw.ws_clone_repo(self.root, self.TID, src.as_uri())
            self.assertTrue(out.get("ok"), out)
            self.assertTrue((self.root / self.TID / "repo" / "src-repo" / "README.md").exists())
            self.assertIn("demo 仓库", out.get("readme_head", ""))
        finally:
            if old is None:
                os.environ.pop("ACPS_WS_CLONE_ALLOW_FILE", None)
            else:
                os.environ["ACPS_WS_CLONE_ALLOW_FILE"] = old


class WorkspaceRosterTest(unittest.TestCase):
    def test_名册含workspace条目且能力齐(self):
        from acps_cluster.core import cluster_roster

        members = {m.key: m for m in cluster_roster.load_roster()}
        self.assertIn("workspace", members)
        m = members["workspace"]
        self.assertEqual(m.default_port, 8470)
        for cap in ("ws.create", "ws.write", "ws.read", "ws.list",
                    "ws.clone_repo", "ws.gc"):
            self.assertIn(cap, m.declared_capabilities)

    def test_能力可见性_全部非公开(self):
        from acps_cluster.delegation.pep import capability_visibility

        public, has_non_public = capability_visibility(partner_workspace_caps())
        self.assertEqual(public, frozenset())
        self.assertTrue(has_non_public)

    def test_agent装配_带amp钩子与PEP(self):
        agent = partner_workspace_agent()
        names = [type(h).__name__ for h in agent.hooks]
        self.assertIn("AipDelegationHooks", names)
        self.assertIn("BridgeHooks", names)
        aip_names = [type(h).__name__ for h in agent.aip_hooks]
        self.assertIn("AipPartnerGuardHooks", aip_names)
        # 计量钩子是独立通道（amp_hooks），10-23 丢钩子事故的教训——单独断言：
        self.assertEqual([type(h).__name__ for h in agent.amp_hooks],
                         ["AmpAccessLoggingHooks"])


class TaskScopeTest(unittest.TestCase):
    """任务作用域：令牌 task_id claim 与请求一致性校验（2026-10-25）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-scope-")
        self.root = Path(self._tmp.name)
        self._root_env = os.environ.get("ACPS_WS_ROOT")
        os.environ["ACPS_WS_ROOT"] = self._tmp.name
        from acps_cluster.delegation.bridge import set_delegation
        self._set = set_delegation

    def tearDown(self) -> None:
        self._set(None)
        if self._root_env is None:
            os.environ.pop("ACPS_WS_ROOT", None)
        else:
            os.environ["ACPS_WS_ROOT"] = self._root_env
        self._tmp.cleanup()

    @staticmethod
    def _fake_token(task_id: str) -> str:
        import base64
        payload = base64.urlsafe_b64encode(
            json.dumps({"task_id": task_id, "skill_id": "ws.write"}).encode()).decode()
        return f"hdr.{payload}.sig"

    def test_作用域一致放行(self):
        pw.ws_create(self.root, "task-a")
        self._set({"token": self._fake_token("task-a"), "targetAic": "w", "skillId": "ws.write"})
        out = pw.ws_write_cap("task-a", "shared/a.txt", "内容")
        self.assertTrue(out["ok"], out)

    def test_跨任务写被拒且不落盘(self):
        pw.ws_create(self.root, "task-a")
        pw.ws_create(self.root, "task-b")
        self._set({"token": self._fake_token("task-a"), "targetAic": "w", "skillId": "ws.write"})
        out = pw.ws_write_cap("task-b", "shared/evil.txt", "越权")
        self.assertEqual(out.get("error"), "scope-mismatch")
        self.assertFalse((self.root / "task-b" / "shared" / "evil.txt").exists())

    def test_无claim令牌按fail_closed拒绝(self):
        self._set({"token": self._fake_token("") or "hdr..sig", "targetAic": "w", "skillId": "ws.write"})
        out = pw.ws_write_cap("task-a", "shared/a.txt", "x")
        self.assertEqual(out.get("error"), "scope-missing")

    def test_无委派直调拒绝(self):
        self._set(None)
        out = pw.ws_write_cap("task-a", "shared/a.txt", "x")
        self.assertEqual(out.get("error"), "scope-required")

    def test_读取与回收同样受作用域约束(self):
        pw.ws_create(self.root, "task-a")
        pw.ws_write(self.root, "task-a", "shared/secret.txt", "数据", actor="scout")
        self._set({"token": self._fake_token("task-b"), "targetAic": "w", "skillId": "ws.read"})
        r = pw.ws_read_cap("task-a", "shared/secret.txt")
        self.assertEqual(r.get("error"), "scope-mismatch")
        g = pw.ws_gc_cap("task-a")
        self.assertEqual(g.get("error"), "scope-mismatch")
        self.assertTrue((self.root / "task-a" / "shared" / "secret.txt").exists())

    def test_令牌claim解码(self):
        from acps_cluster.delegation.bridge import set_delegation
        set_delegation({"token": self._fake_token("task-z"), "targetAic": "w", "skillId": "ws.write"})
        self.assertEqual(pw._delegation_task_id(), "task-z")
        set_delegation({"token": "not-a-jws", "targetAic": "w", "skillId": "ws.write"})
        self.assertIsNone(pw._delegation_task_id())
        set_delegation(None)
        self.assertIsNone(pw._delegation_task_id())


class TaskScopeExtractionTest(unittest.TestCase):
    """leader 侧：从委派参数/请求文本提取任务作用域。"""

    def test_显式参数优先(self):
        from acps_cluster.agents.supervisor import ResourceDispatchAuthHooks
        args = {"task_id": "task-explicit", "request": "task_id=task-in-text"}
        self.assertEqual(ResourceDispatchAuthHooks._task_scope_from_args(args), "task-explicit")

    def test_从请求文本按约定提取(self):
        from acps_cluster.agents.supervisor import ResourceDispatchAuthHooks
        args = {"request": "委派工作区任务，task_id=task-1101，克隆仓库"}
        self.assertEqual(ResourceDispatchAuthHooks._task_scope_from_args(args), "task-1101")

    def test_取不到返回空(self):
        from acps_cluster.agents.supervisor import ResourceDispatchAuthHooks
        self.assertEqual(ResourceDispatchAuthHooks._task_scope_from_args({"request": "没有任何标记"}), "")

    def test_委派钩子同时覆盖两种delegate工具(self):
        import inspect
        from acps_cluster.agents.supervisor import ResourceDispatchAuthHooks
        src = inspect.getsource(ResourceDispatchAuthHooks.wrap_tool_call)
        # 两种工具都在钩子的识别名单里；authenticated 走它自己的 handler（2026-10-26）
        self.assertIn('"acps_delegate_authenticated"', src)
        self.assertIn('"acps_delegate"', src)



class WorkspaceAcsProjectionTest(unittest.TestCase):
    """代码投影（含 workspace 运行时裁剪）必须与平台已批 ACS 逐字一致。"""

    def test_投影与平台已批ACS一致(self):
        snap = CLUSTER_DIR / "_platform_snapshot" / "acs.live.workspace.json"
        if not snap.is_file():
            self.skipTest(f"没有平台快照：{snap}")
        pub = json.loads(snap.read_text(encoding="utf-8"))
        endpoint = (pub.get("endPoints") or [{}])[0].get("url") or "https://127.0.0.1:8470/rpc"
        from urllib.parse import urlsplit
        from wit import AcpsConfig, AgentCertificateRequest, AgentProviderProfile

        host = urlsplit(endpoint).hostname or "127.0.0.1"
        provider = pub.get("provider") or {}
        cfg = AcpsConfig.custom(
            registry_url="https://wt.ioa.pub/registry-server",
            ca_url="https://wt.ioa.pub/ca-server",
            discovery_url="https://wt.ioa.pub/discovery/acps-adp-v2/discover",
            public_endpoint=endpoint,
            certificate=AgentCertificateRequest(ip=(host,)),
            provider=AgentProviderProfile(
                name=provider.get("name", "wit-research-lab"),
                organization=provider.get("organization", "北京邮电大学"),
                email=provider.get("email", "huaibin2025@outlook.com"),
                country_code=provider.get("countryCode", "CN"),
            ),
            strict_interop=True,
        )
        with tempfile.TemporaryDirectory(prefix="ws-acs-") as td:
            runtime = pw.WorkspaceRuntime(
                pw.build_workspace_agent(None), workspace=td, acps=cfg
            )
            draft = runtime.acs_draft()
            compiled_draft = runtime.compiled.acs_draft

        def stable(value):
            value = json.loads(json.dumps(value, ensure_ascii=False))
            for key in ("aic", "active", "lastModifiedTime"):
                value.pop(key, None)
            return value

        self.assertEqual(stable(pub), stable(draft))
        # provisioner 走的是 compiled.acs_draft；这里必须一起校验
        self.assertEqual(stable(pub), stable(compiled_draft))

    def test_平台快照指纹与代码投影一致(self):
        snap = CLUSTER_DIR / "_platform_snapshot" / "acs.live.workspace.json"
        if not snap.is_file():
            self.skipTest(f"没有平台快照：{snap}")
        from wit.acps.provisioner import _fingerprint

        pub = json.loads(snap.read_text(encoding="utf-8"))
        self.assertEqual(
            _fingerprint(pub),
            "9a07103a17a52a20407039b77d3a6527763ff822e20c38c47c206e1ddffe516d",
            "平台快照指纹变了；若平台确实重新批过 ACS，请同步更新此基线与覆盖表",
        )


class WorkspaceHardeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-hardening-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_克隆超时可配且非法值回退默认(self):
        old = os.environ.get("ACPS_WS_CLONE_TIMEOUT")
        os.environ["ACPS_WS_CLONE_TIMEOUT"] = "7"
        try:
            self.assertEqual(pw._clone_timeout(), 7)
            os.environ["ACPS_WS_CLONE_TIMEOUT"] = "bad"
            self.assertEqual(pw._clone_timeout(), pw.DEFAULT_CLONE_TIMEOUT)
        finally:
            if old is None:
                os.environ.pop("ACPS_WS_CLONE_TIMEOUT", None)
            else:
                os.environ["ACPS_WS_CLONE_TIMEOUT"] = old

    def test_README符号链接不读外部文件(self):
        target = self.root / "repo"
        target.mkdir()
        secret = self.root / "secret.txt"
        secret.write_text("server-secret", encoding="utf-8")
        try:
            (target / "README.md").symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("Windows 无 symlink 特权（生产服务器为 Linux，照常覆盖）")
        self.assertEqual(pw._read_readme_head(target), "")
        # 普通 README 仍正常返回
        (target / "README.md").unlink()
        (target / "README.md").write_text("# demo", encoding="utf-8")
        self.assertIn("demo", pw._read_readme_head(target))

    def test_列目录不跟随symlink(self):
        pw.ws_create(self.root, "task-link")
        outside = self.root / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "task-link" / "link.txt"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Windows 无 symlink 特权（生产服务器为 Linux，照常覆盖）")
        out = pw.ws_list(self.root, "task-link")
        entry = next(e for e in out["entries"] if e["path"] == "link.txt")
        self.assertEqual(entry["type"], "symlink")
        self.assertIsNone(entry["bytes"])

    def test_批量GC不再从委派能力开放(self):
        out = pw.ws_gc_cap("", older_than_days=7)
        self.assertEqual(out.get("error"), "batch-gc-disabled")


class ZoneRulesTest(unittest.TestCase):
    """三分区强制：repo 只读 / products 服务端版本化 / scratch 领地 / shared 总线。"""

    TID = "task-zone"
    SCOUT_AIC = "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-zone-")
        self.root = Path(self._tmp.name)
        self._env = os.environ.get("ACPS_WS_ROOT")
        os.environ["ACPS_WS_ROOT"] = self._tmp.name
        from acps_cluster.delegation.bridge import set_delegation
        self._set = set_delegation
        self._set({"token": self._token(self.SCOUT_AIC), "targetAic": "w", "skillId": "ws.write"})

    def tearDown(self) -> None:
        self._set(None)
        if self._env is None:
            os.environ.pop("ACPS_WS_ROOT", None)
        else:
            os.environ["ACPS_WS_ROOT"] = self._env
        self._tmp.cleanup()

    @staticmethod
    def _token(aic: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(
            {"task_id": "task-zone", "act": {"aic": aic, "sub": aic}}).encode()).decode()
        return f"hdr.{payload}.sig"

    def test_caller身份从令牌act解析出名册key(self):
        from acps_cluster.core import cluster_roster
        from unittest import mock
        member = cluster_roster.RosterMember(
            key="scout", name="研枢-Scout-Partner", role="", module="",
            workspace="ws-scout", port_env="", default_port=0, aic=self.SCOUT_AIC)
        with mock.patch.object(cluster_roster, "load_roster", return_value=[member]):
            self.assertEqual(pw._caller_actor(), "scout")
        # 名册解析不到时回退 AIC 末两段（稳定目录名成分）
        self.assertEqual(pw._caller_actor(), "4SZM68.0LC5")

    def test_无分区路径被拒(self):
        pw.ws_create(self.root, self.TID)
        out = pw.ws_write(self.root, self.TID, "loose.txt", "x", actor="scout")
        self.assertEqual(out.get("error"), "zone-required")

    def test_repo只读(self):
        pw.ws_create(self.root, self.TID)
        out = pw.ws_write(self.root, self.TID, "repo/x.py", "x", actor="scout")
        self.assertEqual(out.get("error"), "zone-readonly")

    def test_products服务端版本化且不可覆盖(self):
        pw.ws_create(self.root, self.TID)
        a = pw.ws_write(self.root, self.TID, "products/report.json", '{"v":1}', actor="scout")
        b = pw.ws_write(self.root, self.TID, "products/report.json", '{"v":2}', actor="scout")
        self.assertTrue(a["ok"] and b["ok"], (a, b))
        self.assertIn("0001-report.json", a["versioned_as"])
        self.assertIn("0002-report.json", b["versioned_as"])
        # 调用方给的子目录被忽略（末段文件名才有效），防目录伪造
        c = pw.ws_write(self.root, self.TID, "products/fake/dir.json", "{}", actor="scout")
        self.assertIn("dir.json", c["versioned_as"])
        self.assertNotIn("fake", c["versioned_as"])
        lock = pw.ws_write(self.root, self.TID, "products/x.json", "{}",
                           actor="scout", if_match="")
        self.assertEqual(lock.get("error"), "products-append-only")

    def test_scratch领地_自动前缀与越界拒绝(self):
        pw.ws_create(self.root, self.TID)
        auto = pw.ws_write(self.root, self.TID, "scratch/笔记.md", "草稿", actor="scout")
        self.assertTrue(auto["ok"], auto)
        self.assertTrue(str(auto["path"]).replace("\\", "/").endswith("scratch/scout/笔记.md"))
        foreign = pw.ws_write(self.root, self.TID, "scratch/writer/越界.md", "x", actor="scout")
        # 归位而非拒绝：落到自己领地的同名子目录，原路径不产生任何文件
        self.assertTrue(foreign.get("ok"), foreign)
        self.assertTrue(str(foreign["path"]).replace(chr(92), "/").endswith(
            "scratch/scout/writer/越界.md"))
        self.assertFalse((self.root / self.TID / "scratch" / "writer").exists())
        bare = pw.ws_write(self.root, self.TID, "scratch", "x", actor="scout")
        self.assertEqual(bare.get("error"), "path-required")

    def test_shared乐观锁_conflict返回当前hash(self):
        pw.ws_create(self.root, self.TID)
        first = pw.ws_write(self.root, self.TID, "shared/design.md", "v1", actor="scout")
        writer_view = first["sha256"]          # writer 上次读到的版本
        # scout 在 writer 落笔前更新了同一文件 → writer 持旧 hash 写入必须冲突
        pw.ws_write(self.root, self.TID, "shared/design.md", "v2", actor="writer")
        stale = pw.ws_write(self.root, self.TID, "shared/design.md", "v3-by-writer",
                            actor="writer", if_match=writer_view)
        self.assertEqual(stale.get("error"), "conflict", stale)
        self.assertNotEqual(stale["current_hash"], writer_view)
        # writer 重新读到当前版本后写入成功
        read_back = pw.ws_read(self.root, self.TID, "shared/design.md")
        self.assertEqual(read_back["content"], "v2")
        good = pw.ws_write(self.root, self.TID, "shared/design.md", "v3-by-writer",
                           actor="writer", if_match=read_back["sha256"])
        self.assertTrue(good["ok"], good)

    def test_shared_absent语义_创建后冲突(self):
        pw.ws_create(self.root, self.TID)
        core = pw.ws_write(self.root, self.TID, "shared/new.md", "x",
                           actor="scout", if_match="")
        self.assertTrue(core["ok"])
        again = pw.ws_write(self.root, self.TID, "shared/new.md", "y",
                            actor="scout", if_match="")
        self.assertEqual(again.get("error"), "conflict")

    def test_manifest_jsonl记录create与write且hash对齐(self):
        pw.ws_create(self.root, self.TID)
        pw.ws_write(self.root, self.TID, "shared/a.md", "内容", actor="scout")
        lines = [json.loads(l) for l in
                 (self.root / self.TID / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        actions = [e["action"] for e in lines]
        self.assertEqual(actions, ["create", "write"])
        w = lines[-1]
        self.assertEqual(w["actor"], "scout")
        self.assertEqual(w["sha256"], hashlib.sha256("内容".encode()).hexdigest())
        self.assertEqual(w["prev_sha256"], "")

    def test_gc墓碑落在根级(self):
        pw.ws_create(self.root, self.TID)
        pw.ws_gc(self.root, self.TID, actor="supervisor")
        tomb = self.root / ".gc-log.jsonl"
        self.assertTrue(tomb.exists())
        rec = json.loads(tomb.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(rec["task"], self.TID)
        self.assertEqual(rec["actor"], "supervisor")


class ArchiveProductsTest(unittest.TestCase):
    """supervisor 镜像归档：多调用方各自版本化，二次归档追加序号。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ws-arch-")
        self.root = Path(self._tmp.name)
        self._env = os.environ.get("ACPS_WS_ROOT")
        os.environ["ACPS_WS_ROOT"] = self._tmp.name

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("ACPS_WS_ROOT", None)
        else:
            os.environ["ACPS_WS_ROOT"] = self._env
        self._tmp.cleanup()

    def test_多调用方归档_各自版本化_二次追加(self):
        entries = [
            ("scout", [{"name": "检索结果", "data_items": [{"type": "text", "text": "papers…"}]}]),
            ("writer", [{"name": "大纲", "data_items": [{"type": "text", "text": "outline…"}]}]),
        ]
        out = pw.archive_products(entries, "task-mirror", root=self.root)
        self.assertEqual(out["archived"], 2, out)
        base = self.root / "task-mirror" / "products"
        self.assertTrue((base / "scout" / "0001-检索结果.json").exists())
        self.assertTrue((base / "writer" / "0001-大纲.json").exists())
        out2 = pw.archive_products([("scout", [{"name": "检索结果", "data_items": []}])],
                                   "task-mirror", root=self.root)
        self.assertTrue((base / "scout" / "0002-检索结果.json").exists())
        self.assertEqual(out2["archived"], 1)
        # 台账里有全部四次写入
        manifest = (self.root / "task-mirror" / "manifest.jsonl").read_text(encoding="utf-8")
        self.assertEqual(len([l for l in manifest.splitlines() if l]), 4)  # create + 3 write



def partner_workspace_caps():
    return pw.WS_CAPABILITIES


def partner_workspace_agent():
    return pw.build_workspace_agent(None)


if __name__ == "__main__":
    unittest.main()
