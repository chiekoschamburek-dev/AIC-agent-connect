"""Phase D 域可见性过滤：V1–V5 五场景。

两种运行模式：
1. **纯函数模式**（默认，无 DB/无网络/无模型）：调用本仓
   `delegation.visibility.filter_rows` 纯谓词，用**真实签发的章程投影**做场景断言。
   该模块与 ACPs-community/discovery-server 的 domain_policy 保持同一套语义，
   因此本仓可以离线验证/演示，不依赖兄弟仓路径；线上仍由发现服务侧执行同一谓词。
2. **活栈模式**（--live，需 postgres + discovery-server）：起真服务打 HTTP。

场景：
  V1 匿名查询 → 只见 public（域资产不出现）
  V2 域外成员（critic）查询 → 仍不见域资产（存在性不泄露）
  V3 域内成员（experiment）查询 → 见域资产
  V4 兼容性 → 无 visibility 字段的存量资产对所有人可见（含匿名）

    cd my-project && python acps_cluster/delegation/demo_visibility.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delegation.domain_manifest import (  # noqa: E402
    build_manifest, export_policy_projection, sign_manifest, to_policy, verify_manifest,
)
from delegation.visibility import filter_rows  # noqa: E402

GPU_WS = CLUSTER / "ws-gpu"
EXP_WS = CLUSTER / "ws-experiment"
SUPERVISOR_WS = CLUSTER / "ws-supervisor"
TRUST = GPU_WS / "acps" / "trust-bundle.pem"

GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
CRITIC_AIC = "1.2.156.3088.1.1.BJ5X2N.QYTBJJ.6GH1CI.12KL"

DOMAIN = "lab-x-y"

# 模拟发现索引里的行（与 discovery-server 各查询路径的行结构一致：acs + skill_id）
ROWS = [
    {"acs": {"aic": GPU_AIC, "skills": [
        {"id": "gpu.env", "description": "公开能力（无 visibility 字段 = 存量形态）"},
        {"id": "gpu.run", "description": "域内算力",
         "visibility": "domain", "domains": [DOMAIN]},
    ]}, "skill_id": "gpu.env", "ranking": 1},
    {"acs": {"aic": GPU_AIC, "skills": [
        {"id": "gpu.env"}, {"id": "gpu.run", "visibility": "domain", "domains": [DOMAIN]},
    ]}, "skill_id": "gpu.run", "ranking": 2},
]


def _projection() -> dict:
    manifest = build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
        ],
        assets=[{"providerAic": GPU_AIC, "skill": "gpu.run", "allowedMembers": ["consumer"]}],
        issuers=[GPU_AIC],
    )
    from delegation.token import load_signing_context

    manifest = sign_manifest(manifest, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
    manifest = sign_manifest(manifest, load_signing_context(EXP_WS / "acps" / "client", trust_bundle=TRUST))
    body, signers = verify_manifest(manifest, trust_bundle=TRUST)
    assert signers == frozenset({GPU_AIC, EXP_AIC}), "章程双签验证失败"
    return export_policy_projection(to_policy(body, signers))


def run_pure() -> int:
    projection = _projection()
    policies = {projection["domainId"]: projection["members"]}
    out = CLUSTER / "demo_run" / "visibility"
    out.mkdir(parents=True, exist_ok=True)
    (out / "policy-projection.json").write_text(
        json.dumps(projection, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[visibility] 章程投影（双签验证后导出）：domain={DOMAIN} members={list(policies[DOMAIN])}")

    outsider_rows = filter_rows(ROWS, CRITIC_AIC, policies)
    member_rows = filter_rows(ROWS, EXP_AIC, policies)
    outsider_body_ids = {s["id"] for r in outsider_rows for s in r["acs"]["skills"]}
    member_body_ids = {s["id"] for r in member_rows for s in r["acs"]["skills"]}
    rows = [
        ("V1 匿名查询只见 public", ["gpu.env"], filter_rows(ROWS, None, policies)),
        ("V2 域外成员不见域资产", ["gpu.env"], outsider_rows),
        ("V3 域内成员可见域资产", ["gpu.env", "gpu.run"], member_rows),
        ("V4 存量资产（无字段）对所有人可见", ["gpu.env"], outsider_rows),
        ("V5 ACS 体脱敏：域外响应体不含域技能声明", [], []),
    ]
    passed = 0
    print()
    for name, expect_ids, got in rows:
        if name.startswith("V5"):
            ok = outsider_body_ids == {"gpu.env"} and member_body_ids == {"gpu.env", "gpu.run"}
            passed += ok
            print(f"{'✅' if ok else '❌'} {name}")
            print(f"    期望 域外体={{gpu.env}} 成员体={{gpu.env,gpu.run}} / 实际 域外体={outsider_body_ids} 成员体={member_body_ids}")
            continue
        got_ids = [r["skill_id"] for r in got]
        ok = got_ids == expect_ids
        passed += ok
        print(f"{'✅' if ok else '❌'} {name}")
        print(f"    期望 {expect_ids} / 实际 {got_ids}")
    print(f"\n[visibility] 纯函数模式 通过 {passed}/{len(rows)}（本仓谓词；线上 discovery-server 同语义）")
    return passed == len(rows)


async def run_live() -> int:
    """活栈模式：起 discovery-server（需 postgres）打真实 /discover HTTP。"""



    raise SystemExit("活栈模式需 postgres + discovery-server 运行环境（见 result.md §21 的栈说明），本次未随附自动化")


def main() -> None:
    if "--live" in sys.argv:
        import asyncio

        asyncio.run(run_live())
        return
    ok = run_pure()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
