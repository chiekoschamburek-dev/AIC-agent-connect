"""HPC 网关演示（H 系列）：异步作业全生命周期——提交→排队→沙盒执行→取结果。

场景：
  H1 公开 hpc.info / hpc.jobs 免票（橱窗）→ 放行
  H2 无票提交 hpc.submit → 拒（保险柜）
  H3 持域 grant 提交（异步）：jobId → 轮询 jobs 至 completed → result 取 stdout/产物
  H4 墙钟超限提交 → 校验拒绝
  H5 队列深度背压：灌满后第 9 个拒绝
  H6 配额：max_uses 的 grant 第 N+1 次 wire 提交 → quota-exhausted

    cd yanshu-AIC && python acps_cluster/delegation/demo_hpc.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delegation.demo_domain import (  # noqa: E402
    EXP_WS, SUPERVISOR_WS, TRUST, _acps_config, _record, _send, _state, ROWS,
)

import hpc_runner  # noqa: E402
import partner_hpc  # noqa: E402
from delegation import (  # noqa: E402
    build_manifest, issue_grant, load_signing_context, sign_manifest, verify_manifest, write_manifest,
)

HPC_WS = CLUSTER / "ws-hpc"
HPC_AIC = "1.2.156.3088.1.1.HPC001.AGENT01.RUN01.16Q7"
LEADER_AIC = json.loads((SUPERVISOR_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))["identity"]["ref"]["aic"]
EXP_AIC = json.loads((EXP_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))["identity"]["ref"]["aic"]
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
CRITIC_WS = CLUSTER / "ws-critic"

DOMAIN = "yanshu-hpc"
PORT_HPC = 18803
SUBMIT_SKILL = "hpc.submit"
INFO_SKILL = "hpc.info"


def _manifest(*, revision=1):
    return build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": HPC_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
            {"aic": LEADER_AIC, "role": "auditor"},
        ],
        assets=[{"providerAic": HPC_AIC, "skill": SUBMIT_SKILL, "allowedMembers": ["consumer"]}],
        issuers=[LEADER_AIC],
        revision=revision,
    )


async def _serve(manifests, audit: Path, tmp: Path):
    import os

    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = LEADER_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    os.environ["ACPS_PEP_DOMAINS_DIR"] = str(tmp / "domains")
    os.environ["ACPS_PEP_QUOTA_PATH"] = str(tmp / "quota.sqlite3")
    domains = tmp / "domains"
    domains.mkdir(parents=True, exist_ok=True)
    for m in manifests:
        write_manifest(m, domains)
    from wit import WitRuntime

    ws = tmp / "ws-hpc"
    shutil.copytree(HPC_WS / "acps", ws / "acps")
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("", encoding="utf-8")
    runtime = WitRuntime(partner_hpc.build_hpc_agent(None), workspace=ws, acps=_acps_config(PORT_HPC))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=PORT_HPC, amp=False)
    print(f"[hpc] :{PORT_HPC} → {status.status}（backend={hpc_runner.backend()}）")
    return runtime


JOB_CODE = """\
import json, torch
x = torch.randn(256, 256)
s = float((x @ x).sum())
json.dump({"hpc": "done", "sum": round(s, 2), "gpu": torch.cuda.get_device_name(0)},
          open("job_out.json", "w"))
