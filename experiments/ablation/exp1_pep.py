"""实验① PEP 任务级准入——对照实验 + 量化延迟。

对照：ACPS_PEP_ENABLED=0（官方 SDK 默认 accept） vs =1（研枢 PEP 裁决）。
逐项控制变量：唯一变量是 pep_decide 的 cfg.enabled，其余请求完全相同。

指标：
- 非法请求拦截率：off/on 两组各跑 12 种非法调用，统计被拒比例
- 合法请求成功率：合法公开调用 + 持有效 token 调用，统计放行比例
- 额外裁决延迟：on 组 pep_decide 的纯裁决耗时 p50/p95/p99（ms）

全部用临时自造 CA + 真实 EdDSA 签名，不依赖仓库私钥，可复现。
输出：out/exp1_pep.json + 控制台表。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "acps_cluster"))

from ablation_common import Forge, make_request, summarize, token_data_item  # noqa: E402

from acps_cluster.delegation import token as tok  # noqa: E402
from acps_cluster.delegation.pep import (  # noqa: E402
    JtiRegistry,
    PepConfig,
    pep_decide,
)

LEADER = "leader.aic"        # 白名单签发者（supervisor 角色）
DELEGATE = "delegate.aic"    # 受托方（critic 角色）
TARGET = "target.aic"        # 目标伙伴（scout 角色）
THIRD = "third.aic"          # 无关第三方
PUBLIC_SKILL = "arxiv.search"
PRIVATE_SKILL = "scholar.deep_fetch"


def allowed(decision) -> bool:
    state = getattr(decision, "state", None)
    return str(state).lower() in {"accepted", "working"}


def build_configs(bundle: Path) -> tuple[PepConfig, PepConfig]:
    """两版配置只差 enabled，其余字段逐字节相同。"""
    base = dict(
        my_aic=TARGET,
        leader_aics=frozenset({LEADER}),
        trust_bundle=bundle,
        audit_path=HERE / "out" / "pep-audit.jsonl",
        public_skills=frozenset({PUBLIC_SKILL}),
        has_non_public_skills=True,
    )
    return (
        PepConfig(enabled=False, **base),   # 对照：官方默认
        PepConfig(enabled=True, **base),    # 实验：研枢 PEP
    )


def tamper(t: str) -> str:
    import base64
    head, payload, sig = t.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["acps_skill_id"] = "evil.skill"
    forged = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    return f"{head}.{forged}.{sig}"


def resign(forge: Forge, signing, t: str, mutate) -> str:
    """改 claims 后用同一把私钥重签——签名可信但 claims 越界。"""
    import base64
    head, payload, _ = t.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    mutate(claims)
    new_payload = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode()
    from acps_cluster.delegation.token import _b64u_encode
    sig = _b64u_encode(signing.private_key.sign(f"{head}.{new_payload}".encode()))
    return f"{head}.{new_payload}.{sig}"


def main() -> int:
    (HERE / "out").mkdir(exist_ok=True)
    forge = Forge()
    leader = forge.leaf("leader", aic=LEADER)
    third = forge.leaf("third", aic=THIRD)
    cfg_off, cfg_on = build_configs(forge.trust_bundle)

    issued = tok.issue_token(leader, delegate_aic=DELEGATE, target_aic=TARGET, skill_id=PRIVATE_SKILL)
    consumed = tok.issue_token(leader, delegate_aic=DELEGATE, target_aic=TARGET, skill_id=PRIVATE_SKILL)
    expired = tok.issue_token(
        leader, delegate_aic=DELEGATE, target_aic=TARGET, skill_id=PRIVATE_SKILL,
        ttl_seconds=10, now=time.time() - 1000,
    )
    self_signed = tok.issue_token(third, delegate_aic=DELEGATE, target_aic=TARGET, skill_id=PRIVATE_SKILL)

    def item(t: str, target: str = TARGET, skill: str = PRIVATE_SKILL):
        return token_data_item(t, target_aic=target, skill_id=skill)

    def item_of(*, delegate=DELEGATE, target=TARGET, skill=PRIVATE_SKILL, session=None):
        """照抄单测 _token_item：token 内部 claim 与 dataItem 外层包装一致。"""
        it = tok.issue_token(
            leader, delegate_aic=delegate, target_aic=target, skill_id=skill, session_id=session
        )
        return item(it.token, target=target, skill=skill)

    # ---- 12 种非法调用（每一种都应被 PEP 拒绝；off 组应全部放行）----
    illegal = [
        ("无令牌直连非公开 skill", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL)),
        ("伪造签名 token", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(issued.token + "x")])),
        ("篡改 payload", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(tamper(issued.token))])),
        ("过期 token", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(expired.token)])),
        ("重放已消费 token", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(consumed.token)])),
        ("act 与调用方不符", make_request(
            sender_aic=THIRD, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(issued.token)])),
        ("aud 指向错误目标", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item_of(target=THIRD)])),  # token aud=THIRD，验签 expected=TARGET → 拒
        ("token skill 与请求不符", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item_of(skill=PUBLIC_SKILL)])),  # token 授 PUBLIC，请求要 PRIVATE → 拒
        ("会话绑定不符", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            session_id="session-ablation", data_items=[item_of(session="session-OTHER")])),
        ("签发者不在白名单(自签)", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(self_signed.token)])),
        ("签名可信但 claims 越界", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(resign(forge, leader, issued.token,
                                    lambda c: c.__setitem__("acps_skill_id", "evil.skill")))])),
        ("传输层身份未验证", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            peer_verified=False, data_items=[item(issued.token)])),
    ]

    # ---- 3 种合法调用（都应放行）----
    legal_issue = tok.issue_token(leader, delegate_aic=DELEGATE, target_aic=TARGET, skill_id=PRIVATE_SKILL)
    legal = [
        ("公开 skill 免令牌(L0)", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PUBLIC_SKILL)),
        ("持有效委托 token(L2)", make_request(
            sender_aic=DELEGATE, partner_aic=TARGET, skill_id=PRIVATE_SKILL,
            data_items=[item(legal_issue.token)])),
        ("白名单 leader 直调(L1)", make_request(
            sender_aic=LEADER, partner_aic=TARGET, skill_id=PRIVATE_SKILL)),
    ]

    # 消费一次 consumed token，让"重放"用例成立
    JtiRegistry().claim(consumed.jti, expires_at=consumed.expires_at, now=time.time())

    def run_group(cfg: PepConfig, cases, *, registry_per_case: bool = True):
        rows, lat = [], []
        for name, req in cases:
            reg = JtiRegistry() if registry_per_case else JtiRegistry()
            t0 = time.perf_counter()
            decision = pep_decide(req, cfg, jti_registry=reg)
            dt_ms = (time.perf_counter() - t0) * 1000
            lat.append(dt_ms)
            rows.append({"name": name, "allowed": allowed(decision)})
        return rows, lat

    # 重放用例需要共享 registry：先让原 token 被消费一次
    shared = JtiRegistry()
    shared.claim(consumed.jti, expires_at=consumed.expires_at, now=time.time())
    replay_req = illegal[4][1]

    def run_illegal(cfg: PepConfig):
        rows, lat = [], []
        for idx, (name, req) in enumerate(illegal):
            reg = shared if idx == 4 else JtiRegistry()
            t0 = time.perf_counter()
            decision = pep_decide(req, cfg, jti_registry=reg)
            lat.append((time.perf_counter() - t0) * 1000)
            rows.append({"name": name, "allowed": allowed(decision)})
        return rows, lat

    off_ill, _ = run_illegal(cfg_off)
    on_ill, on_lat = run_illegal(cfg_on)
    off_leg, _ = run_group(cfg_off, legal)
    on_leg, leg_lat = run_group(cfg_on, legal)

    off_block = sum(1 for r in off_ill if not r["allowed"])
    on_block = sum(1 for r in on_ill if not r["allowed"])
    off_pass = sum(1 for r in off_leg if r["allowed"])
    on_pass = sum(1 for r in on_leg if r["allowed"])

    # 重放延迟单独测多轮，避免单次抖动
    replay_lat = []
    for _ in range(200):
        reg = JtiRegistry()
        reg.claim(consumed.jti, expires_at=consumed.expires_at, now=time.time())
        t0 = time.perf_counter()
        pep_decide(replay_req, cfg_on, jti_registry=reg)
        replay_lat.append((time.perf_counter() - t0) * 1000)

    result = {
        "experiment": "① PEP 任务级准入对照",
        "control_variable": "cfg.enabled（off=官方默认 accept / on=研枢 PEP 裁决）",
        "illegal": {
            "total": len(illegal),
            "off_blocked": off_block,
            "on_blocked": on_block,
            "off_block_rate": round(off_block / len(illegal) * 100, 1),
            "on_block_rate": round(on_block / len(illegal) * 100, 1),
            "cases": [{"name": a[0], "off_allowed": b["allowed"], "on_allowed": c["allowed"]}
                      for a, b, c in zip(illegal, off_ill, on_ill)],
        },
        "legal": {
            "total": len(legal),
            "off_passed": off_pass,
            "on_passed": on_pass,
            "off_pass_rate": round(off_pass / len(legal) * 100, 1),
            "on_pass_rate": round(on_pass / len(legal) * 100, 1),
            "cases": [{"name": a[0], "off_allowed": b["allowed"], "on_allowed": c["allowed"]}
                      for a, b, c in zip(legal, off_leg, on_leg)],
        },
        "latency_ms": {
            "illegal_decision_on": summarize(on_lat),
            "legal_decision_on": summarize(leg_lat),
            "valid_token_decision_on": summarize(replay_lat),
        },
    }

    out = HERE / "out" / "exp1_pep.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 78)
    print("实验① PEP 任务级准入对照（唯一变量：cfg.enabled）")
    print("=" * 78)
    print(f"非法调用 {len(illegal)} 种：off 拦截 {off_block}/{len(illegal)} | "
          f"on 拦截 {on_block}/{len(illegal)}  → 拦截率 {result['illegal']['off_block_rate']}% → "
          f"{result['illegal']['on_block_rate']}%")
    for a, b, c in zip(illegal, off_ill, on_ill):
        mark = "OK" if (b["allowed"] and not c["allowed"]) else "!!"
        print(f"  [{mark}] {a[0]:<28} off={'放行' if b['allowed'] else '拒绝'} "
              f"on={'放行' if c['allowed'] else '拒绝'}")
    print(f"合法调用 {len(legal)} 种：off 通过 {off_pass}/{len(legal)} | "
          f"on 通过 {on_pass}/{len(legal)}  → 通过率 {result['legal']['off_pass_rate']}% → "
          f"{result['legal']['on_pass_rate']}%")
    for a, b, c in zip(legal, off_leg, on_leg):
        mark = "OK" if (b["allowed"] and c["allowed"]) else "!!"
        print(f"  [{mark}] {a[0]:<28} off={'放行' if b['allowed'] else '拒绝'} "
              f"on={'放行' if c['allowed'] else '拒绝'}")
    print("-" * 78)
    print("裁决延迟（on，纯裁决，ms）:")
    for k, v in result["latency_ms"].items():
        print(f"  {k:<28} n={v['n']:<4} p50={v['p50']:<8} p95={v['p95']:<8} "
              f"p99={v['p99']:<8} mean={v['mean']}")
    print(f"\n明细：{out}")
    return 0 if (on_block == len(illegal) and on_pass == len(legal)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
