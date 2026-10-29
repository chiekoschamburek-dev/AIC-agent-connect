"""端到端验证：leader 携真实委托令牌，在**关闭白名单**的资源 agent 上通过 PEP 准入。

这是"leader 也必须带令牌"这一需求的**决定性证据**：
  · 白名单为空（leader 不走 leader-allowlist）
  · leader 指名非公开干活能力（cpu.run），且不携令牌 → deny / no-credential
  · leader 携**真实签发**的委托令牌 → allow（走 L2 令牌路径）

令牌用真实签名上下文签、用真实 trust-bundle 验，不走任何 mock。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

CL = Path("/opt/wit/my-project/acps_cluster")
sys.path.insert(0, str(CL.parent))
sys.path.insert(0, str(CL))

from acps_cluster.delegation.pep import PepConfig, evaluate  # noqa: E402
from acps_cluster.delegation.token import (  # noqa: E402
    ROLE_DELEGATION_TOKEN,
    TOKEN_DATA_KEY,
    issue_token,
    load_signing_context,
    token_data_item,
)

WS = CL / "ws-supervisor"
BUNDLE = WS / "acps" / "trust-bundle.pem"
LEADER = "1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6"
# 资源 agent 尚未注册 → 用与现网同格式的占位 AIC
RESOURCE = "1.2.156.3088.1.BUPT.VTF97R.PK47TC.WSY8VG.089N"
SKILL = "cpu.run"


def req(*, token: str | None, skill: str = SKILL):
    items = [{"type": "text", "text": "跑一次 CPU 任务"}]
    if token:
        item = token_data_item(token, target_aic=RESOURCE, skill_id=skill)
        items.append(item)
    return SimpleNamespace(
        leader_aic=LEADER, command_params={"skillId": skill}, data_items=items,
        peer_verified=True, task_id="t-e2e", session_id="s-e2e", delivery="rpc",
    )


def cfg() -> PepConfig:
    return PepConfig(
        enabled=True, my_aic=RESOURCE,
        # 展示能力保持公开（cpu.env/cpu.status），干活能力非公开
        public_skills=frozenset({"cpu.env", "cpu.status"}),
        has_non_public_skills=True,
        # ★ 承认 leader 的**签发资格**（否则令牌被判 issuer-not-authorized），
        #   但关掉直调免令牌通道 → leader 也必须持令牌
        leader_aics=frozenset({LEADER}),
        leader_direct_open=False,
        trust_bundle=BUNDLE,                     # ★ L2 验签必需
    )


def main() -> int:
    failures: list[str] = []

    # 0) 前置：签名链必须完整（chain ≥ 2），否则令牌签出来也验不过
    sc = load_signing_context(WS / "acps" / "client", trust_bundle=BUNDLE)
    print(f"  [0] signing.chain 长度 = {len(sc.chain)}（必须 ≥2，否则令牌无法锚定）")
    if len(sc.chain) < 2:
        failures.append("[0] 证书链不完整（缺中间 CA），令牌必然验签失败")

    # 1) 无令牌 → deny
    out1 = evaluate(req(token=None), cfg())
    print(f"  [1] leader 指名 {SKILL}、**无令牌** → {out1.decision} / {out1.rule}")
    if out1.allowed:
        failures.append(f"[1] 无令牌却放行了（rule={out1.rule}）")
    if out1.rule != "no-credential":
        failures.append(f"[1] 期望规则 no-credential，实得 {out1.rule}")

    # 2) 携真实令牌 → allow（L2）
    #    注意 `delegate_aic` 必须是**实际发起调用者**（leader 自己）：
    #    资源 agent 侧校验 `act.aic == request.leader_aic`（PEP 3e "act 与 peer AIC 绑定"）。
    issued = issue_token(
        sc, delegate_aic=LEADER, target_aic=RESOURCE, skill_id=SKILL,
        ttl_seconds=300, include_intermediates=True,
    )
    out2 = evaluate(req(token=issued.token), cfg())
    print(f"  [2] leader 指名 {SKILL}、**携真实令牌** → {out2.decision} / {out2.rule}")
    if not out2.allowed:
        failures.append(f"[2] 携有效令牌仍被拒：{out2.decision} / {out2.rule} / {out2.reason}")

    # 3) 令牌 skill 与请求 skill 不一致 → 必须拒绝
    issued_wrong = issue_token(
        sc, delegate_aic=LEADER, target_aic=RESOURCE, skill_id="gpu.run",
        ttl_seconds=300, include_intermediates=True,
    )
    out3 = evaluate(req(token=issued_wrong.token, skill=SKILL), cfg())
    print(f"  [3] 令牌绑定 gpu.run 却请求 {SKILL} → {out3.decision} / {out3.rule}")
    if out3.allowed:
        failures.append("[3] 令牌与请求能力不一致却放行了！")

    # 4) 令牌受众指向别人 → 必须拒绝
    OTHER = "1.2.156.3088.1.BUPT.VTF97R.ZZZZZZ.ZZZZZZ.ZZZZ"
    issued_other = issue_token(
        sc, delegate_aic=OTHER, target_aic=OTHER, skill_id=SKILL,
        ttl_seconds=300, include_intermediates=True,
    )
    out4 = evaluate(req(token=issued_other.token), cfg())
    print(f"  [4] 令牌受众是别的 agent → {out4.decision} / {out4.rule}")
    if out4.allowed:
        failures.append("[4] 受众不符却放行了！")

    # 5) 过期令牌 → 必须拒绝
    issued_exp = issue_token(
        sc, delegate_aic=RESOURCE, target_aic=RESOURCE, skill_id=SKILL,
        ttl_seconds=1, include_intermediates=True, now=time.time() - 3600,
    )
    out5 = evaluate(req(token=issued_exp.token), cfg())
    print(f"  [5] 已过期令牌 → {out5.decision} / {out5.rule}")
    if out5.allowed:
        failures.append("[5] 过期令牌却放行了！")

    # 6) 展示能力仍公开（不被本次改造误伤）
    out6 = evaluate(req(token=None, skill="cpu.env"), cfg())
    print(f"  [6] leader 指名展示能力 cpu.env（无令牌）→ {out6.decision} / {out6.rule}")
    if not out6.allowed:
        failures.append(f"[6] 展示能力被误拒：{out6.rule}")

    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：白名单关闭后，leader 必须持有效令牌才能调干活能力；")
    print("        令牌的能力/受众/时效绑定全部生效；展示能力不受影响。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
