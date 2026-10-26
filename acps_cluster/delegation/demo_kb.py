"""知识库演示（K 系列）：数据不动计算动 + 字段级脱敏 + 防整库导出 + 资源方自主授权。

场景：
  K1 公开 schema 免票（橱窗：只有结构没有内容）→ 放行
  K2 无票查询 kb.query → 拒（保险柜）
  K3 持域 grant 查询（filter 模式）→ 行返回，curator 字段被脱敏并注明
  K4 compute 模式（聚合下推）→ 只有聚合表，原始行/摘要/全文不下发
  K5 整库导出尝试（limit=9999）→ 行上限拒绝
  K6 按受限字段 groupby（变相读值）→ 拒绝
  K7 受控写入（ingest）：校验+溯源；写入后查询可见
  K8 配额：max_uses=3 的 grant 第 4 次 wire 查询 → quota-exhausted
  K9 全文检索（search 模式）：主题式问法问得出答案（旧实现只有精确匹配）
  K10 检索面按档位分索引：同一个越权词 consumer 0 命中、provider 才命中
  K11 资源方自主授权（B3）：章程 issuers 只含 KB → leader 自签 grant 被拒

    cd yanshu-AIC && python acps_cluster/delegation/demo_kb.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
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
    EXP_WS, SUPERVISOR_WS, TRUST, _acps_config, _audit_rule, _record, _send, _state, ROWS,
)

import cluster_common  # noqa: E402
import kb_runner  # noqa: E402
import partner_kb  # noqa: E402
from delegation import (  # noqa: E402
    build_manifest, issue_grant, load_signing_context, sign_manifest, verify_manifest, write_manifest,
)

KB_WS = CLUSTER / "ws-kb"
KB_AIC = "1.2.156.3088.1.1.KB0001.AGENT01.RUN01.012I"
LEADER_AIC = json.loads((SUPERVISOR_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))["identity"]["ref"]["aic"]
EXP_AIC = json.loads((EXP_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))["identity"]["ref"]["aic"]
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"
CRITIC_WS = CLUSTER / "ws-critic"

DOMAIN = "yanshu-kb"
PORT_KB = 18802
QUERY_SKILL = "kb.query"
SCHEMA_SKILL = "kb.schema"


def _manifest(*, revision=1):
    """B3 口径：**资源方自己签发**。leader 只是 consumer 成员，不再有签发权。

    `pep._evaluate_grant` 对 grant 只认章程 `issuers`（注释原话："grant 的权力来源是
    章程不是编排关系"），所以 issuers 里去掉 leader 之后，leader 自签的令牌会被判
    `issuer-not-in-domain`——见 K11。
    """
    return build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": KB_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
            {"aic": LEADER_AIC, "role": "consumer"},
        ],
        assets=[
            {"providerAic": KB_AIC, "skill": QUERY_SKILL, "allowedMembers": ["consumer"]},
            # 策展写入是本馆特权：外来 consumer 不能往别人家库里写东西
            {"providerAic": KB_AIC, "skill": "kb.ingest", "allowedMembers": ["provider"]},
        ],
        issuers=[KB_AIC],
        revision=revision,
    )


async def _serve(manifests, audit: Path, tmp: Path):
    import os

    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = LEADER_AIC
    # 与现网 `/etc/wit-kb-pep.env` 一致：资源 agent 关掉"白名单免令牌"的 L1 直调通道。
    # 不设这条，leader 的调用在 L1 就被放行、令牌根本不进裁决——
    # K11 实测踩过：自签 grant 显示 accepted，审计里是 leader-allowlist 而不是 grant 判据。
    os.environ["ACPS_PEP_LEADER_DIRECT"] = "0"
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    os.environ["ACPS_PEP_DOMAINS_DIR"] = str(tmp / "domains")
    os.environ["ACPS_PEP_QUOTA_PATH"] = str(tmp / "quota.sqlite3")
    domains = tmp / "domains"
    domains.mkdir(parents=True, exist_ok=True)
    for m in manifests:
        write_manifest(m, domains)
    from wit import WitRuntime

    ws = tmp / "ws-kb"
    shutil.copytree(KB_WS / "acps", ws / "acps")
    # 本库章程也放进 KB 自己工作区（partner_kb._role_table_from_domains 读它做字段脱敏）。
    # 先清掉副本里带过来的现网章程：现网若有一条更高 revision 的 yanshu-kb，
    # `load_domain_policies` 会取最高版本，演示就会悄悄改用现网策略而不是自己这份。
    local_domains = ws / "acps" / "domains"
    local_domains.mkdir(parents=True, exist_ok=True)
    for stale in local_domains.glob("*.json"):
        stale.unlink()
    for m in manifests:
        write_manifest(m, local_domains)
    # 角色表与 PEP 都从这份副本读，而不是真 ws-kb——否则演示依赖现网章程
    os.environ["ACPS_KB_WORKSPACE"] = str(ws)
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("", encoding="utf-8")
    runtime = WitRuntime(partner_kb.build_kb_agent(None), workspace=ws, acps=_acps_config(PORT_KB))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=PORT_KB, amp=False)
    print(f"[kb] :{PORT_KB} → {status.status}")
    return runtime


async def main() -> None:
    # K 系列跑在独立合成库上：现网 ws-kb/kb.sqlite3 装的是真实语料，
    # 演示既不能依赖它的内容，也不能往里写测试记录。
    tmp = Path(tempfile.mkdtemp(prefix="kb-demo-"))
    os.environ["ACPS_KB_PATH"] = str(tmp / "kb-demo.sqlite3")
    kb_runner.ensure_seed_kb()

    kb_signing = load_signing_context(KB_WS / "acps" / "client", trust_bundle=TRUST)
    leader_signing = load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST)
    m1 = _manifest()
    m1 = sign_manifest(m1, kb_signing)          # 资源方：签发
    m1 = sign_manifest(m1, leader_signing)      # 消费方：确认条款（签名者须是域当事方）
    body, signers = verify_manifest(m1, trust_bundle=TRUST)
    _record("K0 域章程由资源方自签（issuers 只含 KB）", "2 签名方", f"{len(signers)} 签名方",
            f"issuers={len(body.get('issuers') or [])} 且为 KB 自己；leader 已降级为 consumer")

    out = CLUSTER / "demo_run" / "kb"
    out.mkdir(parents=True, exist_ok=True)
    audit = out / "kb-audit.jsonl"
    runtime = await _serve([m1], audit, tmp)

    # 直调路径的角色来源与 wire 路径一致：mTLS 对端 AIC + 本机已验证章程投影。
    # 下面这些本地能力调用先"扮演"已认证的 consumer 对端（2026-10-06 审查后
    # kb_query 不再有 caller/role_table 入参——伪造角色表即提权的洞已封）。
    cluster_common.set_peer_for_direct_call(EXP_AIC)

    try:
        # K1 公开 schema
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_KB, KB_AIC, skill=SCHEMA_SKILL)
        _record("K1 公开 kb.schema 免票", "task accepted", _state(resp))

        # K2 无票查询
        resp = await _send(CRITIC_WS, CRITIC_AIC, PORT_KB, KB_AIC, skill=QUERY_SKILL)
        _record("K2 无票查询 kb.query", "task rejected", _state(resp))

        grant = issue_grant(
            kb_signing, domain=DOMAIN, domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": KB_AIC, "skill": QUERY_SKILL}], max_uses=3, ttl_seconds=3600,
        )

        # K3 filter 模式：consumer 视角，请求 curator 字段
        r = await partner_kb.kb_query(
            {"mode": "filter",
             "where": [{"field": "year", "op": ">=", "value": 2024}, {"field": "tags", "op": "contains", "value": "GNN"}],
             "fields": ["id", "title", "year", "abstract", "full_text", "internal_note"], "limit": 10})
        blob = json.dumps(r, ensure_ascii=False)
        redacted_ok = set(r.get("redactedFields") or []) == {"full_text", "internal_note"}
        no_leak = "机构订阅全文" not in blob and "内部评审分" not in blob
        _record("K3 filter 查询：curator 字段脱敏", "脱敏+不泄露",
                "脱敏+不泄露" if (r.get("ok") and redacted_ok and no_leak and r["returned"] > 0) else f"异常 {str(r)[:120]}",
                f"matched={r.get('matched')} returned={r.get('returned')} redacted={r.get('redactedFields')}")

        # K4 compute 模式：聚合下推
        r = await partner_kb.kb_query(
            {"mode": "compute", "groupby": "venue",
             "aggs": [{"fn": "count"}, {"fn": "avg", "field": "year"}]})
        blob = json.dumps(r, ensure_ascii=False)
        _record("K4 compute 聚合下推（行不下发）", "只有聚合表",
                "只有聚合表" if (r.get("ok") and "abstract" not in blob and "rows" not in r) else f"异常 {str(r)[:100]}",
                f"groups={r.get('groups')} table={r.get('table', [])[:3]}…")

        # K5 整库导出尝试
        r = await partner_kb.kb_query({"mode": "filter", "limit": 9999})
        _record("K5 整库导出尝试（limit=9999）", "行上限拒绝",
                "行上限拒绝" if (not r.get("ok") and "行数上限" in r.get("error", "")) else f"异常 {str(r)[:80]}")

        # K6 按受限字段 groupby（变相读值）
        r = await partner_kb.kb_query(
            {"mode": "compute", "groupby": "internal_note", "aggs": [{"fn": "count"}]})
        _record("K6 按受限字段 groupby（变相读值）", "拒绝",
                "拒绝" if not r.get("ok") else "异常：放行了")

        # K7 受控写入 + 可见
        r = await partner_kb.kb_ingest(
            [{"title": "跨机构知识库共享的实证研究", "year": 2026, "tags": ["KB", "sharing"]}],
            submitter=EXP_AIC)
        r2 = await partner_kb.kb_query(
            {"mode": "filter", "where": [{"field": "year", "op": ">=", "value": 2026}], "limit": 5})
        seen = any("跨机构知识库" in str(row.get("title", "")) for row in r2.get("rows", []))
        _record("K7 受控写入（校验+溯源）后可见", "写入+可见",
                "写入+可见" if (r.get("ok") and seen) else f"异常 {str(r)[:80]}",
                f"added={r.get('added')} 溯源={bool((r.get('provenance') or {}).get('submittedBy'))}")

        # K8 配额：E2E wire 第 4 次（前 3 次：K8 里连发）
        states = []
        for _ in range(4):
            resp = await _send(EXP_WS, EXP_AIC, PORT_KB, KB_AIC, skill=QUERY_SKILL, token=grant.token)
            states.append(_state(resp))
        _record("K8 配额第 4 次查询", "accepted,accepted,accepted,rejected", ",".join(states),
                "直调路径不经配额（同进程），配额作用于 wire 准入")

        # K11 资源方自主授权（B3）：leader 拿自己的 key 自签 grant —— 章程里没有它当签发方
        self_grant = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=LEADER_AIC,
            assets=[{"providerAic": KB_AIC, "skill": QUERY_SKILL}], max_uses=5, ttl_seconds=3600,
        )
        resp = await _send(SUPERVISOR_WS, LEADER_AIC, PORT_KB, KB_AIC,
                           skill=QUERY_SKILL, token=self_grant.token)
        state = _state(resp)
        off = [r for r in _audit_rule(audit) if r.startswith("issuer-not-in-domain")]
        _record("K11 leader 自签 grant（已无签发权）", "task rejected",
                "task rejected" if (state.startswith("rejected") and off) else f"异常 state={state}",
                f"判据={off[-1] if off else '（审计里没有 issuer-not-in-domain）'}；"
                "签发权在章程里，不在编排关系里")

        # K9 全文检索：主题式问法要问得出答案（旧实现只有精确匹配，问不出来）
        r = await partner_kb.kb_query({"mode": "search", "query": "合成摘要", "limit": 5})
        blob = json.dumps(r, ensure_ascii=False)
        hit_ok = r.get("ok") and r.get("matched", 0) > 0 and "合成摘要" in blob
        _record("K9 全文检索 search 模式（主题式问法）", "检索命中",
                "检索命中" if hit_ok else f"异常 {str(r)[:100]}",
                f"matched={r.get('matched')} 档位={r.get('searchLanes')} "
                f"首条摘录={(r['hits'][0].get('excerpt') if r.get('hits') else '')[:34]}…")

        # K10 检索面按档位分索引：同一个越权词，consumer 的通道里根本没有那份内容
        probe = "机构订阅全文"
        consumer = await partner_kb.kb_query({"mode": "search", "query": probe})
        cluster_common.set_peer_for_direct_call(KB_AIC)      # provider 侧（策展身份）
        provider = await partner_kb.kb_query({"mode": "search", "query": probe})
        cluster_common.set_peer_for_direct_call(EXP_AIC)
        verdict = ("检索面守住档位" if (consumer.get("matched") == 0
                                       and provider.get("matched", 0) > 0)
                   else f"异常 consumer={consumer.get('matched')} provider={provider.get('matched')}")
        _record("K10 检索面按档位分索引（越权内容不进调用方通道）", "检索面守住档位", verdict,
                f"consumer 档={consumer.get('searchLanes')} provider 档={provider.get('searchLanes')}；"
                f"命中数 0 vs {provider.get('matched')}，摘录与得分均无越权内容")
    finally:
        await runtime.stop_serving()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n══════════════ 知识库（K 系列）结果 ════════════════")
    def _ok(row):
        e, g = row["expected"], row["outcome"]
        if "签名方" in e:
            return g.startswith(e)
        e2 = e.replace("task ", "")
        if e2.startswith("accepted"):
            return g == e or g.replace("task ", "") == e2
        return g == e or g.replace("task ", "") == e2
    passed = 0
    for row in ROWS:
        good = _ok(row)
        passed += good
        print(f"{'✅' if good else '❌'} {row['scenario']}")
        print(f"    期望 {row['expected']} / 实际 {row['outcome']}  {row['note'][:100]}")
    print(f"\n[kb] 通过 {passed}/{len(ROWS)}；产物在 {out}")


if __name__ == "__main__":
    asyncio.run(main())
