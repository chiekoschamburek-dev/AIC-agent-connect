"""gpu-partner 离线单测：AIC 校验码（规范示例对拍）、沙盒输入校验、名册接线。

Docker/GPU 相关的沙盒真实执行需要 Docker Desktop，不在离线单测覆盖范围内，
本文件全部离线可跑：

    python -m unittest acps_cluster.tests.test_gpu_partner -v
"""

import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.runtime import gpu_runner  # noqa: E402
from deploy.mint_gpu_identity import aic_checksum, base36_4, crc16_ccitt_false  # noqa: E402

# dev 工作区（ws-*）是本机运行时状态，按仓库禁入清单不入库；干净克隆没有它们。
# 依赖这些文件的两组用例显式跳过，而不是以 FileNotFoundError / 断言失败收场。
GPU_ACS = CLUSTER_DIR / "ws-gpu" / "acps" / "acs.final.json"
GPU_ENROLLMENT = CLUSTER_DIR / "ws-gpu" / "acps" / "enrollment.json"
NO_GPU_DEV = "ws-gpu 未铸造（dev 工作区不入库）；干净克隆跳过"


class AicChecksumTest(unittest.TestCase):
    """AIC 校验码实现必须通过规范 §4.1 的示例对拍。"""

    def test_规范示例_CRC原值(self):
        # §4.1(6)：1.2.156.3088.1.1.34C2.478BDF.3GF546 + SALT 0x1234 → 0x646C
        data = b"1.2.156.3088.1.1.34C2.478BDF.3GF546" + b"\x12\x34"
        self.assertEqual(crc16_ccitt_false(data), 0x646C)

    def test_规范示例_Base36(self):
        # §4.1(7)：0x646C → 0JU4
        self.assertEqual(base36_4(0x646C), "0JU4")
        self.assertEqual(aic_checksum("1.2.156.3088.1.1.34C2.478BDF.3GF546"), "0JU4")

    def test_小写输入规范化为大写(self):
        # §4.2(2)：校验时数据码统一大写
        self.assertEqual(
            aic_checksum("1.2.156.3088.1.1.34c2.478bdf.3gf546"),
            aic_checksum("1.2.156.3088.1.1.34C2.478BDF.3GF546"),
        )

    def test_铸造出的gpu_AIC可自校验(self):
        import json

        enrollment = json.loads(
            GPU_ENROLLMENT.read_text(encoding="utf-8")) if GPU_ENROLLMENT.is_file() else {}
        aic = str(((enrollment.get("identity") or {}).get("ref") or {}).get("aic") or "")
        if not aic:  # 未铸造身份的环境（如 CI / 干净克隆）跳过
            self.skipTest(NO_GPU_DEV)
        if not aic.startswith("1.2.156.3088.1.1."):
            # 2026-10-29 起 ws-gpu 切到平台注册身份（…BUPT.VTF97R.…），
            # 平台 AIC 的校验位规则与本地 dev CA 不同，这个用例只覆盖本地铸造格式。
            self.skipTest(f"平台身份不适用本地 AIC 校验规则: {aic[:40]}")
        data_code, checksum = aic.rsplit(".", 1)
        self.assertEqual(aic_checksum(data_code), checksum)
        self.assertRegex(aic, r"^1\.2\.156\.3088\.1\.1\.[0-9A-Z]+\.[0-9A-Z]+\.[0-9A-Z]+\.[0-9A-Z]{4}$")


