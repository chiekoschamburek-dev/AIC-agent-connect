"""保真度实测（留档用）：中转模式下"材料经 leader 模型复述"是否失真。

对照对象：
- M_original：目标伙伴任务产物里的材料原文（协议层字节直达 leader runtime——不经过模型）；
- M_relayed：leader 最终回答里的对应章节（模型读入后复述）。
difflib 相似度 + 长度比 = 中转复述的保真度。隧道模式材料走协议层直达消费方
（不经任何模型转写），保真度构造性为 1.0，无需实测。

    cd my-project && python acps_cluster/delegation/probe_relay_fidelity.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import difflib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402

from wit import AcpsDirect, DataPart, TextPart, WitAgent, WitRuntime  # noqa: E402
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

from delegation.demo_fanout import (  # noqa: E402
    SUPERVISOR_WS, _acps_config, _aic_of, _await_products, _product_text,
)
from delegation.demo_fanout_real import CountingProvider, TOPICS, _target_agent  # noqa: E402

TARGET_WS = CLUSTER / "ws-scout"
PORT = 18491
LEADER_PORT = 18492
TOPIC = TOPICS["scout"]


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fidelity-"))
    out = CLUSTER / "demo_run" / "fanout" / "real"
    out.mkdir(parents=True, exist_ok=True)
    aic = _aic_of(TARGET_WS)
    leader_aic = _aic_of(SUPERVISOR_WS)

    counter = CountingProvider(agents.build_provider())
    audit = out / "fidelity-audit.jsonl"
    audit.write_text("", encoding="utf-8")
    ws_t = tmp / "ws-target"
    shutil.copytree(TARGET_WS / "acps", ws_t / "acps")
    target = WitRuntime(_target_agent("scout", aic, audit, counter), workspace=ws_t,
                        acps=_acps_config(PORT))
    status = await target.serve_direct_partner(host="127.0.0.1", port=PORT, amp=False)

    acs = target.acs_draft()
    acs["aic"] = aic
    for e in acs.get("endPoints", []) or []:
        e["url"] = f"https://localhost:{PORT}/rpc"

    ws_l = tmp / "ws-leader"
    shutil.copytree(SUPERVISOR_WS / "acps", ws_l / "acps")
    cache = ws_l / "acps" / "discovered"
    cache.mkdir(parents=True, exist_ok=True)
    cache_discovered_agent(cache_dir=cache, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs)
    leader_counter = CountingProvider(agents.build_provider())
    leader = WitRuntime(
        WitAgent(
            id="yanshu.fidelity-leader", display_name="fidelity-leader", version="1.0.0",
            description="保真度实测 leader",
            instructions=(
                "你是中转调度者。用 acps_delegate 向指定伙伴取材料，取回后在最终回答里"
                "专设一节【材料原文】，**逐字完整**复述材料全文，不得摘要、改写、增删。"
            ),
            model=leader_counter, capabilities=[], hooks=[AipDelegationHooks()],
            protocols=[AcpsDirect()],
        ),
        workspace=ws_l, acps=_acps_config(LEADER_PORT),
    )
    print(f"[fidelity] target {status.status} | model={__import__('os').environ.get('OPENAI_MODEL')}")

    try:
        from wit import AgentState

        state = AgentState()
        snapshot = await leader.aip_delegate(
            state, partner_aic=aic,
            request=[TextPart(f"请就主题生成材料：{TOPIC}"), DataPart(data={"skillId": "mat.fetch"})],
        )
        done = await _await_products(leader, state, str(snapshot.get("task_ref") or ""), timeout=360)
        original = _product_text(done.get("products") or [])
        # 中转模式的真实处境：材料已作为"上一腿的取回结果"进入 leader 上下文，
        # 测它逐字复述的转写保真度（与 demo_fanout_real 中转腿同构：材料在工具结果里，
        # 要求最终回答逐字保留）
        result = await leader.run(
            "你刚从伙伴取回一份材料（如下）。请在最终回答的【材料原文】一节"
            "**逐字完整**复述这份材料全文（不得摘要、改写、增删），然后另起一节用两句话说明用途。\n\n"
            + original
        )
        answer = str(result.final_text or "")
    finally:
        await target.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    (out / "relay-answer-raw.txt").write_text(answer, encoding="utf-8")
    (out / "relay-original-raw.txt").write_text(original, encoding="utf-8")
    m = re.search(r"材料原文(.*)", answer, re.S)  # 宽容标题形态（【】/##/裸文本）
    relayed = (m.group(1) if m else answer).strip()
    if m:
        cut = re.split(r"\n#+\s|【[^】]*】", relayed)[0].strip()
        relayed = cut or relayed

    ratio = difflib.SequenceMatcher(None, original, relayed).ratio()
    length_ratio = len(relayed) / max(1, len(original))
    record = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "topic": TOPIC,
        "original_chars": len(original),
        "relayed_chars": len(relayed),
        "length_ratio": round(length_ratio, 3),
        "similarity": round(ratio, 4),
        "leader_turns": leader_counter.calls,
        "verbatim_preserved": ratio > 0.98 and 0.95 < length_ratio < 1.05,
        "tunnel_reference": "隧道模式材料经协议层直达（不经模型转写），保真度=1.0（构造性）",
        "original_head": original[:120],
        "relayed_head": relayed[:120],
    }
    (out / "relay-fidelity.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
