"""把一次 supervisor run 记录成 cluster-viz 的 Scenario，并给 AIP 产物追加工作流链接。

设计边界（2026-10-18 与用户定案）：
- 不试图让叮当自动弹网页（平台结果路径是 Markdown，不支持通用全屏/iframe）；
- 只做**代码侧追加一个醒目的 Markdown 链接**，用户点击后在新标签页打开
  `cluster-viz` 的本次任务回放页；
- 默认关闭：未设 `ACPS_VIZ_BASE_URL` 时不记录、不追加链接，行为与以前一致。

环境变量：
- `ACPS_VIZ_BASE_URL`：可视化站点根地址，如 `https://viz.example.com` 或
  `https://example.com/viz`。设置后才启用。
- `ACPS_VIZ_SCENARIO_DIR`：Scenario JSON 输出目录；默认仓库内
  `cluster-viz/public/scenarios`（服务器上与静态站点共享目录，或同步）。

记录的事件刻意只使用 leader 侧**可证实**的事实（计划、委派、令牌、等待状态、产物），
不伪造 PEP allow/deny 或域冻结等伙伴侧事件；需要更细的审计事件时再扩展。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

BASE_ENV = "ACPS_VIZ_BASE_URL"
DIR_ENV = "ACPS_VIZ_SCENARIO_DIR"
DEFAULT_SCENARIO_DIR = (
    Path(__file__).resolve().parents[2] / "cluster-viz" / "public" / "scenarios"
)

DELEGATE_TOOLS = frozenset({
    "acps_delegate", "acps.delegate",
    "acps_delegate_public", "acps_delegate_authenticated",
})
_TERMINAL_OK = {"completed", "success", "awaiting-completion"}
_TERMINAL_FAIL = {"failed", "rejected", "canceled", "cancelled", "expired"}
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


def base_url() -> str:
    """可视化站点根地址；空串表示关闭。"""
    return str(os.getenv(BASE_ENV, "")).strip().rstrip("/")


def scenario_dir() -> Path:
    raw = str(os.getenv(DIR_ENV, "")).strip()
    return Path(raw) if raw else DEFAULT_SCENARIO_DIR


def enabled() -> bool:
    base = base_url()
    return base.startswith("http://") or base.startswith("https://")


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def _base(base_url_override: str | None = None) -> str:
    base = (base_url_override if base_url_override is not None else base_url()).strip().rstrip("/")
    return base


def workflow_url(run_id: str, base_url_override: str | None = None) -> str:
    """本次任务的回放地址。"""
    return f"{_base(base_url_override)}/?run={quote(str(run_id), safe='')}&autoplay=1"


def live_url(base_url_override: str | None = None) -> str:
    """固定实时地址：始终跟随 supervisor 最新 run，旧回答里的链接也不会过期。"""
    return f"{_base(base_url_override)}/?live=1"


def workflow_product(run_id: str, base_url_override: str | None = None) -> dict[str, Any]:
    """一个独立的 AIP product，不修改模型正文。"""
    run = workflow_url(run_id, base_url_override)
    live = live_url(base_url_override)
    text = (
        "## 🧭 智能体协作过程\n\n"
        f"[▶ 实时工作流总览（自动跟随最新任务）]({live})\n\n"
        f"[↺ 回放本次任务]({run})\n\n"
        "> 实时页在整个任务执行期间持续刷新，任务开始后即可打开；"
        "旧回答里的实时链接也会自动切到最新一轮，不会因 run_id 变化失效。\n"
    )
    return {
        "id": "workflow-viz",
        "name": "智能体协作工作流",
        "description": "实时跟随最新任务；另附本次任务回放",
        "data_items": [{"type": "text", "text": text}],
    }


def append_workflow_product(products: list, recorder: "Recorder | None") -> bool:
    """在 products 末尾追加链接产物（幂等）。返回是否追加/已存在。"""
    if not isinstance(products, list):
        return False
    if any(isinstance(p, dict) and p.get("id") == "workflow-viz" for p in products):
        return True
    if recorder is None or not recorder.available:
        return False
    recorder.flush()
    products.append(workflow_product(recorder.run_id, recorder.base_url))
    return True


def instruction_from_event(event: Any) -> str:
    """从 run 上下文尽力取本轮用户指令；取不到用中性文案。"""
    ctx = getattr(event, "context", None)
    state = getattr(ctx, "state", None)
    messages = getattr(state, "messages", None) or []
    for message in reversed(list(messages)):
        role = str(getattr(message, "role", "") or "").lower()
        if role not in {"user", "human"}:
            continue
        text = _content_text(getattr(message, "content", None))
        if text:
            return text[:2000]
    metadata = getattr(state, "metadata", None)
    if isinstance(metadata, Mapping):
        for key in ("task", "task_text", "instruction", "question"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:2000]
    return "研枢集群任务"


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        chunks: list[str] = []
        for part in value:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text") or part.get("content")
                if isinstance(text, str):
                    chunks.append(text)
            else:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(c for c in chunks if c).strip()
    return ""


def _as_dict(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _task_ref(value: Any) -> str:
    """从响应里递归找 task_ref/taskId。"""
    if isinstance(value, Mapping):
        for key in ("task_ref", "taskRef", "task_id", "taskId"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        for nested in value.values():
            found = _task_ref(nested)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _task_ref(item)
            if found:
                return found
    return ""


def _state_name(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("state") or value.get("status") or ""
    return str(value or "").strip().lower()


class Recorder:
    """一次 run 的 Scenario 记录器；只写本地 JSON，不做网络请求。"""

    def __init__(
        self,
        *,
        instruction: str = "",
        roster: Iterable[Any] = (),
        run_id: str | None = None,
        scenario_dir_override: Path | str | None = None,
        base_url_override: str | None = None,
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.base_url = (base_url_override if base_url_override is not None else base_url())
        self.scenario_dir = Path(scenario_dir_override) if scenario_dir_override else scenario_dir()
        self.instruction = str(instruction or "").strip()[:2000] or "研枢集群任务"
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.events: list[dict[str, Any]] = []
        self.agents: dict[str, dict[str, Any]] = {
            "sup": {
                "id": "sup",
                "name": "研枢 · Supervisor",
                "role": "supervisor",
                "org": "yanshu",
                "source": "roster",
                "skills": ["orchestrate"],
            }
        }
        self.regions = [{
            "id": "yanshu",
            "name": "研枢集群（本次任务）",
            "color": "#0ea5e9",
            "x": 40,
            "y": 40,
            "w": 760,
            "h": 620,
        }]
        self._roster = list(roster or ())
        self._pending: dict[str, dict[str, Any]] = {}
        self._task_agents: dict[str, str] = {}
        self._steps = 0
        self._done = False
        self.available = bool(self.base_url)
        self._emit("instruction", text=self.instruction, t=0.5)
        self._emit("agent_up", agent=dict(self.agents["sup"]), t=0.8)
        self._emit("state", agent="sup", state="planning",
                   note="Supervisor 正在拆解任务", t=1.0)
        self._write()

    @property
    def t(self) -> float:
        return round(time.monotonic() - self.started, 2)

    def _emit(self, kind: str, *, t: float | None = None, **fields: Any) -> None:
        event = {"type": kind, **(fields or {})}
        event["t"] = self.t if t is None else t
        self.events.append(event)

    def _write(self) -> None:
        if not self.available:
            return
        try:
            target_dir = Path(self.scenario_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp = target_dir / f".{self.run_id}.json.tmp"
            tmp.write_text(
                json.dumps(self.scenario(), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, target_dir / f"{self.run_id}.json")
            self._write_live(target_dir)
        except OSError as exc:
            self.available = False
            print(f"[viz] Scenario 写入失败，工作流链接已禁用: {type(exc).__name__}: {exc}",
                  flush=True)

    def _write_live(self, target_dir: Path) -> None:
        """维护“当前最新 run”指针；旧 run 的迟到事件不得覆盖新 run。"""
        path = target_dir / "live.json"
        previous: dict[str, Any] = {}
        try:
            if path.is_file():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    previous = loaded
        except (OSError, ValueError):
            previous = {}
        try:
            if float(previous.get("started_at") or 0) > self.started_wall + 0.001:
                return
        except (TypeError, ValueError):
            pass
        payload = {
            "run_id": self.run_id,
            "title": f"研枢 · 本次协作（{self.run_id}）",
            "instruction": self.instruction[:300],
            "started_at": self.started_wall,
            "updated_at": time.time(),
            "done": bool(self._done),
        }
        tmp = target_dir / ".live.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def scenario(self) -> dict[str, Any]:
        return {
            "title": f"研枢 · 本次协作（{self.run_id}）",
            "description": "由 Supervisor 真实执行轨迹生成的只读回放；不含 token/私钥。",
            "instruction": self.instruction,
            "regions": list(self.regions),
            "agents": list(self.agents.values()),
            "events": list(self.events),
        }

    def flush(self) -> None:
        self._write()

    def _ensure_agent(self, target: str, skill: str | None = None) -> dict[str, Any]:
        target = str(target or "").strip()
        for member in self._roster:
            aic = str(getattr(member, "aic", "") or "")
            key = str(getattr(member, "key", "") or "")
            name = str(getattr(member, "name", "") or "")
            if target and target in {aic, key, name}:
                agent_id = _SAFE_ID.sub("-", key or aic).strip("-")[:28] or "partner"
                spec = self.agents.get(agent_id)
                if spec is None:
                    spec = {
                        "id": agent_id,
                        "name": name or key or target[-16:],
                        "role": "partner",
                        "org": "yanshu",
                        "source": "roster",
                        "skills": [skill] if skill else [],
                    }
                    self.agents[agent_id] = spec
                    self._emit("agent_up", agent=dict(spec))
                elif skill and skill not in spec["skills"]:
                    spec["skills"].append(skill)
                return spec
        digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:10] if target else uuid.uuid4().hex[:10]
        agent_id = f"p-{digest}"
        spec = self.agents.get(agent_id)
        if spec is None:
            spec = {
                "id": agent_id,
                "name": target[-18:] if target else "外部伙伴",
                "role": "partner",
                "org": "yanshu",
                "source": "domain",
                "skills": [skill] if skill else [],
            }
            self.agents[agent_id] = spec
            self._emit("agent_up", agent=dict(spec))
        elif skill and skill not in spec["skills"]:
            spec["skills"].append(skill)
        return spec

    def plan(self, plan: Any) -> None:
        data = _as_dict(plan)
        steps = data.get("steps") if isinstance(data.get("steps"), list) else []
        subtasks = []
        for index, step in enumerate(steps):
            item = _as_dict(step)
            title = str(item.get("purpose") or item.get("capability") or item.get("partner") or "")
            if title:
                subtasks.append({"id": str(item.get("id") or f"t{index + 1}"), "title": title[:80]})
        if subtasks:
            self._emit("plan", subtasks=subtasks)
            self._emit("state", agent="sup", state="planning",
                       note=f"计划 {len(subtasks)} 步", t=self.t + 0.2)

    def tool_request(self, name: str, args: Any, tool_call: Any = None) -> None:
        args = _as_dict(args)
        if name == "plan_gate":
            self.plan(args.get("plan") or args)
            return
        if name in DELEGATE_TOOLS:
            target = str(
                args.get("partner_aic") or args.get("partner")
                or args.get("target_aic") or args.get("target") or ""
            ).strip()
            if not target:
                return
            skill = str(
                args.get("skill_id") or args.get("skillId")
                or args.get("skill") or "delegate"
            ).strip() or "delegate"
            agent = self._ensure_agent(target, skill)
            self._steps += 1
            task_id = f"step-{self._steps}"
            label = str(args.get("request") or args.get("task") or "").strip().replace("\n", " ")[:80]
            self._emit("delegate", **{"from": "sup"}, to=agent["id"], taskId=task_id,
                       skill=skill, label=label or skill)
            self._emit("state", agent=agent["id"], taskId=task_id, state="working")
            if name == "acps_delegate_authenticated":
                self._emit("token", kind="delegation", issuer="sup",
                           holder=agent["id"], skill=skill, label="本次委派")
            key = str(getattr(tool_call, "id", None) or id(tool_call))
            self._pending[key] = {"agent": agent["id"], "task": task_id}
            self._write()
            return
        if name == "aip_submit":
            products = args.get("products")
            if not isinstance(products, list):
                products = args if isinstance(args, list) else []
            for product in products:
                if not isinstance(product, Mapping):
                    continue
                product_id = str(product.get("id") or product.get("name") or "product")
                self._emit("product", agent="sup", name=product_id[:80], state="final")
            self._write()

    def tool_response(self, name: str, response: Any, tool_call: Any = None) -> None:
        if name in DELEGATE_TOOLS:
            key = str(getattr(tool_call, "id", None) or id(tool_call))
            pending = self._pending.pop(key, None)
            if pending is None and tool_call is None and self._pending:
                pending = self._pending.pop(next(iter(self._pending)), None)
            if pending is None:
                return
            ref = _task_ref(response)
            if ref:
                self._task_agents[ref] = pending["agent"]
            self._write()
            return
        if name in {"wait_partner", "wait_many"}:
            if name == "wait_partner":
                refs = [str(_as_dict(response).get("task_ref") or _as_dict(response).get("taskRef") or "")]
                states = [_as_dict(response).get("state") or _as_dict(response).get("status")]
            else:
                rows = _as_dict(response).get("arrived") or []
                refs = [str(_as_dict(row).get("task_ref") or _as_dict(row).get("taskRef") or "") for row in rows]
                states = [_as_dict(row).get("state") or _as_dict(row).get("status") for row in rows]
            for ref, state in zip(refs, states):
                agent = self._task_agents.get(ref)
                if not agent:
                    continue
                state_name = _state_name(state)
                if state_name in _TERMINAL_OK:
                    mapped = "completed"
                elif state_name in _TERMINAL_FAIL:
                    mapped = "rejected"
                else:
                    mapped = "working"
                self._emit("state", agent=agent, taskId=ref, state=mapped,
                           note=state_name or None)
            self._write()

    def finish(self, summary: str = "") -> None:
        text = str(summary or "").strip().replace("\n", " ")[:300]
        if self._done:
            for event in reversed(self.events):
                if event.get("type") == "done":
                    if text:
                        event["summary"] = text
                    break
            self._write()
            return
        self._done = True
        self._emit("state", agent="sup", state="completed", note="本次 run 收尾")
        self._emit("done", summary=text or "本次协作结束")
        self._write()


def start_run(*, instruction: str = "", roster: Iterable[Any] = ()) -> Recorder | None:
    """按环境变量决定是否记录；关闭时返回 None。"""
    if not enabled():
        return None
    return Recorder(instruction=instruction, roster=roster)