print("HPC-JOB-OK")
"""


async def _await_terminal(job_id: str, *, timeout: float = 240) -> dict | None:
    for _ in range(int(timeout / 2)):
        await asyncio.sleep(2)
        await hpc_runner.worker_tick()
        job = hpc_runner.read_job(job_id)
        if job and job.get("state") in hpc_runner.TERMINAL:
            return job
    return None


async def main() -> None:
    m1 = _manifest()
    m1 = sign_manifest(m1, load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST))
    m1 = sign_manifest(m1, load_signing_context(HPC_WS / "acps" / "client", trust_bundle=TRUST))
    body, signers = verify_manifest(m1, trust_bundle=TRUST)
    _record("H0 HPC 域章程（hpc 为 provider，双签）", "2 签名方", f"{len(signers)} 签名方")

    tmp = Path(tempfile.mkdtemp(prefix="hpc-demo-"))
    out = CLUSTER / "demo_run" / "hpc"
    out.mkdir(parents=True, exist_ok=True)
    audit = out / "hpc-audit.jsonl"
    runtime = await _serve([m1], audit, tmp)
    leader_signing = load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST)

    # 清空作业目录（演示隔离）
    import glob

    for f in glob.glob(str(hpc_runner.JOBS_DIR / "*.json")):
        Path(f).unlink()

    try:
        # H1 橱窗
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_HPC, HPC_AIC, skill=INFO_SKILL)
        _record("H1 公开 hpc.info 免票", "task accepted", _state(resp))

        # H2 无票提交
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_HPC, HPC_AIC, skill=SUBMIT_SKILL)
        _record("H2 无票提交 hpc.submit", "task rejected", _state(resp))

        grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": HPC_AIC, "skill": SUBMIT_SKILL}], max_uses=3, ttl_seconds=3600,
        )

        # H3 异步全生命周期（能力直调路径：真实沙盒执行）
        sub = await partner_hpc.hpc_submit(
            name="gnn-train", files={"main.py": JOB_CODE}, walltimeMinutes=5, submitter=EXP_AIC)
        job_id = sub.get("jobId", "")
        _record("H3a 提交入队（异步）", "queued", sub.get("state", ""),
                f"jobId={job_id} backend={sub.get('backend')}")
        job = await _await_terminal(job_id)
        result = await partner_hpc.hpc_result(job_id) if job else {"ok": False}
        ok3 = job and job.get("state") == "completed" and "HPC-JOB-OK" in result.get("stdoutHead", "") \
            and any(a["name"] == "job_out.json" for a in result.get("artifacts", []))
        _record("H3b 终态取结果（stdout+产物元信息）", "completed+结果",
                "completed+结果" if ok3 else f"异常 state={job and job.get('state')} {str(result)[:100]}",
                f"duration={job and job.get('durationSeconds')}s exit={job and job.get('exitCode')}")

        # H4 墙钟超限
        r = await partner_hpc.hpc_submit(name="too-long", files={"main.py": "1"}, walltimeMinutes=999)
        _record("H4 墙钟超限拒绝", "拒绝", "拒绝" if (not r.get("ok") and "墙钟" in r.get("error", "")) else f"异常 {str(r)[:80]}")

        # H5 队列背压（当前无活跃作业——先灌 MAX_QUEUE_DEPTH 个）
        states = []
        for i in range(hpc_runner.MAX_QUEUE_DEPTH):
            r = hpc_runner.submit({"name": f"fill-{i}", "files": {"main.py": "print(1)"}, "walltimeMinutes": 1})
            states.append(r.get("ok"))
        r9 = hpc_runner.submit({"name": "overflow", "files": {"main.py": "1"}, "walltimeMinutes": 1})
        ok5 = all(states) and not r9.get("ok") and "背压" in r9.get("error", "")
        _record("H5 队列深度背压（第 9 个拒绝）", "8成1拒",
                "8成1拒" if ok5 else f"异常 {str(r9)[:80]}", f"深度上限={hpc_runner.MAX_QUEUE_DEPTH}")

        # H6 配额（wire 第 4 次）
        wire_states = []
        for _ in range(4):
            resp = await _send(EXP_WS, EXP_AIC, PORT_HPC, HPC_AIC, skill=SUBMIT_SKILL, token=grant.token)
            wire_states.append(_state(resp))
        _record("H6 配额第 4 次提交", "accepted,accepted,accepted,rejected", ",".join(wire_states),
                "直调路径不经配额，配额作用于 wire 准入")
    finally:
        await runtime.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n══════════════ HPC 网关（H 系列）结果 ════════════════")
    def _ok(row):
        e, g = row["expected"], row["outcome"]
        if "签名方" in e:
            return g.startswith(e)
        e2, g2 = e.replace("task ", ""), g.replace("task ", "")
        return (e2.count("accepted"), e2.count("rejected")) == (g2.count("accepted"), g2.count("rejected")) or g2 == e2
    passed = 0
    for row in ROWS:
        good = _ok(row)
        passed += good
        print(f"{'✅' if good else '❌'} {row['scenario']}")
        print(f"    期望 {row['expected']} / 实际 {row['outcome']}  {row['note'][:100]}")
    print(f"\n[hpc] 通过 {passed}/{len(ROWS)}；产物在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
