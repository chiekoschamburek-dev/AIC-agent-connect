#!/bin/sh
# 按 systemd 的真实顺序加载 env（共享 → per-agent）后复核 PEP 口径
cd /opt/wit/my-project/acps_cluster || exit 1
sudo -u wit YANSHU_RUN_TRACE=0 /opt/wit/venv/bin/python - <<'PY'
import os
import sys
from pathlib import Path

CL = Path('/opt/wit/my-project/acps_cluster')
sys.path.insert(0, str(CL.parent))
sys.path.insert(0, str(CL))

def load(path: Path) -> int:
    """模拟 systemd EnvironmentFile：后者覆盖前者。"""
    if not path.is_file():
        return 0
    n = 0
    for ln in path.read_text(encoding='utf-8').splitlines():
        s = ln.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        k, v = s.split('=', 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")
        n += 1
    return n

from delegation.pep import load_pep_config

print(f"  {'agent':14s} {'enabled':8s} {'direct_open':12s} {'leader_aics':12s} 说明")
print("  " + "-" * 72)

for w in ("scout", "writer", "critic", "synthesis", "contradiction",
          "frontier", "experiment", "supervisor", "cpu", "gpu", "kb"):
    ws = CL / f"ws-{w}"
    # 按 systemd 顺序：共享 → per-agent
    n1 = load(CL / 'deploy' / '.env.production')
    n2 = load(Path(f'/etc/wit-{w}-pep.env'))

    if not (ws / 'acps').is_dir():
        note = "（工作区尚未创建）"
        try:
            cfg = load_pep_config(ws, public_skills=frozenset(),
                                  has_non_public_skills=True)
        except Exception as exc:
            print(f"  {w:14s} —— {note} 读取失败 {type(exc).__name__}")
            continue
        print(f"  {w:14s} {str(cfg.enabled):8s} {str(cfg.leader_direct_open):12s} "
              f"{len(cfg.leader_aics):<12d} {note}")
        continue

    try:
        cfg = load_pep_config(ws)
        print(f"  {w:14s} {str(cfg.enabled):8s} {str(cfg.leader_direct_open):12s} "
              f"{len(cfg.leader_aics):<12d} env行: 共享{n1} + 专属{n2}")
    except Exception as exc:
        print(f"  {w:14s} 读取失败 {type(exc).__name__}: {exc}")

print()
print("  期望：现有 8 个 agent → enabled=True, direct_open=True（直调可用）")
print("        三个资源 agent → enabled=True, direct_open=False（leader 也需令牌）")
print("  注：/etc/wit-<agent>-pep.env 只在审核通过后由 finish_resource_agents.sh 写入。")
PY
