"""离线单测：discovery 过滤补丁（`import cluster_common` 即生效）。

背景：wit 2.1.0 的 `discovery_filter()` 在 `include_unavailable=True`（默认值）时会构造
平台已废弃的 `onlyAvailable` 条件 → wt.ioa.pub 返回 400 → supervisor 发现不了任何 agent。
`cluster_common` 导入时把 `discovery_filter` 改为恒返回 None（详见该模块内的注释块）。

    python -m unittest acps_cluster.tests.test_discovery_patch -v
"""

import sys
import unittest
from pathlib import Path

CLUSTER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = CLUSTER_DIR.parent
for _p in (str(PROJECT_DIR), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wit.acps.discovery as discovery  # noqa: E402

import cluster_common  # noqa: E402,F401  （导入即打补丁，顺序不能调）


class DiscoveryFilterPatchTest(unittest.TestCase):
    def test_默认参数不再产生过滤条件(self):
        # 这就是 supervisor 失败的那条路径：模型不传 include_unavailable 时走默认值 True
        self.assertIsNone(discovery.discovery_filter())

    def test_显式传值也不再产生过滤条件(self):
        self.assertIsNone(discovery.discovery_filter(include_unavailable=True))
        self.assertIsNone(discovery.discovery_filter(include_unavailable=False))

    def test_调用方自带filter也一并丢弃(self):
        # 平台对任何非空 filter 对象都 400（实测连 {"conditions":[]} 也拒），故整体不带
        self.assertIsNone(
            discovery.discovery_filter(include_unavailable=True, filter={"conditions": []})
        )

    def test_补丁可幂等重放(self):
        # 多个模块都会 import cluster_common；重复打补丁不应改变行为
        current = discovery.discovery_filter
        cluster_common._patch_discovery_filter()
        self.assertIs(discovery.discovery_filter, current)


if __name__ == "__main__":
    unittest.main(verbosity=2)
