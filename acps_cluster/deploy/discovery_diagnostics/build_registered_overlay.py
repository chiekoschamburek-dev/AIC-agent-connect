#!/usr/bin/env python3
"""从"平台现网 ACS"生成注册文本覆盖表（registered_acs_overlay.json）。

## 为什么需要它
wit 有 ACS 指纹保护：**agent 注册完成后，代码投影出来的 ACS 必须与注册版逐字一致**，
否则拒绝启动（`acs_fingerprint_changed_after_submission`）。
而 `discovery_metadata` 的"发现门面"会改写声明的描述类字段 → 在"绑定已注册工作区"的服务器上必然冲突。

覆盖表的作用：把**平台侧那份权威文本**（描述 / tags / examples）固化成代码可读的数据，
让门面在"该 agent 已注册"时**直接返回注册文本**，于是：

    代码投影 ≡ 注册版  →  指纹不漂 →  守护规则永不触发 →  部署不再受顺序约束

同时门面的"用户问法"增益仍然保留在**尚未注册**的 agent 上（本地开发、新 agent 首次注册）。

## 数据来源与字段
对每个 agent 取 `_platform_snapshot/acs.live.<key>.json`（换身份时从工作区取回的现网 ACS）：

- agent 级：`description`
- skill 级：按 skill id 取 `description` / `tags` / `examples`（只取 ACS schema 允许的键）

## 用法
    python build_registered_overlay.py                 # 用 _platform_snapshot 生成
    python build_registered_overlay.py --dry-run       # 只打印将写入的内容
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

CL = Path(__file__).resolve().parents[2]           # acps_cluster
SNAP = CL / "_platform_snapshot"
OUT = CL / "registered_acs_overlay.json"

# 快照文件名 → agent 门面 id（与 discovery_metadata.AGENT_FACADES 的键一致）
KEY_TO_AGENT = {
    "scout": "yanshu.scout-partner",
    "supervisor": "yanshu.supervisor",
    "critic": "yanshu.critic-partner",
    "writer": "yanshu.writer-partner",
    "synthesis": "yanshu.synthesis-partner",
    "experiment": "yanshu.experiment-partner",
    "frontier": "yanshu.frontier-partner",
    "contradiction": "yanshu.contradiction-partner",
    # 2026-10-14 补：kb 已上服务器（8599），其注册文本也要进覆盖表，
    # 否则代码投影会退回门面文本 → 与注册版不符 → 重启撞指纹保护
    "kb": "yanshu.kb-partner",
    # 2026-10-16 补：citation 已注册（8468）并以平台身份上线，同上
    "citation": "yanshu.citation-integrity",
    # 2026-10-25 补：workspace 存储资源已注册（8470）。注册稿是平台控制台按
    # 手写 ACS 批准的（技能名是中文、没有 inputModes），代码投影必须逐字对齐，
    # 否则重生成覆盖表时这条会被漏掉、重启撞指纹保护。
    "workspace": "yanshu.workspace-partner",
}
SKILL_KEYS = ("id", "name", "description", "version", "tags", "examples", "inputModes", "outputModes")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    agents: dict[str, dict] = {}
    skills: dict[str, dict] = {}
    skills_by_agent: dict[str, dict] = {}
    sources: dict[str, str] = {}

    for key, agent_id in KEY_TO_AGENT.items():
        f = SNAP / f"acs.live.{key}.json"
        if not f.is_file():
            print(f"  [跳过] 没有快照：{f.name}")
            continue
        acs = json.loads(f.read_text(encoding="utf-8"))
        agents[agent_id] = {
            "aic": acs.get("aic"),
            "version": acs.get("version"),
            "description": acs.get("description") or "",
        }
        per_agent = skills_by_agent.setdefault(agent_id, {})
        for s in acs.get("skills") or []:
            sid = s.get("id")
            if not sid:
                continue
            fields = {k: s[k] for k in SKILL_KEYS if k in s}
            # 保留既有叠加表形状：skill 记录带 agentId，便于人工核对归属。
            fields["agentId"] = agent_id
            per_agent[sid] = fields
            # 扁平表改"首个写入者优先"：跨 agent 同名技能（critic 与 citation 都声明
            # citation.verify，2026-10-16 实测）时保住既有 agent 的注册文本；
            # 消歧请用 skillsByAgent（消费侧 apply_to_capabilities(..., agent_id=…)）。
            skills.setdefault(sid, fields)
        sources[agent_id] = f"acs.live.{key}.json（v{acs.get('version')}, aic=…{(acs.get('aic') or '')[-12:]}）"
        print(f"  {key:14s} → {agent_id:28s} 描述 {len(acs.get('description') or ''):4d} 字 / "
              f"skills {[s.get('id') for s in (acs.get('skills') or [])]}")

    payload = {
        "_comment": (
            "注册文本覆盖表：由 build_registered_overlay.py 从平台现网 ACS 生成。"
            "discovery_metadata 在生成声明时会优先采用本表的文本，"
            "以保证「代码投影 ≡ 平台注册版」，避免触发 wit 的 ACS 指纹保护。"
            "重新注册（换 AIC/版本）后需重新生成本文件。"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "my-project/acps_cluster/_platform_snapshot/acs.live.*.json",
        "agents": agents,
        "skills": skills,
        "skillsByAgent": skills_by_agent,
    }
    # 保持仓库既有的一空格缩进与 LF，避免在 Windows 上生成整文件 diff。
    text = json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
    print(f"\nagent 覆盖 {len(agents)} 个 / skill 覆盖 {len(skills)} 个"
          f"（按 agent 命名空间 {sum(len(v) for v in skills_by_agent.values())} 个），"
          f"共 {len(text)} 字节")
    for agent_id, src in sources.items():
        print(f"  来源 {agent_id:28s} {src}")
    if args.dry_run:
        print("\n（--dry-run，未写文件）")
        return 0
    with OUT.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"\n已写出：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
