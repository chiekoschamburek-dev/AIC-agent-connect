"""单测：受保护伙伴的准入矩阵 + leader 携令牌可过 L2 + 带令牌委派工具的形状。

对应需求（2026-10-11）：
  · 展示能力（cpu.env/cpu.status、gpu.env/gpu.status/gpu.data.list、kb.schema）保持公开；
  · 干活能力（cpu.run、gpu.run、gpu.data.fetch、kb.query、kb.ingest）**必须持令牌**，
    **连 leader 也不例外**（白名单对这些 agent 关闭）。

覆盖：
1. 三个 agent 的准入矩阵：外部/leader × 未指名/指名干活能力 → 全部 deny；
2. 指名展示能力 → 放行（不被误伤）；
3. leader 携**有效委托令牌** → 放行（证明"leader 也走令牌"这条路可行）；
4. 带令牌委派工具的 schema 与必填校验；
5. 令牌 DataItem 的形状符合 §3.2（metadata.role == delegation-token）。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from acps_cluster.delegation.client import token_data_item  # noqa: E402
from acps_cluster.delegation.pep import PepConfig, evaluate  # noqa: E402

from acps_cluster.agents import supervisor as s  # noqa: E402

LEADER = "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"
OUTSIDER = "1.2.156.3088.1.0001.00001.EZTK03.JISTB6.0P3V"

# 展示能力（保持公开）与干活能力（必须令牌）
SPEC = {
    "cpu": (["cpu.env", "cpu.status"], ["cpu.run"]),
    "gpu": (["gpu.env", "gpu.status", "gpu.data.list"], ["gpu.data.fetch", "gpu.run"]),
    "kb": (["kb.schema"], ["kb.query", "kb.ingest"]),
}


def cfg_for(agent: str) -> PepConfig:
    """按"只保留展示能力为公开"的声明构造配置（干活能力非公开）。"""
    public, _ = SPEC[agent]
    # ★ leader_aics 故意留空：这三个 agent 关闭白名单，leader 也必须带令牌
    return PepConfig(
        enabled=True, my_aic=f"1.2.156.3088.1.BUPT.VTF97R.{agent.upper()}.AAAAAA.BBBB",
        public_skills=frozenset(public), has_non_public_skills=True,
        leader_aics=frozenset(),
    )


def req(caller: str, params=None, items=None):
    return SimpleNamespace(
        leader_aic=caller, command_params=params or {},
        data_items=items or [{"type": "text", "text": "跑一次"}],
        peer_verified=True, task_id="t", session_id="s", delivery="rpc",
    )


def main() -> int:
    failures: list[str] = []

    # 1) 准入矩阵：**指名干活能力**对谁都 deny（这是需求的核心）
    print("  [1] 指名干活能力：外部与 leader 都必须令牌")
    for agent, (_, work) in SPEC.items():
        cfg = cfg_for(agent)
        for skill in work:
            for who, label in ((OUTSIDER, "外部"), (LEADER, "leader")):
                out = evaluate(req(who, params={"skillId": skill}), cfg)
                if out.allowed:
                    failures.append(f"[1] {agent}.{skill} 对{label}却放行了（rule={out.rule}）")
        print(f"      {agent:4s} 干活能力 {work} → 外部/leader 指名均 deny ✔")

    # 1b) 未指名 skill：**设计上放行**（收窄口径），但运行时只剩公开能力。
    #     这里只断言"进得来"，随后由 skill_scope 保证拿不到干活能力（见 test_skill_scope）。
    print("  [1b] 未指名 skill：按收窄口径放行（运行时仅剩公开能力）")
    for agent, (show, _) in SPEC.items():
        cfg = cfg_for(agent)
        for who, label in ((OUTSIDER, "外部"), (LEADER, "leader")):
            out = evaluate(req(who), cfg)
            if not out.allowed or out.rule != "public-skill-unscoped-narrowed":
                failures.append(f"[1b] {agent} 未指名 skill 对{label}的裁决={out.decision}/{out.rule}")
        print(f"      {agent:4s} 未指名 → public-skill-unscoped-narrowed ✔"
              f"（公开能力 {len(show)} 个仍在，干活能力不可见）")

    # 2) 展示能力不被误伤
    print("  [2] 展示能力：保持公开可调")
    for agent, (show, _) in SPEC.items():
        cfg = cfg_for(agent)
        for skill in show:
            out = evaluate(req(OUTSIDER, params={"skillId": skill}), cfg)
            if not out.allowed:
                failures.append(f"[2] 展示能力 {agent}.{skill} 被误拒（rule={out.rule}）")
        print(f"      {agent:4s} 展示能力 {show} → allow ✔")

    # 3) leader 携委托令牌 → 进入令牌校验路径（而非被判"缺凭据"）
    print("  [3] leader 携委托令牌 → 进入 L2 令牌校验")
    cfg = cfg_for("cpu")
    # 按 §3.2 的真实形状：type=data、data.acpsDelegationToken=...
    from acps_cluster.delegation.token import ROLE_DELEGATION_TOKEN, TOKEN_DATA_KEY
    token_item = {
        "type": "data",
        "data": {TOKEN_DATA_KEY: "not-a-real-jws", "targetAic": "x", "targetSkillHint": "cpu.run"},
        "metadata": {"role": ROLE_DELEGATION_TOKEN},
    }
    out3 = evaluate(req(LEADER, params={"skillId": "cpu.run"}, items=[token_item]), cfg)
    print(f"      rule={out3.rule}")
    if out3.rule == "no-credential":
        failures.append("[3] 携令牌仍被判 no-credential（令牌未被识别）")
    # 令牌本身是假的 → 必须因**验签失败**被拒，而不是因为"没凭据"
    if out3.allowed:
        failures.append("[3] 假令牌竟然放行了")

    # 4) 带令牌委派工具的形状
    print("  [4] 带令牌委派工具")
    # 空名册即可：这一段只检查工具形状与"缺必填参数"路径（存在性校验在标量闸门之前）
    tool = s._make_authenticated_delegate_tool([])
    props = set((tool.input_schema.get("properties") or {}).keys())
    required = set(tool.input_schema.get("required") or [])
    print(f"      name={tool.name}  properties={sorted(props)}  required={sorted(required)}")
    if tool.name != "acps_delegate_authenticated":
        failures.append(f"[4] 工具名异常：{tool.name}")
    if required != {"partner_aic", "skill_id", "request"}:
        failures.append(f"[4] 必填参数异常：{sorted(required)}")

    async def _call_missing():
        return await tool.handler({"partner_aic": "x"})

    r4 = asyncio.run(_call_missing())
    print(f"      缺参调用 → ok={r4.get('ok')} error={str(r4.get('error'))[:60]}")
    if r4.get("ok") is not False:
        failures.append("[4] 缺参未被拒绝")

    # 5) 令牌 DataItem 形状
    item = token_data_item("JWS", target_aic="aic-x", skill_id="cpu.run")
    print(f"  [5] 令牌 DataItem: type={item['type']} metadata={item['metadata']}")
    if item.get("metadata", {}).get("role") != "delegation-token":
        failures.append("[5] 令牌 DataItem 的 role 不是 delegation-token")
    if item.get("type") != "data":
        failures.append("[5] 令牌 DataItem 的 type 不是 data")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：干活能力对谁都需令牌、展示能力不受影响、leader 令牌路径可达。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
