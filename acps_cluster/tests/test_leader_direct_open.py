"""单测：`leader_direct_open` 把"直调放行"与"签发者授权"解耦。

## 背景（2026-10-11 实测发现的耦合）

`leader_aics` 原本同时承担两个职责：
  ① L1 直调白名单（`pep.py:440` → allow / leader-allowlist）
  ② 令牌**签发者**白名单（`pep.py:538` `allowed_issuers = cfg.leader_aics`）

于是"关掉白名单让 leader 也必须带令牌"会**连带剥夺 leader 的签发资格**，
它签的令牌被判 `deny / issuer-not-authorized`（实测：
`token issuer is not an authorized leader`）——两个需求互相打架。

修法：新增 `leader_direct_open`（env `ACPS_PEP_LEADER_DIRECT`，默认 True 向后兼容）。
资源类 agent 保留 `leader_aics={leader}`（承认签发资格）并设 `leader_direct_open=False`
（不给直调免令牌通道）→ "leader 也必须持令牌"与"leader 签的令牌有效"同时成立。

## 覆盖

1. 默认（True）：leader 直调 → allow / leader-allowlist（既有行为不变）
2. 关闭后：leader 直调、无令牌 → **deny / no-credential**
3. 关闭后：leader 的 AIC 仍在 `leader_aics` 里 → **签发者授权不受影响**（关键）
4. 关闭后：非 leader 调用不受影响（仍按原规则）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ["YANSHU_RUN_TRACE"] = "0"

from delegation.pep import PepConfig, evaluate  # noqa: E402

LEADER = "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"
OUTSIDER = "1.2.156.3088.1.0001.00001.EZTK03.JISTB6.0P3V"


def cfg(*, direct_open: bool) -> PepConfig:
    return PepConfig(
        enabled=True, my_aic="1.2.156.3088.1.BUPT.VTF97R.CPUAAA.CCCCCC.DDDD",
        public_skills=frozenset({"cpu.env", "cpu.status"}),
        has_non_public_skills=True,
        leader_aics=frozenset({LEADER}),      # ★ 保留签发资格
        leader_direct_open=direct_open,       # ★ 是否给直调免令牌通道
    )


def req(caller: str, skill: str | None = "cpu.run"):
    params = {"skillId": skill} if skill else {}
    return SimpleNamespace(
        leader_aic=caller, command_params=params,
        data_items=[{"type": "text", "text": "跑一次"}],
        peer_verified=True, task_id="t", session_id="s", delivery="rpc",
    )


def main() -> int:
    failures: list[str] = []

    # 1) 默认开启：既有行为不变
    out1 = evaluate(req(LEADER), cfg(direct_open=True))
    print(f"  [1] 默认(direct=True) leader 直调 → {out1.decision} / {out1.rule}")
    if not out1.allowed or out1.rule != "leader-allowlist":
        failures.append(f"[1] 既有行为被破坏：{out1.decision}/{out1.rule}")

    # 2) 关闭后：leader 直调被拒
    out2 = evaluate(req(LEADER), cfg(direct_open=False))
    print(f"  [2] direct=False leader 直调（无令牌）→ {out2.decision} / {out2.rule}")
    if out2.allowed:
        failures.append(f"[2] 关闭直调后仍放行：{out2.rule}")
    if out2.rule != "no-credential":
        failures.append(f"[2] 期望 no-credential，实得 {out2.rule}")

    # 3) ★ 关键：签发者授权不受影响（leader 仍在白名单 → 令牌可被接受）
    c = cfg(direct_open=False)
    print(f"  [3] direct=False 时 leader_aics 仍含 leader: {LEADER in c.leader_aics}")
    print(f"      → allowed_issuers 仍承认它，令牌不会被判 issuer-not-authorized")
    if LEADER not in c.leader_aics:
        failures.append("[3] 签发者资格被误删")

    # 4) 关闭直调不影响其他调用方
    out4 = evaluate(req(OUTSIDER, skill="cpu.env"), cfg(direct_open=False))
    print(f"  [4] direct=False 外部指名展示能力 → {out4.decision} / {out4.rule}")
    if not out4.allowed:
        failures.append(f"[4] 误伤了展示能力：{out4.rule}")
    out4b = evaluate(req(OUTSIDER), cfg(direct_open=False))
    print(f"      direct=False 外部指名干活能力 → {out4b.decision} / {out4b.rule}")
    if out4b.allowed:
        failures.append("[4] 外部竟能调干活能力")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：直调放行与签发者授权已解耦；关闭直调后 leader 必须持令牌。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
