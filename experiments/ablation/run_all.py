#!/usr/bin/env python3
"""一条命令复跑 ①②③④ 四组对照实验（外加 ④b 规模化分布）。

    cd yanshu-AIC
    python experiments/ablation/run_all.py

全部实验自包含：运行时临时铸造 Ed25519 证书链，不读仓库 dev CA 私钥、不连网、
不起 Docker，因此干净克隆可直接复跑；原始数据落在 `experiments/ablation/out/`。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

SCRIPTS = ["exp1_pep.py", "exp2_domain.py", "exp3_workspace.py",
           "exp4_guard.py", "exp4b_guard_scale.py"]

if __name__ == "__main__":
    failed = []
    for s in SCRIPTS:
        print(f"\n{'#' * 78}\n# 运行 {s}\n{'#' * 78}", flush=True)
        rc = subprocess.run([PY, str(HERE / s)], cwd=HERE.parent.parent).returncode
        if rc != 0:
            failed.append(s)
    print("\n" + "=" * 78)
    if failed:
        print(f"以下实验未达预期（返回非 0）：{', '.join(failed)}")
        raise SystemExit(1)
    print("五组对照实验全部完成（①②④③ + ④b 规模化分布）。原始数据见 experiments/ablation/out/。")
