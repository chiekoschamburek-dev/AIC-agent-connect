"""真实业务跑通：受托方持委托 token 直连 scout，scout 用**真实 LLM** 完成非公开 skill 检索。

与 demo_pep.py 的区别：这里不打桩。
- scout 挂真实 provider（.env 的 MiniMax），被授权后真的执行 `scholar.deep_fetch`
  （OpenAlex 真实网络检索），把文献清单作为 Product 返回；
- 调用方以 critic 身份持 supervisor 签发的 token，经 `runtime.aip_delegate` 走真实 mTLS 直连；
- 全程不启 registry/CA：靠 enrollment 本地状态 + 手工 seed 的 discovery 缓存（见 PEP-result.md §4.4）。

    cd my-project && python acps_cluster/delegation/real_business.py "查询词"
"""

import os as _os
import sys as _sys

# 见 demo_pep.py 顶部说明：脚本直跑时本目录会被塞进 sys.path[0]，令包内 token.py 遮蔽标准库 token。
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# demo_pep 在导入时会把自己的目录从 sys.path 摘掉（避免 delegation/token.py 遮蔽标准库 token）；
# 先导入它，再借用它的常量与工具函数。
from delegation import demo_pep as dp  # noqa: E402  isort:skip
from delegation import direct_fetch, issue_token, load_signing_context  # noqa: E402

import agents  # noqa: E402
import partner_scout  # noqa: E402
from wit import WitRuntime  # noqa: E402

PORT = 18450
TASK = "调研 GNN（图神经网络）在知识图谱补全中的最新进展，返回 10 篇文献，每条含标题、年份、链接与摘要要点"
TERMINAL = {"completed", "failed", "canceled", "rejected"}


def _provider_name() -> str:
    return os.getenv("OPENAI_MODEL", "?")


async def _serve_scout(provider, root: Path, audit: Path):
    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = dp.LEADER_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("", encoding="utf-8")
    shutil.copytree(dp.SCOUT_WS / "acps", root / "acps")
    runtime = WitRuntime(
        partner_scout.build_scout_agent(provider), workspace=root, acps=dp._acps_config(PORT)
    )
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=PORT, amp=False)
    print(f"[scout] 监听 https://localhost:{PORT}/rpc → {status.status}（PEP 已启用）")
    return runtime


async def main() -> None:
    task = " ".join(sys.argv[1:]) or TASK
    out = CLUSTER / "demo_run" / "pep" / "real_business"
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pep-real-"))
    audit = out / "audit.jsonl"

    provider = agents.build_provider()
    print(f"[业务] 模型={_provider_name()} | 目标 skill={dp.PRIVATE_SKILL} | 任务={task[:60]}…")
    scout_rt = await _serve_scout(provider, tmp / "scout", audit)
    started = time.time()
    try:
        caller, state = dp._caller_runtime(tmp, PORT)
        issued = issue_token(
            load_signing_context(dp.SUPERVISOR_WS / "acps" / "client", trust_bundle=dp.TRUST_BUNDLE),
            delegate_aic=dp.CRITIC_AIC,
            target_aic=dp.SCOUT_AIC,
            skill_id=dp.PRIVATE_SKILL,
            ttl_seconds=600,
        )
        print(f"[token] 已签发 jti={issued.jti[:8]}… 有效期 {issued.expires_at - int(time.time())}s")

        result = await direct_fetch(
            caller, state, target_aic=dp.SCOUT_AIC, request=task,
            token=issued.token, skill_id=dp.PRIVATE_SKILL,
        )
        task_ref = result.get("task_ref")
        print(f"[委派] taskRef={task_ref} state={result.get('state')} 用时 {time.time()-started:.1f}s")

        # awaiting-completion = 伙伴已产出、等 leader 关单（AIP 规定 leader 必须发 complete）
        for _ in range(60):
            if result.get("state") in TERMINAL or result.get("state") == "awaiting-completion":
                break
            if not task_ref:
                break
            await asyncio.sleep(5)
            result = await caller.aip_task(state, action="wait", task_ref=task_ref)
            print(f"  [poll] state={result.get('state')} products={len(result.get('products') or [])}")

        if result.get("state") == "awaiting-completion" and task_ref:
            closed = await caller.aip_task(state, action="complete", task_ref=task_ref)
            print(f"[收尾] leader 已 complete → state={closed.get('state')}")
            result = closed if closed.get("products") else result

        elapsed = time.time() - started
        print(f"\n[结果] state={result.get('state')} 用时 {elapsed:.1f}s")
        for product in result.get("products") or []:
            print(f"== Product: {product.get('name')} ({product.get('id')})")
            for item in product.get("data_items") or product.get("dataItems") or []:
                blob = item.get("text") or json.dumps(item.get("data"), ensure_ascii=False)
                print(blob[:4000])
        (out / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if audit.exists():
            print("\n[审计]")
            for line in audit.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                print(
                    f"  {record['decision']:5s} {record['rule']:20s} code={record['code']} "
                    f"jti={(record['jti'] or '-')[:8]} reason={record['reason']}"
                )
        print(f"\n[done] 产物写入 {out}")
    finally:
        await scout_rt.stop_serving()
        await provider.aclose()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
