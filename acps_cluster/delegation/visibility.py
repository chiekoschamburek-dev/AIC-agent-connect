"""Phase D 域可见性过滤（RFC Part C / CDAP Phase D）的本仓实现。

语义（可见性是**元数据层**收窄，不是访问控制——真正的准入永远在 PEP）：
- skill 无 `visibility` 字段 → `public`（默认，存量数据零影响）；
- `visibility == "domain"` → 仅当调用方是 `domains` 中任一域的成员时出现在发现结果里；
- agent 过滤后无可见 skill → 整个 agent 不出现（不泄露存在性）；
- 无调用方身份（匿名查询）→ 只见 public；
- 策略投影（`export_policy_projection` 产出的 JSON）只做哑读取，签名验证在导出方完成，
  发现服务端不重复实现密码学。

与 ACPs-community/discovery-server 的 `app/discovery/domain_policy.py` 保持同一套
纯谓词语义；本模块让 yanshu-AIC 侧可以离线验证/导出/演示，不依赖兄弟仓路径。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICY_DIR_ENV = "DOMAIN_POLICY_DIR"
OVERFETCH_FACTOR = 3
_FETCH_LIMIT_CAP = 50

_POLICY_CACHE: dict[str, tuple[tuple[int, int], dict[str, dict[str, list[str]]]]] = {}


def policy_directory(directory: Path | str | None = None) -> Path | None:
    """显式目录 > DOMAIN_POLICY_DIR；都没有则返回 None（特性关闭）。"""
    if directory is not None:
        return Path(directory)
    raw = str(os.getenv(POLICY_DIR_ENV) or "").strip()
    return Path(raw) if raw else None


def load_policy_projections(
    directory: Path | str | None = None,
) -> dict[str, dict[str, list[str]]]:
    """读策略投影目录 → {domainId: {aic: []}}（与 discovery-server 同形）。

    坏文件跳过，其余域仍生效（与发现服务的哑读取一致）。
    """
    root = policy_directory(directory)
    if root is None or not root.is_dir():
        return {}
    members: dict[str, dict[str, list[str]]] = {}
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("域策略投影 %s 不可读，已跳过: %s", path, exc)
            continue
        if not isinstance(data, Mapping):
            continue
        domain_id = str(data.get("domainId") or "").strip()
        member_map = data.get("members")
        if domain_id and isinstance(member_map, Mapping):
            members[domain_id] = {str(aic): [] for aic in member_map}
    return members


def _policy_signature(root: Path) -> tuple[int, int]:
    try:
        files = list(root.glob("*.json"))
        return (len(files), max((p.stat().st_mtime_ns for p in files), default=0))
    except OSError:
        return (-1, 0)


def cached_policies(directory: Path | str | None = None) -> dict[str, dict[str, list[str]]]:
    root = policy_directory(directory)
    if root is None:
        return {}
    signature = _policy_signature(root)
    # 显式目录与环境变量目录可能不同：缓存键带上解析后的绝对路径。
    key = str(root.resolve())
    hit = _POLICY_CACHE.get(key)
    if hit is not None and hit[0] == signature:
        return hit[1]
    policies = load_policy_projections(root)
    _POLICY_CACHE[key] = (signature, policies)
    return policies


def reload_policy_cache() -> None:
    """章程投影分发后调用（演示/测试）。"""
    _POLICY_CACHE.clear()


def _skill_entry(acs: Mapping[str, Any] | None, skill_id: str) -> Mapping[str, Any] | None:
    skills = (acs or {}).get("skills")
    if not isinstance(skills, list):
        return None
    for skill in skills:
        if isinstance(skill, Mapping) and str(skill.get("id") or "") == skill_id:
            return skill
    return None


def _is_member(table: Mapping[str, Any], domain: str, caller_aic: str) -> bool:
    domain_policy = table.get(domain)
    if isinstance(domain_policy, Mapping):
        # 投影形态 {aic: ...} 与 discovery-server 的 {aic: []} 都接受
        members = domain_policy.get("members") if "members" in domain_policy else domain_policy
        if isinstance(members, Mapping):
            return caller_aic in members
    if isinstance(domain_policy, (set, frozenset, list, tuple)):
        return caller_aic in domain_policy
    return False


def skill_visible_to(
    acs: Mapping[str, Any] | None,
    skill_id: str,
    caller_aic: str | None,
    policies: Mapping[str, Any] | None = None,
) -> bool:
    """纯谓词：该 (agent 的 acs, skill) 对该调用方是否可见。"""
    entry = _skill_entry(acs or {}, skill_id)
    if entry is None:
        return True  # 行数据缺 skill 入口（如整 agent 级结果）：不在此层拦截
    visibility = str(entry.get("visibility") or "public").strip().lower()
    if visibility != "domain":
        return True
    if not caller_aic:
        return False
    domains = entry.get("domains") or ()
    if not isinstance(domains, (list, tuple, set, frozenset)):
        return False
    table = policies if policies is not None else cached_policies()
    for domain in domains:
        if _is_member(table, str(domain), caller_aic):
            return True
    return False


def sanitize_acs(
    acs: Mapping[str, Any] | None,
    caller_aic: str | None,
    policies: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """响应体脱敏：保留的 acs.skills 只含该调用方可见的 skill（非变异副本）。"""
    body = dict(acs or {})
    skills = body.get("skills")
    if isinstance(skills, list) and skills:
        body["skills"] = [
            dict(skill)
            for skill in skills
            if isinstance(skill, Mapping)
            and skill_visible_to(body, str(skill.get("id") or ""), caller_aic, policies)
        ]
    return body


def filter_rows(
    rows: list[dict[str, Any]],
    caller_aic: str | None,
    policies: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """行级后过滤 + ACS 体脱敏：行携带 `acs`（全量）与 `skill_id`/`skillId`。"""
    kept: list[dict[str, Any]] = []
    for row in rows:
        acs = row.get("acs") or {}
        skill_id = str(row.get("skillId") or row.get("skill_id") or "")
        if skill_visible_to(acs, skill_id, caller_aic, policies):
            kept.append({**row, "acs": sanitize_acs(acs, caller_aic, policies)})
    return kept


def overfetch_limit(request_limit: int) -> int:
    """可见性启用时的取数上限：request_limit * K，钳到协议上限。"""
    return min(_FETCH_LIMIT_CAP, max(1, int(request_limit)) * OVERFETCH_FACTOR)


def write_policy_projections(
    policies: Mapping[str, Any],
    directory: Path | str,
) -> list[Path]:
    """把一个或多个章程投影写成 discovery-server 可哑读的 JSON。

    `policies` 可以是 {domainId: projection} 或单个 projection。返回值是写出的路径。
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    if "domainId" in policies and isinstance(policies.get("domainId"), str):
        items = [policies]
    else:
        items = list(policies.values())
    written: list[Path] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        domain_id = str(item.get("domainId") or "").strip()
        if not domain_id:
            continue
        path = root / f"{domain_id}.json"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(item, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        written.append(path)
    return written