class ValidateJobTest(unittest.TestCase):
    """沙盒八条硬边界的输入侧校验（模型提议、代码裁决）。"""

    def _ok(self, raw):
        spec, error = gpu_runner.validate_job(raw)
        self.assertIsNone(error, error)
        return spec

    def test_合法任务通过且缺省值正确(self):
        spec = self._ok({"files": {"main.py": "print(1)"}})
        self.assertEqual(spec.image, gpu_runner.DEFAULT_IMAGE)
        self.assertEqual(spec.entrypoint, ("python", "/workspace/input/main.py"))
        self.assertEqual(spec.timeout_seconds, gpu_runner.DEFAULT_TIMEOUT_SECONDS)

    def test_镜像不在白名单被拒(self):
        _, error = gpu_runner.validate_job({"files": {"main.py": "x"}, "image": "ubuntu:latest"})
        self.assertIn("白名单", error)

    def test_非python入口被拒(self):
        _, error = gpu_runner.validate_job({"files": {"main.py": "x"}, "entrypoint": ["sh", "-c", "ls"]})
        self.assertIn("可执行文件", error)

    def test_shell元字符被拒(self):
        _, error = gpu_runner.validate_job(
            {"files": {"main.py": "x"}, "entrypoint": ["python", "a.py; rm -rf /"]}
        )
        self.assertIn("元字符", error)

    def test_路径穿越文件名被拒(self):
        for bad in ("../escape.py", "a/b.py", "a\\b.py", ".hidden", ".."):
            _, error = gpu_runner.validate_job({"files": {bad: "x"}})
            self.assertIsNotNone(error, f"文件名 {bad!r} 应被拒绝")

    def test_裸文件名入口被补全到只读挂载点(self):
        # 2026-10-18 实测：模型给 ["python3","main.py"]，容器 CWD 是可写的 output/，
        # 于是 5 次执行全部 exit=2（can't open file '/workspace/output/main.py'）
        spec = self._ok({"files": {"main.py": "x"}, "entrypoint": ["python3", "main.py"]})
        self.assertEqual(spec.entrypoint, ("python3", "/workspace/input/main.py"))

    def test_入口带flags时只改写py参数(self):
        spec = self._ok({"files": {"main.py": "x"}, "entrypoint": ["python", "-u", "main.py"]})
        self.assertEqual(spec.entrypoint, ("python", "-u", "/workspace/input/main.py"))

    def test_指向output的绝对路径入口被拒(self):
        _, error = gpu_runner.validate_job(
            {"files": {"main.py": "x"}, "entrypoint": ["python", "/workspace/output/main.py"]}
        )
        self.assertIsNotNone(error)
        self.assertIn("/workspace/input/", error)

    def test_入口相对路径不能逃逸挂载点(self):
        spec = self._ok({"files": {"main.py": "x"}, "entrypoint": ["python", "./main.py"]})
        self.assertEqual(spec.entrypoint, ("python", "/workspace/input/main.py"))

    def test_缺py入口被拒(self):
        _, error = gpu_runner.validate_job({"files": {"data.txt": "x"}})
        self.assertIn(".py", error)

    def test_单文件超限被拒(self):
        big = "a" * (gpu_runner.MAX_FILE_BYTES + 1)
        _, error = gpu_runner.validate_job({"files": {"main.py": big}})
        self.assertIn("单文件上限", error)

    def test_总输入超限被拒(self):
        files = {f"m{i}.py": "x" * (gpu_runner.MAX_FILE_BYTES) for i in range(6)}
        files["main.py"] = "print(1)"
        _, error = gpu_runner.validate_job({"files": files})
        self.assertIn("总大小", error)

    def test_超时被钳制到上限(self):
        spec = self._ok({"files": {"main.py": "x"}, "timeout_seconds": 99999})
        self.assertEqual(spec.timeout_seconds, gpu_runner.MAX_TIMEOUT_SECONDS)
        spec = self._ok({"files": {"main.py": "x"}, "timeout_seconds": 1})
        self.assertEqual(spec.timeout_seconds, 30)


