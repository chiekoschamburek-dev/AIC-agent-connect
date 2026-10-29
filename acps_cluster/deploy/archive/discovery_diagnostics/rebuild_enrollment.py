#!/usr/bin/env python3
"""重建被写坏的工作区 enrollment（结构取自健康参照，身份值取自平台记录 + 本地证件）。

背景：contradiction / frontier 的 `enrollment.json` 丢失了 identity / review /
certificateIssue / finalAcsPath 等字段（变成 None），wit 于是把它当作"未注册"重新走流程，
撞上远端已存在的记录 → 报"delete or finish the remote registration"。

做法（确定性、可复验）：
1. 取健康参照（ws-critic）的 enrollment 作为**结构模板**；
2. 用目标工作区的 agentId / 平台端口 / AIC / 平台 ACS 字段做替换；
3. 证书相关路径按目标工作区重写；
4. 指纹按目标工作区"当前代码投影"重算（用 wit 自己的 `_fingerprint`）；
5. 清掉失败态键。

用法：
    python rebuild_enrollment.py --ws ws-contradiction --template ws-critic [--apply]
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

CL = Path("/opt/wit/my-project/acps_cluster")
FAILURE_KEYS = ("failure", "newAcsFingerprint", "newAcpsConfigFingerprint",
                "previousAcsFingerprint", "previousAcpsConfigFingerprint",
                "candidateDraftPath", "remoteRegistration", "reconciliation")

# 工作区 → (agentId, 平台端口)
META = {
    "ws-contradiction": ("yanshu.contradiction-partner", 8449),
    "ws-frontier": ("yanshu.frontier-partner", 8450),
    "ws-critic": ("yanshu.critic-partner", 8448),
    "ws-writer": ("yanshu.writer-partner", 8447),
    "ws-synthesis": ("yanshu.synthesis-partner", 8446),
    "ws-experiment": ("yanshu.experiment-partner", 8461),
    "ws-scout": ("yanshu.scout-partner", 8445),
    "ws-supervisor": ("yanshu.supervisor", 8444),
    "ws-citation": ("yanshu.citation-integrity", 8468),
}


def sub_paths(obj, old_ws: str, new_ws: str, old_port: int, new_port: int):
    """递归把路径里的工作区名与端口换掉。"""
    if isinstance(obj, dict):
        return {k: sub_paths(v, old_ws, new_ws, old_port, new_port) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sub_paths(v, old_ws, new_ws, old_port, new_port) for v in obj]
    if isinstance(obj, str):
        s = obj.replace(f"/{old_ws}/", f"/{new_ws}/")
        s = s.replace(f":{old_port}/rpc", f":{new_port}/rpc")
        return s
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ws", required=True)
    ap.add_argument("--template", default="ws-critic")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    target = CL / args.ws / "acps"
    tmpl = CL / args.template / "acps"
    tpl_enr = json.loads((tmpl / "enrollment.json").read_text(encoding="utf-8"))
    cur_enr = json.loads((target / "enrollment.json").read_text(encoding="utf-8"))
    live_acs = json.loads((target / "acs.final.json").read_text(encoding="utf-8"))

    agent_id, new_port = META[args.ws]
    tpl_agent, tpl_port = META[args.template]
    aic = live_acs.get("aic") or (cur_enr.get("identity") or {}).get("ref", {}).get("aic")

    new = copy.deepcopy(tpl_enr)
    new = sub_paths(new, args.template, args.ws, tpl_port, new_port)
    new["agentId"] = agent_id
    new["status"] = "active"
    # 身份：AIC 换成本工作区的
    for path in (("identity", "ref"), ("review",), ("review", "authoritativeAcs")):
        node = new
        for k in path:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and "aic" in node:
            node["aic"] = aic
    if isinstance(new.get("review"), dict):
        new["review"].setdefault("status", "approved")
        new["review"]["authoritativeAcs"] = live_acs
    for k in FAILURE_KEYS:
        new.pop(k, None)

    print(f"### {args.ws} ← 模板 {args.template}")
    print(f"    agentId: {cur_enr.get('agentId')} → {agent_id}")
    print(f"    aic    : {aic}")
    print(f"    端口   : {tpl_port} → {new_port}")
    print(f"    当前缺失的关键字段：{[k for k in ('identity','review','certificateIssue','finalAcsPath','finalAcsSource') if not cur_enr.get(k)]}")
    for k in ("identity", "review", "certificateIssue", "finalAcsPath", "finalAcsSource"):
        v = new.get(k)
        print(f"    重建后 {k:18s} = {'✔ 有' if v else '✘ 空'}"
              + (f"（{len(v)} 项）" if isinstance(v, (dict, list)) else f"（{v}）" if v else ""))

    if not args.apply:
        print("    （dry-run，未写文件）")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(target / "enrollment.json", target / f"enrollment.json.pre-rebuild-{stamp}")
    (target / "enrollment.json").write_text(
        json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"    已写回（备份 enrollment.json.pre-rebuild-{stamp}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
