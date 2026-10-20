#!/usr/bin/env python3
"""把已验签的域章程导出为 discovery-server 可哑读的**策略投影**目录。

背景：Phase D 的可见性过滤在平台侧 discovery-server 执行（读 `DOMAIN_POLICY_DIR`）。
服务器不重复实现验签——持双签章程的一方用本脚本导出 `{domainId, members, assets}`，
再把输出目录交给发现服务。签名验证在这里完成；发现服务只做成员判定。

用法：
    python acps_cluster/deploy/export_domain_projections.py \
        --domains-dir acps_cluster/ws-gpu/acps/domains \
        --trust-bundle acps_cluster/ws-gpu/acps/trust-bundle.pem \
        --out acps_cluster/policy-projections
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CLUSTER.parent))
sys.path.insert(0, str(CLUSTER))

from delegation.domain_manifest import (  # noqa: E402
    export_policy_projection,
    load_domain_policies,
)
from delegation.visibility import write_policy_projections  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出域章程投影（discovery 可见性过滤用）")
    ap.add_argument("--domains-dir", required=True, help="章程目录（含 *.rN.json）")
    ap.add_argument("--trust-bundle", required=True, help="验签用 trust-bundle")
    ap.add_argument("--out", required=True, help="投影输出目录（DOMAIN_POLICY_DIR）")
    ap.add_argument("--domain", action="append", default=[],
                    help="只导出指定 domainId（可多次；默认全部）")
    args = ap.parse_args(argv)

    policies = load_domain_policies(args.domains_dir, trust_bundle=args.trust_bundle)
    if args.domain:
        wanted = set(args.domain)
        policies = {d: p for d, p in policies.items() if d in wanted}
        missing = wanted - set(policies)
        if missing:
            print(f"✘ 没有可用策略: {sorted(missing)}（验签失败/不存在）")
            return 2
    if not policies:
        print(f"✘ {args.domains_dir} 下没有可导出的域策略")
        return 2

    projections = {d: export_policy_projection(p) for d, p in policies.items()}
    written = write_policy_projections(projections, args.out)
    for domain, policy in sorted(policies.items()):
        print(f"  {domain}: revision={policy.revision} status={policy.status} "
              f"members={len(policy.members)} assets={len(policy.assets)}")
    print(f"✔ 已导出 {len(written)} 份策略投影到 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
