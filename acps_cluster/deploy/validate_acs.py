"""ACS 提交门禁：用 registry-server 的同款校验逻辑预先裁决，避免平台驳回。

校验四段，全部对齐 registry 的真实代码路径（不是我的推断）：
1. jsonschema —— 直接加载 registry 的 `app/agent/acsSchema.json`，且**不传 format_checker**
   （复刻 `app/utils/acs.py::_validate_acs_schema`，故 `format:uri/email` 不生效这一事实
   在这里同样成立：脏邮箱/脏 iconUrl 不会在这里报错，但零宽字符落在 endPoints[].url
   会在第 2 段炸——历史上就是这么被驳回一次的）；
2. endPoints —— 复刻 `_validate_endpoint_security`（每个端点必须引用一个 mutualTLS 方案，
   比规范更严）与 `check_url_format`（urlparse 须有 scheme+netloc，且 scheme 匹配 transport 映射表）；
3. 隐形字符 —— 逐码点扫描 U+200B/U+200C/U+200D/U+FEFF/U+00A0 等（剪贴板污染防线）；
4. 代码侧一致性 —— skills 的 id 集合必须等于 `partner_<key>.py` 里 `@capability` 声明的
   acps_skill 集合（ACS 由代码派生，手填稿漂移就是"名册/计划闸门"那类老故障的根因）；
   有参数的能力必须声明 inputModes（wit 由签名推导，缺了说明手填时删错了字段）。

用法（用 registry 的 venv，它装了 jsonschema）：
    ACPs-community/registry-server/.venv/Scripts/python.exe acps_cluster/deploy/validate_acs.py acps_cluster/acs.gpu-partner.filled.json
    ... validate_acs.py <acs.json> --module partner_kb   # 含域可见性/域名一致性检查
    ... validate_acs.py --all          # 校验三份填报稿 + 已过审批次做对照组
退出码：0 全通过，1 有 FAIL。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

CLUSTER = Path(__file__).resolve().parents[1]
REPO = CLUSTER.parents[1]
REGISTRY_SCHEMA = REPO / "ACPs-community/registry-server/app/agent/acsSchema.json"

# registry app/utils/acs.py::_TRANSPORT_SCHEME_MAP 的副本（同源，改官方要同步）
TRANSPORT_SCHEME_MAP = {
    "AMQP": {"amqp", "amqps"},
    "GRPC": {"grpc", "grpcs", "http", "https"},
    "HTTP_JSON": {"http", "https"},
    "JSONRPC": {"http", "https"},
    "REST": {"http", "https"},
}

TOP_REQUIRED = [
    "aic", "active", "lastModifiedTime", "protocolVersion", "name", "description",
    "version", "provider", "securitySchemes", "endPoints", "capabilities",
    "defaultInputModes", "defaultOutputModes", "skills",
]

INVISIBLE = {
    "​": "零宽空格 ZWSP",
    "‌": "零宽不连字 ZWNJ",
    "‍": "零宽连字 ZWJ",
    "﻿": "BOM/零宽无破块 ZWNBSP",
    " ": "不换行空格 NBSP",
    "⁠": "词连接符 FSWP",
    "ㅤ": "Hangul 填充符",
}


class Report:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fails: list[str] = []
        self.warns: list[str] = []

    def fail(self, msg: str) -> None:
        self.fails.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def render(self) -> str:
        head = f"✗ {self.path.name}" if self.fails else f"✓ {self.path.name}"
        body = [f"    FAIL {m}" for m in self.fails] + [f"    warn {m}" for m in self.warns]
        return "\n".join([head, *body])


def check_schema(acs: dict, report: Report) -> None:
    try:
        from jsonschema import ValidationError, validate as json_validate
    except ImportError:
        report.warn("jsonschema 未安装：跳过 schema 段（请用 registry-server 的 venv 运行）")
        return
    schema = json.loads(REGISTRY_SCHEMA.read_text(encoding="utf-8"))
    try:
        json_validate(instance=acs, schema=schema)   # 与 registry 一致：不传 format_checker
    except ValidationError as exc:
        report.fail(f"INVALID_ACS（jsonschema）: Json path [ {exc.json_path} ]; {exc.message}")


def check_endpoints(acs: dict, report: Report) -> None:
    schemes = {
        name
        for name, scheme in (acs.get("securitySchemes") or {}).items()
        if isinstance(scheme, dict) and scheme.get("type") == "mutualTLS"
    }
    if not schemes:
        report.fail("securitySchemes 里没有 type=mutualTLS 的方案")
    for i, endpoint in enumerate(acs.get("endPoints") or []):
        securities = endpoint.get("security") or []
        names = {k for item in securities for k in item}
        if not (names & schemes):
            report.fail(f"endPoints[{i}] 必须引用 mutualTLS 方案（registry 硬性，规范里 security 才是可选）")
        url = endpoint.get("url") or ""
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            report.fail(f"endpoint URL format: {url!r}（urlparse 缺 scheme 或 netloc）")
            continue
        allowed = TRANSPORT_SCHEME_MAP.get((endpoint.get("transport") or "").upper())
        if allowed and parsed.scheme not in allowed:
            report.fail(f"endPoints[{i}] scheme={parsed.scheme!r} 与 transport={endpoint.get('transport')!r} 不匹配")
        host = parsed.hostname or ""
        if host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".local"):
            report.warn(f"endPoints[{i}] 是本机地址 {host}：机器闸门能过，公网不可达会被人工审核驳回")
        if not parsed.path.rstrip("/"):
            report.warn(f"endPoints[{i}] 缺路径段（wit 的 AIP 端点是 /rpc）")


def check_invisible(raw: str, report: Report) -> None:
    for offset, char in enumerate(raw):
        label = INVISIBLE.get(char)
        if label:
            report.fail(f"第 {offset} 个字符是 {label}（U+{ord(char):04X}）——提交前必须清除")
    if any(ord(c) < 0x20 and c not in "\t\n\r" for c in raw):
        report.fail("含控制字符")


def capability_ids_from_module(module_name: str) -> tuple[set[str], dict[str, int]]:
    """从 partner 脚本源码里解析 @capability(id=..., acps_skill=...) 与参数个数。

    用 AST 不用 import：填报稿校验不该触发 wit 导入与证书读取。
    """
    import ast

    path = CLUSTER / f"{module_name}.py"
    if not path.is_file():
        return set(), {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    declared: set[str] = set()
    arity: dict[str, int] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "capability"):
                continue
            kwargs = {kw.arg: kw.value for kw in deco.keywords}
            id_node = kwargs.get("id")
            if not (isinstance(id_node, ast.Constant) and isinstance(id_node.value, str)):
                continue
            skill = kwargs.get("acps_skill")
            is_skill = skill is None or (isinstance(skill, ast.Constant) and skill.value is True) or isinstance(
                skill, (ast.Dict, ast.Call)
            )
            if isinstance(skill, ast.Constant) and skill.value is False:
                is_skill = False
            if not is_skill:
                continue
            declared.add(id_node.value)
            args = node.args
            n_params = len(args.args) + len(args.kwonlyargs)
            excluded = {"runtime", "state", "self", "cls"}
            n_params -= sum(1 for a in (*args.args, *args.kwonlyargs) if a.arg in excluded)
            n_params -= sum(1 for a in (*args.args, *args.kwonlyargs) if _is_wit_runtime(a))
            arity[id_node.value] = max(0, n_params)
    # 共享工厂构造的能力没有 @capability 装饰器，但同样是代码声明；
    # 目前只有纯 domain agent 的 public profile 使用这个通道。
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "make_agent_info"
        ):
            declared.add("agent.info")
            arity.setdefault("agent.info", 0)
    return declared, arity


def _is_wit_runtime(arg: ast.arg) -> bool:
    annotation = arg.annotation
    if annotation is None:
        return False
    name = getattr(annotation, "id", "") or getattr(annotation, "attr", "")
    return name in {"WitRuntime", "AgentState"}


def domain_visibility_from_module(module_name: str) -> dict[str, list[str]]:
    """从源码 AST 取 `acps_skill={"visibility": "domain", "domains": [...]}` 声明。

    平台的 AgentSkill 是 additionalProperties:false，填报稿里漏掉/被脚本剥掉
    visibility 时，代码里的声明就"平台看不到"——这里把它变成硬性一致性检查。
    """
    import ast

    path = CLUSTER / f"{module_name}.py"
    if not path.is_file():
        # 标准包化后模块位于 agents/ / runtime/ / core/ 下；按短名回退查找。
        short = module_name.rsplit(".", 1)[-1]
        for folder in ("agents", "runtime", "core"):
            candidate = CLUSTER / folder / f"{short}.py"
            if candidate.is_file():
                path = candidate
                break
    if not path.is_file():
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    declared: dict[str, list[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and getattr(deco.func, "id", "") == "capability"):
                continue
            kwargs = {kw.arg: kw.value for kw in deco.keywords}
            id_node = kwargs.get("id")
            skill_node = kwargs.get("acps_skill")
            if not (isinstance(id_node, ast.Constant) and isinstance(id_node.value, str)):
                continue
            if not isinstance(skill_node, ast.Dict):
                continue
            mapping = {
                key.value: value
                for key, value in zip(skill_node.keys, skill_node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            visibility = mapping.get("visibility")
            if not (isinstance(visibility, ast.Constant) and visibility.value == "domain"):
                continue
            domains_node = mapping.get("domains")
            domains = [
                item.value for item in getattr(domains_node, "elts", [])
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            declared[id_node.value] = domains
    return declared


def check_code_consistency(acs: dict, module_name: str | None, report: Report) -> None:
    if not module_name:
        report.warn("未指定 --module：跳过与代码的一致性校验")
        return
    declared, arity = capability_ids_from_module(module_name)
    if not declared:
        report.fail(f"{module_name}.py 里没解析出任何 @capability(id=...)")
        return
    acs_ids = {s.get("id") for s in acs.get("skills") or []}
    missing, extra = declared - acs_ids, acs_ids - declared
    if missing:
        report.fail(f"代码声明但 ACS 未列（会被 ADP 漏匹配）: {sorted(missing)}")
    if extra:
        report.fail(f"ACS 列了但代码没有（审核实测能力时必翻车）: {sorted(extra)}")
    declared_visibility = domain_visibility_from_module(module_name)
    acs_skills = {str(s.get("id")): s for s in acs.get("skills") or []}
    for skill_id, domains in declared_visibility.items():
        skill = acs_skills.get(skill_id)
        if skill is None:
            continue  # 缺技能已在上方 missing 报过
        if str(skill.get("visibility") or "") != "domain":
            report.fail(
                f"skill {skill_id} 代码声明 domain 可见，但 ACS 没有 visibility=domain"
                "（平台/发现层会把它当 public）"
            )
        elif sorted(str(d) for d in skill.get("domains") or []) != sorted(domains):
            report.fail(
                f"skill {skill_id} domains 与代码不一致："
                f"ACS={skill.get('domains')!r} 代码={domains!r}"
            )
    for skill in acs.get("skills") or []:
        params = arity.get(skill.get("id"), 0)
        if params and not skill.get("inputModes"):
            report.warn(f"skill {skill.get('id')} 有 {params} 个入参却没声明 inputModes")
        if not params and skill.get("inputModes"):
            report.warn(f"skill {skill.get('id')} 无入参却声明了 inputModes")
        if not skill.get("name") or skill.get("name") == skill.get("id") or re.match(
            r"^[a-z0-9_]+$", skill.get("name") or ""
        ):
            report.warn(f"skill {skill.get('id')} 的 name={skill.get('name')!r} 是函数名而非可读名（人机两读都吃亏）")
        if not skill.get("examples"):
            report.warn(f"skill {skill.get('id')} 缺 examples（审核与语义匹配都靠它）")


def check_agent_fields(acs: dict, report: Report) -> None:
    if not (acs.get("name") or "").strip():
        report.fail("name 为空（registry 用请求体的 name，但同名 active 会撞 AGENT_NAME_ALREADY_CLAIMED）")
    if not (acs.get("description") or "").strip():
        report.warn("description 为空：schema 放行，但审核与 ADP 匹配都没抓手")
    if not (acs.get("skills") or []):
        report.warn("skills 为空：registry 放行，等于在网络上隐身")
    if acs.get("protocolVersion") != "02.02":
        report.warn(f"protocolVersion={acs.get('protocolVersion')!r}（已过审批次用 '02.02'）")
    provider = acs.get("provider") or {}
    country = provider.get("countryCode") or ""
    if country and not re.fullmatch(r"[A-Z]{2}", country):
        report.fail(f"provider.countryCode={country!r} 不符合 ^[A-Z]{{2}}$")
    email = provider.get("email") or ""
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email):
        report.warn(f"provider.email={email!r} 形态可疑（schema 的 format:email 不生效，故不阻断）")
    aic = acs.get("aic") or ""
    if aic:
        report.warn(f"提交稿里 aic={aic!r} 会被 registry 覆盖为分配值；手填非合规 OID 无收益，建议留空")


def validate(path: Path, module_name: str | None = None) -> Report:
    raw = path.read_text(encoding="utf-8")
    report = Report(path)
    check_invisible(raw, report)
    try:
        acs = json.loads(raw)
    except ValueError as exc:
        report.fail(f"不是合法 JSON: {exc}")
        return report
    missing_keys = [k for k in TOP_REQUIRED if k not in acs]
    if missing_keys:
        report.fail(f"缺 registry 必填顶层键: {missing_keys}")
    check_schema(acs, report)
    check_endpoints(acs, report)
    check_agent_fields(acs, report)
    check_code_consistency(acs, module_name, report)
    return report


FILLED = {
    "acs.gpu-partner.filled.json": "partner_gpu",
    "acs.cpu-partner.filled.json": "partner_cpu",
    "acs.kb-partner.filled.json": "partner_kb",
    "acs.eval-partner.filled.json": "partner_eval",
    "acs.model-partner.filled.json": "partner_model",
}


def main(argv: list[str]) -> int:
    if "--all" in argv:
        targets = [(CLUSTER / name, module) for name, module in FILLED.items()
                   if (CLUSTER / name).is_file()]
        targets += [(p, None) for p in sorted(CLUSTER.glob("acs.*-partner.filled.json"))
                    if p.name not in FILLED]
        targets += [(CLUSTER / w / "acps" / "acs.final.json", None)
                    for w in ("ws-gpu", "ws-cpu", "ws-kb")
                    if (CLUSTER / w / "acps" / "acs.final.json").is_file()]
    else:
        module = None
        if "--module" in argv:
            idx = argv.index("--module")
            if idx + 1 >= len(argv):
                print("--module 缺参数")
                return 2
            module = argv[idx + 1]
            argv = argv[:idx] + argv[idx + 2:]
        paths = [Path(a) for a in argv if not a.startswith("--")]
        targets = [(p, module) for p in paths]
    failed = 0
    for path, module in targets:
        report = validate(path, module)
        print(report.render())
        failed += bool(report.fails)
    print(f"\n{len(targets)} 份，{failed} 份 FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
