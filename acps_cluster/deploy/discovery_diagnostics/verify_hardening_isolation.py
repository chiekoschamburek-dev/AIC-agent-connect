#!/usr/bin/env python3
"""核实：现有 8 个 agent 的 PEP 口径与"加固只作用于三个新 agent"的隔离性。

## 为什么要查

`.env.production` 是 8 个服务**共享**的（unit 里 `EnvironmentFile=` 指向同一个文件）。
若把 `ACPS_PEP_LEADER_DIRECT=0` 写进**共享**文件，会导致
**leader 连现有 8 个伙伴也必须持令牌** → 调度链直接断（现有委派都是纯文本直调）。

正确做法：只写在**每个资源 agent 自己的** `/etc/wit-<agent>-pep.env` 里。

本脚本核实：
  1. 三个新 agent 的加固配置**不在**共享文件里；
  2. 现有 8 个 agent 的 `leader_direct_open` 仍为 True（白名单直调可用）；
  3. 按 agent 单独的 env 文件是否已就位（finish 脚本会写）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

CL = Path("/opt/wit/my-project/acps_cluster")
ENVFILE = CL / "deploy" / ".env.production"

print("=== 1) 共享 .env.production 里是否有 ACPS_PEP_LEADER_DIRECT ===")
lines = ENVFILE.read_text(encoding="utf-8").splitlines()
found = []
for i, ln in enumerate(lines, 1):
    s = ln.strip()
    if s.startswith("#"):
        continue
    if "ACPS_PEP_LEADER_DIRECT" in s:
        found.append((i, s))
if found:
    for i, s in found:
        print(f"  ⚠ 第 {i} 行: {s}")
    print("  ⚠ 共享文件里设置该项会**误伤现有 8 个 agent**（leader 直调被要求令牌）！")
else:
    print("  ✔ 共享文件里没有——加固只会在按 agent 的 /etc/wit-<agent>-pep.env 里生效")

print()
print("=== 2) 现有 8 个 agent 的 PEP 口径（应为 direct_open=True）===")
sys.path.insert(0, str(CL.parent))
sys.path.insert(0, str(CL))
os.environ["YANSHU_RUN_TRACE"] = "0"
from delegation.pep import load_pep_config  # noqa: E402

for w in ("scout", "writer", "critic", "synthesis",
          "contradiction", "frontier", "experiment", "supervisor"):
    ws = CL / f"ws-{w}"
    if not (ws / "acps").is_dir():
        print(f"  ws-{w:14s} 【无工作区】")
        continue
    try:
        cfg = load_pep_config(ws)
    except Exception as exc:  # noqa: BLE001
        print(f"  ws-{w:14s} 读取失败 {type(exc).__name__}: {exc}")
        continue
    print(f"  ws-{w:14s} enabled={cfg.enabled} direct_open={cfg.leader_direct_open} "
          f"leader_aics={len(cfg.leader_aics)} 公开能力={len(cfg.public_skills)}")

print()
print("=== 3) 按 agent 的 env 文件状态 ===")
for w in ("cpu", "gpu", "kb"):
    f = Path(f"/etc/wit-{w}-pep.env")
    if f.is_file():
        print(f"  {f}: 存在")
        for ln in f.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s and not s.startswith("#"):
                print(f"      {s}")
    else:
        print(f"  {f}: 【不存在】（审核通过后排服务时由 finish_resource_agents.sh 写入）")
print("  --- 对照：现有的一个（scout）")
f = Path("/etc/wit-scout-pep.env")
print(f"  {f}: {'存在' if f.is_file() else '不存在'}")

print()
print("=== 4) 结论 ===")
print("  加固配置（LEADER_AICS=supervisor + LEADER_DIRECT=0）**只写进资源 agent 自己的 env**，")
print("  共享 .env.production 不动 → 现有 8 个 agent 的直调行为完全不变。")
