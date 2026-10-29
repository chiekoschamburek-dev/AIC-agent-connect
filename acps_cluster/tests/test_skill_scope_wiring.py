"""P0-2 回归：skill-scope 必须进入 run 生命周期 hooks，而不是 aip_hooks。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

CLUSTER_DIR = Path(__file__).resolve().parents[1]
for _p in (str(CLUSTER_DIR.parent), str(CLUSTER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from acps_cluster.agents import partner_scout  # noqa: E402
from wit import AcpsConfig, AgentProviderProfile  # noqa: E402
from wit.kernel.capabilities import ToolManager  # noqa: E402

# 交付口径已删除仓库内的 ACS 声明稿（acs.*.filled.json）；这里用内联 provider
# 让 wit 从 agent 能力现算 ACS，避免测试依赖已移除的数据文件。
PROVIDER = AgentProviderProfile(
    name="yanshu-test", organization="yanshu", email="test@example.com", country_code="CN",
)


class SkillScopeWiringTest(unittest.TestCase):
    def test_skill_scope在compiled_hooks而非aip_hooks(self):
        agent = partner_scout.build_scout_agent(None)
        # 不再依赖仓库内的 ACS 声明稿（按交付口径已删除）；compile 只需要协议地址。
        cfg = AcpsConfig.custom(
            registry_url="https://localhost:9001",
            ca_url="https://localhost:9003",
            discovery_url="https://localhost:9005",
            public_endpoint="https://localhost:8460/rpc",
            provider=PROVIDER,
        )
        compiled = agent.compile(acps=cfg)
        run_ids = [getattr(hook, "id", type(hook).__name__) for hook in compiled.hooks]
        aip_ids = [getattr(hook, "id", type(hook).__name__) for hook in compiled.aip_hooks]
        self.assertIn("yanshu.skill-scope", run_ids)
        self.assertNotIn("yanshu.skill-scope", aip_ids)

    def test_run_start真的移除未准入的非公开工具(self):
        agent = partner_scout.build_scout_agent(None)
        cfg = AcpsConfig.custom(
            registry_url="https://localhost:9001",
            ca_url="https://localhost:9003",
            discovery_url="https://localhost:9005",
            public_endpoint="https://localhost:8460/rpc",
            provider=PROVIDER,
        )
        compiled = agent.compile(acps=cfg)
        hook = next(h for h in compiled.hooks if getattr(h, "id", "") == "yanshu.skill-scope")
        manager = ToolManager(compiled.tools)
        asyncio.run(hook.on_run_start(SimpleNamespace(
            context=SimpleNamespace(tool_manager=manager, config=None))))
        remaining = set(manager.lookup())
        self.assertIn("arxiv_search", remaining)
        self.assertNotIn("scholar_deep_fetch", remaining)
        self.assertNotIn("dataset_fetch", remaining)


if __name__ == "__main__":
    unittest.main()
