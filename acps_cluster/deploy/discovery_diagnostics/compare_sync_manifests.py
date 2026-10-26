"""本地 ↔ 服务器 同步审计（只读）：比对两份文件清单，按影响分级输出。

用法：
    python compare_sync_manifests.py --local manifest.local.json --server manifest.server.json
可选：
    --deploy-guard  只输出"部署红线"部分（部署前跑这个当门禁）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# 影响分级的关键规则（路径前缀 → 级别/说明）
RULES: list[tuple[str, str, str]] = [
    ("acps_cluster/delegation/", "P0", "委托授权/PEP/域机制（服务器没有 → 相关能力未上线）"),
    ("acps_cluster/partner_", "P0", "partner 主程序（服务器跑的是旧版）"),
    ("acps_cluster/supervisor.py", "P0", "leader 主程序"),
    ("acps_cluster/cluster_common.py", "P0", "公共装配（注册/配置/补丁）"),
    ("acps_cluster/cluster_roster.py", "P0", "集群名册（leader 调度依据）"),
    ("acps_cluster/discovery_metadata.py", "P1", "发现门面登记表"),
    ("acps_cluster/capabilities/", "P1", "能力实现（被 partner 调用）"),
    ("capabilities/", "P1", "能力实现（顶层目录）"),
    ("acps_cluster/llm_tools.py", "P1", "LLM 调用层"),
    ("acps_cluster/aip_guard.py", "P1", "入站内容外壳化"),
    ("acps_cluster/citation_verify.py", "P1", "引用核验执行层"),
    ("acps_cluster/gpu_runner.py", "P1", "沙盒执行层"),
    ("acps_cluster/hpc_runner.py", "P1", "HPC 执行层"),
    ("acps_cluster/kb_runner.py", "P1", "知识库执行层"),
    ("acps_cluster/kb_store.py", "P0", "知识库存储层（partner_kb 直接 import，缺文件即起不来）"),
    ("acps_cluster/kb_admin.py", "P2", "知识库建库/迁移/语料导入入口"),
    ("acps_cluster/eval_runner.py", "P1", "评测执行层"),
    ("acps_cluster/tests/", "P2", "测试"),
    ("acps_cluster/deploy/", "P2", "部署与运维脚本"),
    ("acps_cluster/demo_run/", "P3", "演示产物"),
    ("docs/", "P2", "文档"),
]


def level_of(path: str) -> tuple[str, str]:
    for prefix, level, why in RULES:
        if path.startswith(prefix) or path == prefix.rstrip("/"):
            return level, why
    return "P3", "其他"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--deploy-guard", action="store_true")
    args = ap.parse_args()

    loc = json.loads(Path(args.local).read_text(encoding="utf-8"))
    srv = json.loads(Path(args.server).read_text(encoding="utf-8"))
    L, S = loc["files"], srv["files"]

    only_local = sorted(set(L) - set(S))
    only_server = sorted(set(S) - set(L))
    both = sorted(set(L) & set(S))
    differ = [p for p in both if L[p]["sha256"] != S[p]["sha256"]]
    same = [p for p in both if L[p]["sha256"] == S[p]["sha256"]]

    print("=" * 82)
    print(f"本地清单：{loc.get('label')} / {len(L)} 个文件")
    print(f"服务器清单：{srv.get('label')} / {len(S)} 个文件")
    print(f"仅在本地 {len(only_local)} | 仅在服务器 {len(only_server)} | "
          f"两边都有但内容不同 {len(differ)} | 完全一致 {len(same)}")

    groups: dict[str, list[str]] = {}
    for p in only_local + differ:
        lvl, why = level_of(p)
        groups.setdefault(lvl, []).append(p)

    if args.deploy_guard:
        print("\n【部署红线】" + ("（下面这些文件服务器缺或与本地不一致）" if groups.get("P0") else "无 P0 差异"))
        for p in sorted(groups.get("P0", [])):
            tag = "缺失" if p in only_local else "内容不同"
            print(f"  [{tag}] {p}")
        return 0

    for lvl, title in (("P0", "P0 功能缺失/漂移（服务器上不存在或版本不同 → 能力不可用）"),
                       ("P1", "P1 支撑代码差异（可能导致行为不一致）"),
                       ("P2", "P2 测试/部署脚本/文档差异"),
                       ("P3", "P3 其他（演示产物、附属文件）")):
        items = sorted(groups.get(lvl, []))
        if not items:
            continue
        print("\n" + "=" * 82)
        print(f"### {title}  共 {len(items)} 个")
        # 按顶层目录归并，便于扫读
        buckets: dict[str, list[str]] = {}
        for p in items:
            key = "/".join(p.split("/")[:2]) if "/" in p else p
            buckets.setdefault(key, []).append(p)
        for key in sorted(buckets):
            ps = buckets[key]
            state_missing = sum(1 for p in ps if p in only_local)
            state_diff = len(ps) - state_missing
            print(f"\n  ▸ {key}  （缺失 {state_missing} / 不一致 {state_diff}）")
            for p in ps[:12]:
                tag = "缺" if p in only_local else "≠"
                print(f"      [{tag}] {p}")
            if len(ps) > 12:
                print(f"      …… 另有 {len(ps) - 12} 个同类")

    print("\n" + "=" * 82)
    print(f"### 仅在服务器上（本地没有 → 服务器手工改动或历史遗留）共 {len(only_server)} 个")
    for p in only_server[:40]:
        print(f"  [服务器独有] {p}")
    if len(only_server) > 40:
        print(f"  …… 另有 {len(only_server) - 40} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