class GpuWiringTest(unittest.TestCase):
    """gpu-partner 与集群的接线：名册条目 + PEP 语义（gpu.run 非公开、其余公开）。"""

    def test_名册含gpu成员_路线B启用(self):
        """2026-10-02 路线 B：gpu 用本地 dev 身份 + 反向隧道参与，名册已登记。

        服务器侧不读隧道，只靠 `ws-gpu/acps/{acs.final,enrollment}.json` 寻址桩解析身份、
        靠预热发现缓存解析端点。这里只断言"名册认识它、能力齐"。
        """
        from acps_cluster.core import cluster_roster

        if not GPU_ACS.is_file():
            # 名册的 gpu 条目能力来自 ws-gpu 的 ACS 投影；干净克隆没有 dev 工作区，
            # 能力列表会退化为声明子集，此时该断言没有意义。
            self.skipTest(NO_GPU_DEV)
        roster = cluster_roster.load_roster()
        gpu = [m for m in roster if m.key == "gpu"]
        self.assertEqual(len(gpu), 1)
        self.assertEqual(gpu[0].module, "partner_gpu")
        for cap in ("gpu.env", "gpu.status", "gpu.data.list", "gpu.data.fetch", "gpu.run"):
            self.assertIn(cap, gpu[0].capabilities)
        # 能力声明本身不依赖名册条目
        from acps_cluster.agents import partner_gpu

        self.assertEqual(
            tuple(getattr(c, "capability", c).id for c in partner_gpu.GPU_CAPABILITIES),
            ("gpu.env", "gpu.status", "gpu.data.list", "gpu.data.fetch", "gpu.run"),
        )

    def test_gpu能力可见性_公开三非公开二(self):
        from acps_cluster.agents import partner_gpu
        from acps_cluster.delegation.pep import capability_visibility

        public, has_non_public = capability_visibility(partner_gpu.GPU_CAPABILITIES)
        self.assertEqual(public, frozenset({"gpu.env", "gpu.status", "gpu.data.list"}))
        # gpu.run / gpu.data.fetch 非公开 ⇒ 未指名 skill 的请求 fail-closed
        self.assertTrue(has_non_public)

    def test_docker_args包含全部沙盒硬边界(self):
        from pathlib import Path as P

        spec, _ = gpu_runner.validate_job({"files": {"main.py": "x"}})
        args = gpu_runner.build_docker_args(spec, "test", P("/in"), P("/out"))
        joined = " ".join(args)
        for guard in ("--gpus device=0", "--network none", "--memory 7g", "--cpus 4",
                      "--cap-drop ALL", "no-new-privileges", "--read-only",
                      "--pids-limit 256", "--user 1000:1000"):
            self.assertIn(guard, joined, f"沙盒缺硬边界: {guard}")
        self.assertIn(":/workspace/input:ro", joined)
        self.assertIn(":/workspace/output:rw", joined)


