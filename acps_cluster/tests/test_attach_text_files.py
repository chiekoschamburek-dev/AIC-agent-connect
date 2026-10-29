"""长文本产物自动附 .md 文件（AIP FileDataItem）。"""

from __future__ import annotations

import base64
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

from wit.acps import aip  # noqa: E402

from acps_cluster.agents import supervisor  # noqa: E402


def _product(text: str, name: str = "报告") -> dict:
    return {"id": "final", "name": name,
            "data_items": [{"type": "text", "text": text}]}


class AttachTextFilesTest(unittest.TestCase):
    def test_默认关闭不改产物(self):
        products = [_product("x" * 3000)]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACPS_SUPERVISOR_ATTACH_TEXT_FILES", None)
            n = supervisor._attach_text_files(products)
        self.assertEqual(n, 0)
        self.assertEqual(len(products[0]["data_items"]), 1)

    def test_长文本附md且校验通过(self):
        products = [_product("正文" * 1000)]
        with mock.patch.dict(os.environ, {"ACPS_SUPERVISOR_ATTACH_TEXT_FILES": "1"}):
            n = supervisor._attach_text_files(products)
        self.assertEqual(n, 1)
        items = products[0]["data_items"]
        self.assertEqual(items[0]["type"], "text")
        file_item = items[-1]
        self.assertEqual(file_item["type"], "file")
        self.assertEqual(file_item["name"], "报告.md")
        self.assertEqual(file_item["mime_type"], "text/markdown; charset=utf-8")
        self.assertEqual(base64.b64decode(file_item["bytes"]).decode("utf-8"),
                         items[0]["text"])
        validated = aip.validate_products([products[0]])
        self.assertEqual(validated[0]["dataItems"][-1]["type"], "file")

    def test_uri模式落盘并给下载链接(self):
        import tempfile

        products = [_product("正文" * 1000)]
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
            "ACPS_SUPERVISOR_ATTACH_TEXT_FILES": "1",
            "ACPS_SUPERVISOR_ATTACH_TEXT_MODE": "uri",
            "ACPS_SUPERVISOR_FILE_DIR": td,
            "ACPS_SUPERVISOR_FILE_BASE_URL": "http://example.test/files",
        }):
            n = supervisor._attach_text_files(products)
            files = list(Path(td).glob("*.md"))
            self.assertEqual(len(files), 1)
            written = files[0].read_text(encoding="utf-8")
        self.assertEqual(n, 1)
        self.assertEqual(written, products[0]["data_items"][0]["text"].split("\n\n📎")[0])
        file_item = next(d for d in products[0]["data_items"] if d["type"] == "file")
        self.assertTrue(file_item["uri"].startswith("http://example.test/files/"))
        self.assertIn("📎 文件下载：http://example.test/files/",
                      products[0]["data_items"][0]["text"])

    def test_短文本不附(self):
        products = [_product("短")]
        with mock.patch.dict(os.environ, {"ACPS_SUPERVISOR_ATTACH_TEXT_FILES": "1"}):
            n = supervisor._attach_text_files(products)
        self.assertEqual(n, 0)

    def test_幂等不重复附(self):
        products = [_product("正文" * 1000)]
        with mock.patch.dict(os.environ, {"ACPS_SUPERVISOR_ATTACH_TEXT_FILES": "1"}):
            self.assertEqual(supervisor._attach_text_files(products), 1)
            self.assertEqual(supervisor._attach_text_files(products), 0)
        self.assertEqual(sum(1 for d in products[0]["data_items"] if d["type"] == "file"), 1)

    def test_dataItems驼峰也支持(self):
        product = {"id": "final", "name": "R",
                   "dataItems": [{"type": "text", "text": "正文" * 1000}]}
        with mock.patch.dict(os.environ, {"ACPS_SUPERVISOR_ATTACH_TEXT_FILES": "1"}):
            n = supervisor._attach_text_files([product])
        self.assertEqual(n, 1)
        self.assertEqual(sum(1 for d in product["dataItems"] if d["type"] == "file"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
