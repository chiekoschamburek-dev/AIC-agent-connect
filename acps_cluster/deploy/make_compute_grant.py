"""模拟算力公司签发“域级 Grant”（不经过研枢 supervisor 签名）。

用途：比赛 demo 里 GPU/CPU 属于算力公司，算力方用自己的 AIC/私钥建域并给研枢
experiment 签发 grant；研枢只消费 grant，不参与签发。

本脚本把“算力公司”模拟为 provider 工作区（默认 ws-gpu）：
- 建一份双签域章程：provider + consumer（experiment）各签一次；
- issuers 只有 provider；
- 用 provider 私钥给 consumer 签发 grant，落到 consumer 的 grants 目录；
- 打印/写出供 experiment 读取的 external grant 文件（0600）。

运行（my-project 下）：
    python acps_cluster/deploy/make_compute_grant.py \
        --provider-workspace gpu --consumer-workspace experiment \
        --domain compute-gpu --max-uses 5 --ttl 3600
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER))

from delegation.domain_manifest import (  # noqa: E402
    build_manifest,
    sign_manifest,
    verify_manifest,
    write_manifest,
)
from delegation.grant import issue_grant  # noqa: E402
from delegation.token import load_signing_context  # noqa: E402


def _aic(workspace: Path) -> str:
    data = json.loads((workspace / "acps" / "acs.final.json").read_text(encoding="utf-8"))
    aic = str(data.get("aic") or "").strip()
    if not aic:
        raise SystemExit(f"{workspace}/acs.final.json 缺 AIC")
    return aic


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider-workspace", default="gpu",
                    help="算力方工作区名（ws-<name>，默认 gpu）")
    ap.add_argument("--consumer-workspace", default="experiment",
                    help="消费方工作区名（ws-<name>，默认 experiment）")
    ap.add_argument("--consumer-aic", default="",
                    help="消费方 AIC（跨机时填服务器 experiment AIC；默认从 workspace 读）")
    ap.add_argument("--supervisor-workspace", default="supervisor",
                    help="第二签名方（域 auditor；只签章程，不签调用 token）")
    ap.add_argument("--out", default="",
                    help="grant 文件输出路径（默认 <consumer>/acps/grants/<domain>.grant.json）")
    ap.add_argument("--domain", default="compute-gpu")
    ap.add_argument("--revision", type=int, default=1)
    ap.add_argument("--previous", default=None,
                    help="上一版章程文件（revision>1 必填/可自动发现）")
    ap.add_argument("--max-uses", type=int, default=5)
    ap.add_argument("--ttl", type=int, default=3600)
    ap.add_argument("--skill", action="append", default=[],
                    help="可重复；默认 gpu.run 与 gpu.data.fetch")
    args = ap.parse_args()

    provider_ws = CLUSTER / f"ws-{args.provider_workspace}"
    consumer_ws = CLUSTER / f"ws-{args.consumer_workspace}"
    supervisor_ws = CLUSTER / f"ws-{args.supervisor_workspace}"
    provider_aic = _aic(provider_ws)
    consumer_aic = args.consumer_aic.strip() or _aic(consumer_ws)
    supervisor_aic = _aic(supervisor_ws)
    provider_signing = load_signing_context(
        provider_ws / "acps" / "client", trust_bundle=provider_ws / "acps" / "trust-bundle.pem")
    supervisor_signing = load_signing_context(
        supervisor_ws / "acps" / "client", trust_bundle=supervisor_ws / "acps" / "trust-bundle.pem")
    trust = provider_ws / "acps" / "trust-bundle.pem"
    domains_dir = provider_ws / "acps" / "domains"
    existing = sorted(
        domains_dir.glob(f"{args.domain}.r*.json"),
        key=lambda p: int(p.stem.rsplit(".r", 1)[-1]),
    ) if domains_dir.is_dir() else []
    if existing and int(existing[-1].stem.rsplit(".r", 1)[-1]) >= args.revision:
        print(f"✘ 目录里已有 {args.domain}.r{existing[-1].stem.rsplit('.r', 1)[-1]}"
              f"（≥ 本次 revision={args.revision}）；修订必须升 revision")
        return 2
    previous = None
    if args.revision > 1:
        prev_path = Path(args.previous) if args.previous else (
            domains_dir / f"{args.domain}.r{args.revision - 1}.json"
        )
        if not prev_path.is_file() and existing:
            prev_path = existing[-1]
        if not prev_path.is_file():
            print(f"✘ revision={args.revision} 需要上一版章程；找不到 {prev_path}，用 --previous 指定")
            return 2
        previous = json.loads(prev_path.read_text(encoding="utf-8"))
        prev_body, _ = verify_manifest(previous, trust_bundle=trust)
        if int(prev_body.get("revision") or 0) >= args.revision:
            print("✘ 上一版 revision 不低于新 revision")
            return 2

    skills = args.skill or ["gpu.run", "gpu.data.fetch"]
    manifest = build_manifest(
        domain_id=args.domain,
        members=[
            {"aic": provider_aic, "role": "provider"},
            {"aic": consumer_aic, "role": "consumer"},
            # 第二签名方只确认域条款，不是 token 签发者；issuers 里没有 supervisor。
            {"aic": supervisor_aic, "role": "auditor"},
        ],
        assets=[
            {"providerAic": provider_aic, "skill": skill, "allowedMembers": ["consumer"]}
            for skill in skills
        ],
        issuers=[provider_aic],  # 只有算力方可以签发
        revision=args.revision,
        status="active",
        previous=previous,
    )
    # 双签：算力方 + 域 auditor（研枢 supervisor）；研枢不签 grant 本身。
    manifest = sign_manifest(manifest, provider_signing)
    manifest = sign_manifest(manifest, supervisor_signing)
    verify_manifest(manifest, trust_bundle=trust)
    domains_dir.mkdir(parents=True, exist_ok=True)
    write_manifest(manifest, domains_dir)

    grant = issue_grant(
        provider_signing,
        domain=args.domain,
        domain_revision=args.revision,
        subject_aic=consumer_aic,
        assets=[{"providerAic": provider_aic, "skill": skill} for skill in skills],
        max_uses=args.max_uses,
        ttl_seconds=args.ttl,
    )
    grant_dir = consumer_ws / "acps" / "grants"
    grant_dir.mkdir(parents=True, exist_ok=True)
    grant_path = Path(args.out) if args.out else grant_dir / f"{args.domain}.grant.json"
    grant_path.write_text(json.dumps({
        "token": grant.token,
        "targetAic": provider_aic,
        "skill": skills[0],
        "domain": args.domain,
        "jti": grant.jti,
        "expiresAt": grant.expires_at,
        "maxUses": args.max_uses,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(grant_path, 0o600)
    except OSError:
        pass

    print(f"[domain] {args.domain} r{args.revision} -> {domains_dir / (args.domain + '.r' + str(args.revision) + '.json')}")
    print(f"[issuer] {provider_aic}")
    print(f"[subject] {consumer_aic}")
    print(f"[assets] {skills}")
    print(f"[grant] jti={grant.jti[:12]}... maxUses={args.max_uses} ttl={args.ttl}s")
    print(f"[grant-file] {grant_path} (0600)")
    print("[mode] grant issuer = provider; supervisor 只签域章程，不签调用 token")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
