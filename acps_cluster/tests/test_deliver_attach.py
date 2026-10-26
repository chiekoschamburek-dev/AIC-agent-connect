"""单测：交付文件（AIP `FileDataItem`）——**附加**与**归档**两段时序。

为什么测这些：
- 这是"把成品作为**文件**交付给调用方"的落地路径（协议 file 分支，uri/bytes 二选一）；
- 载荷必须符合 AIP 契约（`DATA_ITEM_JSON_SCHEMA`），否则平台侧校验/解析会失败——
  用 wit 自带的 schema 直接校验，**契约由协议本体兜底**，不靠人记字段名；
- **时序是踩过坑的**（2026-10-13 实测）：先归档再提交，那次提交被协议拒绝
  （任务处于 awaiting-input），文件已进 `sent/` 却从未交付成功。所以拆成两段：
  `_attach_deliver_files()` 只读不搬；`_archive_delivered()` 只在**提交成功后**调用。

覆盖：
1. 开关关闭 → 不附、不动文件（行为与引入前一致）
2. 开关打开 → 文件被附上（bytes 可解回原文）、载荷通过 AIP schema 校验，**且文件仍在原地**
3. `_archive_delivered` 在"提交成功后"调用 → 移入 sent/；再次 attach 不重复交付
4. 超限文件跳过（不附、不移动）
5. 无产物 → 返回空且不吞文件
6. `data_items` / `dataItems` 两种键名都能正确追加
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
for _p in (str(CL.parent), str(CL)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.agents import supervisor as s  # noqa: E402

_SWITCH = "ACPS_SUPERVISOR_ATTACH_FILES"


def _quiet(*_a, **_k):
    return None


def _products(key: str = "dataItems") -> list:
    return [{"id": "p1", "name": "n", key: [{"type": "text", "text": "终稿"}]}]


class AttachOffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deliver-off-"))
        (self.tmp / "a.md").write_text("hello", encoding="utf-8")
        os.environ.pop(_SWITCH, None)

    def test_默认关闭不附不动(self):
        products = _products()
        self.assertEqual(s._attach_deliver_files(products, deliver=self.tmp, logger=_quiet), [])
        self.assertEqual(len(products[0]["dataItems"]), 1)
        self.assertTrue((self.tmp / "a.md").is_file(), "关闭时不应移动文件")


class AttachOnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deliver-on-"))
        self.body = "# 综述\n\n正文正文".encode("utf-8")
        (self.tmp / "survey.md").write_bytes(self.body)
        (self.tmp / "big.bin").write_bytes(b"x" * (300 * 1024))  # 超默认 256KB
        os.environ[_SWITCH] = "1"

    def tearDown(self):
        os.environ.pop(_SWITCH, None)

    def test_附上且载荷合规但不动文件(self):
        products = _products()
        attached = s._attach_deliver_files(products, deliver=self.tmp, logger=_quiet)
        self.assertEqual([p.name for p in attached], ["survey.md"], "big.bin 应超限被跳过")
        items = products[0]["dataItems"]
        self.assertEqual(len(items), 2, "原有 text 产物必须保留")
        f = items[-1]
        self.assertEqual(f["type"], "file")
        self.assertEqual(f["name"], "survey.md")
        self.assertEqual(f["mimeType"], "text/markdown")
        self.assertEqual(base64.b64decode(f["bytes"]), self.body)
        # 关键时序：附加阶段**不搬文件**（提交失败时不能白白消费）
        self.assertTrue((self.tmp / "survey.md").is_file())
        self.assertFalse((self.tmp / "sent").exists())
        # 契约校验：用 wit 自带的 AIP schema 验（file 分支要求 uri/bytes 二选一）
        try:
            import jsonschema
            from wit.acps.aip import DATA_ITEM_JSON_SCHEMA
        except ImportError as exc:  # pragma: no cover
            self.skipTest(f"缺 jsonschema/wit schema: {exc}")
        jsonschema.validate(f, DATA_ITEM_JSON_SCHEMA)

    def test_提交成功后归档且不重复交付(self):
        attached = s._attach_deliver_files(_products(), deliver=self.tmp, logger=_quiet)
        moved = s._archive_delivered(attached, deliver=self.tmp, logger=_quiet)
        self.assertEqual(moved, 1)
        self.assertTrue((self.tmp / "sent" / "survey.md").is_file())
        self.assertFalse((self.tmp / "survey.md").exists())
        # 二次 attach → 没有新文件可交付
        again = s._attach_deliver_files(_products(), deliver=self.tmp, logger=_quiet)
        self.assertEqual(again, [])

    def test_归档空列表是安全的(self):
        self.assertEqual(s._archive_delivered([], deliver=self.tmp, logger=_quiet), 0)

    def test_data_items键名同样支持(self):
        products = _products(key="data_items")
        self.assertEqual(len(s._attach_deliver_files(products, deliver=self.tmp,
                                                     logger=_quiet)), 1)
        self.assertEqual(products[0]["data_items"][-1]["type"], "file")

    def test_无产物不吞文件(self):
        self.assertEqual(s._attach_deliver_files([], deliver=self.tmp, logger=_quiet), [])
        self.assertTrue((self.tmp / "survey.md").is_file())

    def test_目录不存在返回空(self):
        n = s._attach_deliver_files(_products(), deliver=self.tmp / "nope", logger=_quiet)
        self.assertEqual(n, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
