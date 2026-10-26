"""烟雾：yanshu-eval 域闭环——双签章程 → 域内 grant → experiment 直调 eval.submit 真打分。

前置（2026-10-03）：ws-eval/acps/domains/ 已有双签章程（revision=1, active）；
eval ACS/名册/端口(8464)已对齐。本脚本验证一条完整链：
PEP 加载章程 → supervisor 签域 grant（subject=experiment）→ experiment 直连
（PEP L2 grant 分支：issuers/成员/角色×资产/配额）→ host 侧确定性打分返回。
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os

import agents  # noqa: E402
import partner_eval  # noqa: E402
from delegation import demo_pep as dp  # noqa: E402
from delegation import issue_grant, load_signing_context, token_data_item  # noqa: E402
from wit import AcpsDirect, AgentState, TextPart, WitAgent, WitRuntime  # noqa: E402
from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

PORT = 8464
OUT = CLUSTER / "demo_run" / "eval"
AUDIT = OUT / "audit-eval-smoke.jsonl"
SUPERVISOR_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"
EVAL_AIC = "1.2.156.3088.1.1.EVAL01.AGENT01.RUN01.0ZBR"
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
TRUST = CLUSTER / "ws-gpu" / "acps" / "trust-bundle.pem"
TERMINAL = {"completed", "failed", "canceled", "rejected", "awaiting-completion"}


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text("", encoding="utf-8")
    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = SUPERVISOR_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(AUDIT)

    provider = agents.build_provider()
    eval_rt = WitRuntime(
        partner_eval.build_eval_agent(provider), workspace=CLUSTER / "ws-eval", acps=dp._acps_config(PORT)
    )
    status = await eval_rt.serve_direct_partner(host="127.0.0.1", port=PORT, amp=False)
    print(f"[serve] ws-eval 端口 {PORT} → {status.status}（真实工作区，含 yanshu-eval 章程）", flush=True)
    try:
        grant = issue_grant(
            load_signing_context(CLUSTER / "ws-supervisor" / "acps" / "client", trust_bundle=TRUST),
            domain="yanshu-eval", domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": EVAL_AIC, "skill": "eval.submit"}],
            max_uses=2, ttl_seconds=1800,
        )
        print(f"[grant] jti={grant.jti[:8]}…（域 yanshu-eval, subject=experiment, max_uses=2）", flush=True)

        # 调用方 runtime（experiment 身份）+ 发现缓存指向本地 eval
        import shutil, tempfile
        tmp = Path(tempfile.mkdtemp(prefix="eval-smoke-"))
        root = tmp / "ws-caller"
        shutil.copytree(CLUSTER / "ws-experiment" / "acps", root / "acps")
        cache_dir = root / "acps" / "discovered"
        cache_dir.mkdir(parents=True, exist_ok=True)
        acs = json.loads((CLUSTER / "ws-eval" / "acps" / "acs.final.json").read_text(encoding="utf-8"))
        cache_discovered_agent(cache_dir=cache_dir, summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs)
        agent = WitAgent(
            id="yanshu.eval-smoke-caller", display_name="研枢-Eval-Smoke-Caller", version="1.0.0",
            description="烟雾调用方（experiment 身份）", instructions="烟雾专用；只走 aip_delegate。",
            model=None, capabilities=[], hooks=[AipDelegationHooks()], protocols=[AcpsDirect()],
        )
        caller = WitRuntime(agent, workspace=root, acps=dp._acps_config(18499))
        state = AgentState()

        payload = token_data_item(grant.token, target_aic=EVAL_AIC, skill_id="eval.submit")
        result = await caller.aip_delegate(
            state, partner_aic=EVAL_AIC,
            request=[
                TextPart(
                    "请调用 eval.submit 提交评测：mode=predictions, benchmark=demo-cls, "
                    "submitter=experiment, predictions=[1,1,0,0,1,1,0,1,1,0]，把返回的 JSON 原样输出。"
                ),
                __import__("wit").DataPart(data=dict(payload["data"]), metadata=dict(payload["metadata"])),
            ],
        )
        t0 = time.time()
        for _ in range(36):
            if result.get("state") in TERMINAL or not result.get("task_ref"):
                break
            await asyncio.sleep(5)
            result = await caller.aip_task(state, action="wait", task_ref=result.get("task_ref"))
        if result.get("state") == "awaiting-completion" and result.get("task_ref"):
            result = await caller.aip_task(state, action="complete", task_ref=result.get("task_ref"))
        text = ""
        for product in result.get("products") or []:
            for item in product.get("data_items") or product.get("dataItems") or []:
                text += (item.get("text") or "") + "\n"
        print(f"\n[state] {result.get('state')} 用时 {time.time() - t0:.0f}s", flush=True)
        print(text[:2000], flush=True)
        (OUT / "smoke-result.json").write_text(
            json.dumps({"state": result.get("state"), "products": text}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        await eval_rt.stop_serving()
        await provider.aclose()
        print("\n[audit]")
        for line in AUDIT.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                print(f"  {r.get('decision'):5s} {r.get('rule',''):18s} jti={(r.get('jti') or '-')[:8]}")


if __name__ == "__main__":
    asyncio.run(main())