class DatasetFetchTest(unittest.TestCase):
    """gpu.data.fetch 的输入校验与安全解压（回环 http 服务器做真实下载，不起 Docker）。"""

    CLEANUP_PREFIX = "unittest_"

    def setUp(self):
        # 回环下载只用于测试/本地演示：生产默认拒绝（2026-10-21 SSRF 收口）。
        import os

        self._old_loopback = os.environ.get("ACPS_GPU_DATA_ALLOW_LOOPBACK")
        os.environ["ACPS_GPU_DATA_ALLOW_LOOPBACK"] = "1"

    def tearDown(self):
        import os
        import shutil

        for entry in gpu_runner.DATASETS_DIR.glob(f"{self.CLEANUP_PREFIX}*"):
            shutil.rmtree(entry, ignore_errors=True)
        if self._old_loopback is None:
            os.environ.pop("ACPS_GPU_DATA_ALLOW_LOOPBACK", None)
        else:
            os.environ["ACPS_GPU_DATA_ALLOW_LOOPBACK"] = self._old_loopback

    def test_数据集名校验(self):
        for bad in ("../escape", "a/b", "a b", "", "x" * 65, ".hidden"):
            error = gpu_runner.validate_fetch(bad, "https://example.com/d.zip")
            self.assertIsNotNone(error, f"数据集名 {bad!r} 应被拒绝")

    def test_URL策略(self):
        # https 放行
        self.assertIsNone(gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "a", "https://example.com/d.zip"))
        # 非回环 http 拒绝；回环 http 放行（本地演示/自托管数据源）
        self.assertIsNotNone(gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "b", "http://example.com/d"))
        self.assertIsNone(gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "c", "http://127.0.0.1:8000/d"))
        self.assertIsNone(gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "d", "http://localhost:8000/d"))
        # 其他 scheme 拒绝
        for scheme in ("file:///etc/passwd", "ftp://x/y", "gopher://x"):
            self.assertIsNotNone(gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "e", scheme))

    def test_URL策略_默认拒绝内网回环与云元数据(self):
        """SSRF 收口：不带显式放行开关时，任何内网/回环/链路本地地址都必须拒。"""
        import os

        os.environ.pop("ACPS_GPU_DATA_ALLOW_LOOPBACK", None)
        try:
            cases = {
                "unittest_ssrf1": "https://127.0.0.1/d.zip",
                "unittest_ssrf2": "https://10.0.0.5/d.zip",
                "unittest_ssrf3": "https://192.168.1.10/d.zip",
                "unittest_ssrf4": "https://169.254.169.254/latest/meta-data",
                "unittest_ssrf5": "http://127.0.0.1:8000/d",
            }
            for name, url in cases.items():
                self.assertIsNotNone(
                    gpu_runner.validate_fetch(name, url), f"{url} 默认应被拒绝"
                )
        finally:
            os.environ["ACPS_GPU_DATA_ALLOW_LOOPBACK"] = "1"

    def test_域名白名单(self):
        import os

        old = os.environ.get("ACPS_GPU_DATA_DOMAINS")
        try:
            os.environ["ACPS_GPU_DATA_DOMAINS"] = "mirrors.example.com"
            self.assertIsNone(
                gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "f", "https://mirrors.example.com/d.zip")
            )
            error = gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "g", "https://elsewhere.com/d.zip")
            self.assertIn("白名单", error)
        finally:
            if old is None:
                os.environ.pop("ACPS_GPU_DATA_DOMAINS", None)
            else:
                os.environ["ACPS_GPU_DATA_DOMAINS"] = old

    def test_sha256格式校验(self):
        error = gpu_runner.validate_fetch(self.CLEANUP_PREFIX + "h", "https://example.com/d", "not-hex")
        self.assertIsNotNone(error)

    def test_zip路径穿越被拒(self):
        import zipfile
        from pathlib import Path as P
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            blob = P(td) / "evil.zip"
            with zipfile.ZipFile(blob, "w") as zf:
                zf.writestr("../../escape.txt", "pwned")
                zf.writestr("/abs/path.txt", "pwned")
            with self.assertRaises(ValueError):
                gpu_runner._safe_unzip(blob, P(td) / "out")

    def test_回环真实下载_解压与sha256校验(self):
        import hashlib
        import json
        import threading
        import zipfile
        from functools import partial
        from http.server import HTTPServer, SimpleHTTPRequestHandler
        from pathlib import Path as P
        import asyncio
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            serving = P(td)
            # 一个 zip 数据集（正常成员）+ 一个散文件
            with zipfile.ZipFile(serving / "ds.zip", "w") as zf:
                zf.writestr("train.csv", "a,b\n1,2\n")
                zf.writestr("meta.json", json.dumps({"rows": 1}))
            (serving / "raw.csv").write_text("x\n", encoding="utf-8")
            handler = partial(SimpleHTTPRequestHandler, directory=str(serving))
            handler.log_message = lambda *a, **k: None
            server = HTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                # ① zip：下载 + 安全解压
                result = asyncio.run(gpu_runner.fetch_dataset(self.CLEANUP_PREFIX + "zip", f"{base}/ds.zip"))
                self.assertTrue(result["ok"], result.get("error"))
                target = gpu_runner.DATASETS_DIR / f"{self.CLEANUP_PREFIX}zip"
                self.assertEqual(sorted(p.name for p in target.iterdir()), ["meta.json", "train.csv"])
                # ② 散文件
                result = asyncio.run(gpu_runner.fetch_dataset(self.CLEANUP_PREFIX + "raw", f"{base}/raw.csv"))
                self.assertTrue(result["ok"], result.get("error"))
                self.assertTrue((gpu_runner.DATASETS_DIR / f"{self.CLEANUP_PREFIX}raw" / "raw.csv").exists())
                # ③ sha256 校验：正确值通过，错误值拒绝
                good = hashlib.sha256((serving / "raw.csv").read_bytes()).hexdigest()
                result = asyncio.run(
                    gpu_runner.fetch_dataset(self.CLEANUP_PREFIX + "ok", f"{base}/raw.csv", sha256=good)
                )
                self.assertTrue(result["ok"])
                bad = "0" * 64
                result = asyncio.run(
                    gpu_runner.fetch_dataset(self.CLEANUP_PREFIX + "bad", f"{base}/raw.csv", sha256=bad)
                )
                self.assertFalse(result["ok"])
                self.assertIn("sha256", result["error"])
                # ④ 重名拒绝
                result = asyncio.run(gpu_runner.fetch_dataset(self.CLEANUP_PREFIX + "zip", f"{base}/ds.zip"))
                self.assertFalse(result["ok"])
            finally:
                server.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
