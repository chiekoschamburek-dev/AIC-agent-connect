#!/usr/bin/env python3
"""部署前真实验证（服务器侧）：新版代码在真实注册状态下是否还触发 ACS 指纹保护。

要点：`sys.path` 先到先得 —— 必须让**暂存区（新版）代码优先**，
线上目录只用于补足暂存区没有的包（capabilities / vendor 等）。

做法：把生产工作区的**注册状态**复制到临时工作区，用暂存区代码跑 `runtime.enroll()`，
只看返回 status 与 enrollment 里的 failure。不碰生产工作区，不进入长驻服务。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
from pathlib import Path

CL = Path("/opt/wit/my-project/acps_cluster")
STAGED = Path("/opt/wit/sync-staging")
TEST_ROOT = Path("/tmp/acs-guard-test")

CASES = {
    "contradiction": ("ws-contradiction", "ACPS_CONTRADICTION"),
    "frontier": ("ws-frontier", "ACPS_FRONTIER"),
    "critic": ("ws-critic", "ACPS_CRITIC"),
    "scout": ("ws-scout", "ACPS_SCOUT"),
    "supervisor": ("ws-supervisor", "ACPS_SUPERVISOR"),
    "experiment": ("ws-experiment", "ACPS_EXPERIMENT"),
}


def env_from_file() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (CL / "deploy" / ".env.production").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    return env


def set_paths() -> None:
    """暂存区优先，线上补足。"""
    order = [str(STAGED / "acps_cluster"), str(STAGED),
             "/opt/wit/my-project/acps_cluster", "/opt/wit/my-project"]
    for p in order:
        while p in sys.path:
            sys.path.remove(p)
    for p in reversed(order):
        sys.path.insert(0, p)


async def one(key: str, ws_name: str, prefix: str) -> bool:
    src = CL / ws_name / "acps"
    case = TEST_ROOT / key
    if case.exists():
        shutil.rmtree(case)
    (case / "acps").mkdir(parents=True)
    for name in ("enrollment.json", "acs.final.json", "acs.draft.json", "trust-bundle.pem"):
        if (src / name).is_file():
            shutil.copy2(src / name, case / "acps" / name)
    for d in ("client", "server"):
        if (src / d).is_dir():
            shutil.copytree(src / d, case / "acps" / d)

    set_paths()
    for mod_name in list(sys.modules):
        if mod_name.startswith(("partner_", "cluster_common", "discovery_metadata", "bootstrap", "supervisor")):
            del sys.modules[mod_name]

    cc = importlib.import_module("cluster_common")
    module_name = "supervisor" if key == "supervisor" else f"partner_{key}"
    mod = importlib.import_module(module_name)
    builder = [n for n in dir(mod) if n.startswith("build_") and n.endswith("_agent")][0]

    from wit import WitRuntime
    agent = getattr(mod, builder)(None)
    runtime = WitRuntime(agent, workspace=case, acps=cc.build_acps_config(0))

    reg = json.loads((case / "acps" / "acs.final.json").read_text(encoding="utf-8"))
    print(f"\n### {key}")
    print(f"    代码投影描述 {len(agent.description)} 字 / 注册版 {len(reg.get('description') or '')} 字 "
          f"→ 逐字一致={agent.description == (reg.get('description') or '')}")

    creds = cc.build_credentials(prefix.replace("ACPS_", ""))
    result = await runtime.enroll(credentials=creds)
    st = getattr(result, "status", "?")
    failure = None
    try:
        failure = json.loads((case / "acps" / "enrollment.json").read_text(encoding="utf-8")).get("failure")
    except Exception:  # noqa: BLE001
        pass
    bad = (st == "failed") or (failure == "acs_fingerprint_changed_after_submission")
    print(f"    enroll: status={st}  failure={failure}")
    print(f"    → {'✘ 仍触发指纹保护' if bad else '✔ 未触发指纹保护'}")
    return not bad


async def main() -> int:
    os.environ.update({k: v for k, v in env_from_file().items()
                       if k.startswith(("ACPS_", "OPENAI_"))})
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    results = {}
    for key, (ws, prefix) in CASES.items():
        try:
            results[key] = await one(key, ws, prefix)
        except Exception as exc:  # noqa: BLE001
            print(f"### {key} 异常：{type(exc).__name__}: {exc}")
            results[key] = False
    print("\n=== 汇总 ===")
    for k, v in results.items():
        print(f"  {k:14s} {'✔' if v else '✘'}")
    ok = all(results.values())
    print(f"\n=== 结论：{'✔ 新版代码不再触发指纹保护，可以部署' if ok else '✘ 仍有 agent 会触发，不可部署'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
