"""从代码派生新的 ACS 填报稿（只用于重新提交平台，不联网、不改任何现网文件）。

做法（对齐项目既有约定"ACS 由代码派生，手填稿漂移是故障根因"）：

1. 本地构建 `partner_<key>.py` 里的真实 WitAgent；
2. 取 `Capability.to_acps_skill()` 的投影（**平台将看到的就是它**）；
3. 用一份**已过审的现网 ACS**（`--like`，默认取平台公开 ACS 快照）作为载体，
   只替换 `description` / `skills` / `version` / `lastModifiedTime`，
   `endPoints` / `certificate` / `provider` / `capabilities` / 安全方案等一律原样保留；
4. 用 registry 的 `acsSchema.json` 做 jsonschema 校验（不传 format_checker，复刻平台行为），
   再用 `deploy/validate_acs.py` 的隐形字符/端点规则复核。

skill 的 `visibility` / `domains` 会原样进入填报稿（registry schema 已放开这两个字段）：
`visibility=domain` 的技能若缺 domains 会被这里直接拒绝，防止"代码里有、平台看不到"。

用法：
    python acps_cluster/deploy/make_acs.py scout \
        --like acps_cluster/ws-scout/acps/acs.final.json \
        --out acps_cluster/acs.scout.resubmit.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
REPO = CLUSTER.parents[1]
SCHEMA = REPO / "ACPs-community/registry-server/app/agent/acsSchema.json"

# key → (模块名, 构建函数名, 显示名)
AGENTS: dict[str, tuple[str, str]] = {
    "supervisor": ("supervisor", "build_supervisor_agent"),
    "scout": ("partner_scout", "build_scout_agent"),
    "writer": ("partner_writer", "build_writer_agent"),
    "synthesis": ("partner_synthesis", "build_synthesis_agent"),
    "critic": ("partner_critic", "build_critic_agent"),
    "frontier": ("partner_frontier", "build_frontier_agent"),
    "contradiction": ("partner_contradiction", "build_contradiction_agent"),
    "experiment": ("partner_experiment", "build_experiment_agent"),
    "gpu": ("partner_gpu", "build_gpu_agent"),
    "cpu": ("partner_cpu", "build_cpu_agent"),
    "model": ("partner_model", "build_model_agent"),
    "eval": ("partner_eval", "build_eval_agent"),
    "kb": ("partner_kb", "build_kb_agent"),
    "hpc": ("partner_hpc", "build_hpc_agent"),
    "citation": ("partner_citation", "build_citation_agent"),
}

INVISIBLE = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0", "\u2060")


SKILL_KEYS = (
    "id", "name", "description", "version", "tags", "examples", "inputModes", "outputModes",
    # 三层能力的声明通道：public/非公开由 PEP 的 public 标志决定，visibility=domain
    # 是发现层的会员制。registry 的 AgentSkill 是 additionalProperties:false，所以
    # 这两个键必须同时出现在本白名单和 registry schema 里，否则会被静默剥掉。
    "visibility", "domains",
)
_DOMAIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def build_skills(agent, carrier_skills: list[dict], agent_version: str) -> list[dict]:
    """把能力投影成平台 schema 要求的 skill 对象。

    registry 的 AgentSkill 是 `additionalProperties: false` 且 required =
    id/name/description/version/tags：wit 的 `to_acps_skill()` 在能力未声明 version 时
    会**省掉 version**（例：arxiv.fetch），直接提交会被平台驳回。这里按
    「能力自带 version → 旧 ACS 同 id 的 version → agent 版本」补齐，并只保留 schema 允许的键。
    """
    old_by_id = {s.get("id"): s for s in (carrier_skills or [])}
    out: list[dict] = []
    for item in list(getattr(agent, "capabilities", []) or []):
        cap = getattr(item, "capability", item)
        skill = cap.to_acps_skill()
        if skill is None:
            continue
        sid = skill.get("id")
        cleaned = {k: v for k, v in skill.items() if k in SKILL_KEYS}
        old = old_by_id.get(sid) or {}
        visibility = str(cleaned.get("visibility") or "").strip().lower()
        if visibility:
            if visibility not in {"public", "domain"}:
                raise SystemExit(f"skill {sid} visibility={visibility!r}（只允许 public|domain）")
            cleaned["visibility"] = visibility
        elif str(old.get("visibility") or "").strip().lower() == "domain":
            # 注册版是域可见、代码却没声明：宁可拒绝重提，也不能静默把会员制技能
            # 降级成对所有人可见。要撤回域可见性就显式写 visibility="public"。
            raise SystemExit(
                f"skill {sid} 注册版是 visibility=domain，但代码未声明——"
                "拒绝静默降级；如确要公开请显式声明 visibility=\"public\""
            )
        if visibility == "domain":
            domains_raw = cleaned.get("domains")
            if not isinstance(domains_raw, (list, tuple)) or not domains_raw:
                raise SystemExit(f"skill {sid} visibility=domain 必须声明非空 domains")
            domains: list[str] = []
            for domain in domains_raw:
                text = str(domain or "").strip()
                if not text or not _DOMAIN_ID_RE.fullmatch(text):
                    raise SystemExit(f"skill {sid} 的域 id 不合法: {domain!r}")
                if text not in domains:
                    domains.append(text)
            cleaned["domains"] = domains
        elif "domains" in cleaned:
            raise SystemExit(f"skill {sid} 声明了 domains 但没有 visibility=domain")
        cleaned.setdefault("version", old.get("version") or agent_version)
        cleaned.setdefault("tags", [])
        cleaned.setdefault("examples", [])
        missing = [k for k in ("id", "name", "description", "version", "tags") if not cleaned.get(k)]
        if missing:
            raise SystemExit(f"skill {sid} 缺必需字段 {missing}（平台 schema 会驳回）")
        out.append({k: cleaned[k] for k in SKILL_KEYS if k in cleaned})
    return out


def check_invisible(payload: object, path: str = "$") -> list[str]:
    bad: list[str] = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            bad += check_invisible(v, f"{path}.{k}")
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            bad += check_invisible(v, f"{path}[{i}]")
    elif isinstance(payload, str):
        for ch in INVISIBLE:
            if ch in payload:
                bad.append(f"{path} 含隐形字符 U+{ord(ch):04X}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从代码派生 ACS 填报稿")
    ap.add_argument("key", choices=sorted(AGENTS))
    ap.add_argument("--like", required=True, help="载体 ACS（已过审的现网 ACS 快照）")
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--version",
        default=None,
        help=(
            "覆盖 version（默认取 agent 自己声明的版本）。"
            "注意 registry 的 (name, version) 在活跃记录间必须唯一："
            "老记录 APPROVED 不可改，重新注册时建议抬版本号，避免撞车。"
        ),
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印差异，不写文件")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(CLUSTER))
    sys.path.insert(0, str(CLUSTER.parent))
    module_name, builder_name = AGENTS[args.key]
    module = __import__(module_name)
    agent = getattr(module, builder_name)(None)

    carrier = json.loads(Path(args.like).read_text(encoding="utf-8"))
    version = str(args.version or getattr(agent, "version", "") or carrier.get("version") or "1.0.0")
    new_skills = build_skills(agent, carrier.get("skills") or [], version)
    desc = str(getattr(agent, "description", "") or "")

    acs = dict(carrier)
    old_desc, old_skills, old_version = acs.get("description"), acs.get("skills"), acs.get("version")
    acs["description"] = desc
    acs["skills"] = new_skills
    acs["version"] = version
    acs["lastModifiedTime"] = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat()
    # 提交稿按项目约定留空 aic：registry 会分配/覆盖（现网 ACS 里的 aic 是平台分配的产物）
    acs["aic"] = ""

    print(f"agent = {getattr(agent, 'display_name', '?')}  ({module_name}.{builder_name})")
    print(f"载体   = {args.like}")
    print(f"description {len(old_desc or '')} 字 → {len(desc)} 字")
    print(f"version     {old_version} → {version}")
    print("skills:")
    old_ids = [s.get("id") for s in (old_skills or [])]
    new_ids = [s.get("id") for s in new_skills]
    for sid in new_ids:
        s = next(x for x in new_skills if x["id"] == sid)
        flag = "新增" if sid not in old_ids else "更新"
        print(
            f"  [{flag}] {sid:28s} 描述 {len(s.get('description') or ''):3d} 字  "
            f"tags {len(s.get('tags') or []):2d}  examples {len(s.get('examples') or []):2d}  name={s.get('name')}"
        )
    for sid in old_ids:
        if sid not in new_ids:
            print(f"  [移除] {sid}")

    # 校验 1：jsonschema（不传 format_checker，复刻 registry 行为）
    import jsonschema  # noqa: PLC0415

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(instance=acs, schema=schema)
        print("schema 校验：PASS")
    except jsonschema.ValidationError as exc:
        print(f"schema 校验：FAIL → {exc.message} @ {list(exc.path)}")
        return 1

    # 校验 2：隐形字符
    bad = check_invisible(acs)
    print("隐形字符：PASS" if not bad else "隐形字符：FAIL\n  " + "\n  ".join(bad))
    if bad:
        return 1

    # 校验 3：端点安全方案引用
    schemes = acs.get("securitySchemes") or {}
    for ep in acs.get("endPoints") or []:
        secs = ep.get("security") or []
        assert any("mtls" in s for s in secs), f"端点 {ep.get('url')} 未引用 mtls 方案"
    assert "mtls" in schemes, "securitySchemes 缺 mtls"
    print("端点/安全方案：PASS")

    text = json.dumps(acs, ensure_ascii=False, indent=2) + "\n"
    if args.dry_run:
        print("\n（--dry-run，未写文件）")
        return 0
    out = Path(args.out)
    out.write_text(text, encoding="utf-8")
    print(f"\n已写出：{out}  ({len(text)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
