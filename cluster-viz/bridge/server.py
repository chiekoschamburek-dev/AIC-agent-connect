"""cluster-viz 桥接桩：把研枢集群的真实运行事件翻译成可视化契约事件（src/types.ts）。

用途（后续接线）：
1. 实时模式（live）：在 supervisor/partner 的 wit hooks（AipDelegationHooks / AMP hooks /
   transform hook）里调用 emit()，事件经 WebSocket 推给前端，画面随真实集群同步。
2. 转录模式（replay）：把一次真实运行的 pep-audit JSONL + demo_run 产物翻译成一个
   scenario JSON（regions/agents/events），前端以回放方式呈现——录制视频的最稳路径。

事件契约见 ../src/types.ts 的 VizEvent 定义；本文件是给集群侧用的发射器骨架。
当前为桩：提供类型、翻译函数与本地 WS 广播（websockets 库可选安装）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

# ─────────────────────────── 事件构造 ───────────────────────────


def ev_instruction(text: str, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "instruction", "text": text}


def ev_plan(subtasks: list[tuple[str, str]], t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "plan", "subtasks": [{"id": i, "title": x} for i, x in subtasks]}


def ev_delegate(frm: str, to: str, task_id: str, skill: str, label: str | None = None, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "delegate", "from": frm, "to": to, "taskId": task_id, "skill": skill, "label": label}


def ev_token(kind: str, issuer: str, holder: str, *, domain: str | None = None,
             max_uses: int | None = None, skill: str | None = None, label: str | None = None,
             t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "token", "kind": kind, "issuer": issuer, "holder": holder,
            "domain": domain, "maxUses": max_uses, "skill": skill, "label": label}


def ev_pep(caller: str, target: str, skill: str, decision: str, rule: str,
           domain: str | None = None, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "pep", "caller": caller, "target": target, "skill": skill,
            "decision": decision, "rule": rule, "domain": domain}


def ev_state(agent: str, state: str, note: str | None = None, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "state", "agent": agent, "state": state, "note": note}


def ev_product(agent: str, name: str, state: str, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "product", "agent": agent, "name": name, "state": state}


def ev_domain(action: str, domain_id: str, revision: int | None = None, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "domain", "action": action, "domainId": domain_id, "revision": revision}


def ev_metric(agent: str, key: str, value: str, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "metric", "agent": agent, "key": key, "value": value}


def ev_reconcile(domain: str, total_uses: int, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "reconcile", "domain": domain, "totalUses": total_uses}


def ev_done(summary: str, t: float | None = None) -> dict[str, Any]:
    return {"t": _t(t), "type": "done", "summary": summary}


_T0 = time.monotonic()  # 事件时间原点：前端从 0 起播，绝对 monotonic 值会让时长爆炸


def _t(t: float | None) -> float:
    """相对秒。此前直接返回 time.monotonic()（开机至今的秒数），
    而 scenarioDuration=max(t)+1 且播放从 0 开始 → 画面几乎不动（审查发现）。"""
    return round(time.monotonic() - _T0 if t is None else t, 3)


_LIVE: list[Any] = []  # serve() 注册的队列，emit() 往里塞


def emit(event: Mapping[str, Any]) -> None:
    """集群侧 hooks 的一行发射器（模块 docstring 承诺的入口，此前并不存在）。

    非阻塞、丢帧不报错：可视化旁路绝不能掀掉正在跑的集群任务。
    """
    for queue in list(_LIVE):
        try:
            queue.put_nowait(dict(event))
        except Exception:  # noqa: BLE001
            pass


# ─────────────────── pep-audit JSONL → 契约事件 ───────────────────

def translate_audit_line(line: str) -> dict[str, Any] | None:
    """把一行 pep-audit（acps_cluster/delegation/audit.py 的 JSONL）翻成 pep 契约事件。

    审计行字段：ts/decision/rule/callerAic/myAic/taskId/sessionId/jti/domain/domainRevision/skillHint。
    集群侧 AIC 到画布 agent id 的映射由调用方提供（roster 表），这里只做结构翻译。
    """
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    decision = rec.get("decision")
    if decision not in {"allow", "deny"}:
        return None
    return {
        "t": 0.0,  # 时间由回放装配器统一重排（审计 ts 是墙钟，需换算成相对秒）
        "type": "pep",
        "caller": rec.get("callerAic"),
        "target": rec.get("myAic"),
        "skill": rec.get("skillHint") or "",
        "decision": decision,
        "rule": rec.get("rule") or "",
        "domain": rec.get("domain"),
    }


def translate_audit_file(path: Path | str) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        ev = translate_audit_line(line)
        if ev is not None:
            out.append(ev)
    return out


# ─────────────────────────── WebSocket 广播 ───────────────────────────


async def serve(events_source: Iterable[Mapping[str, Any]], host: str = "127.0.0.1", port: int = 8777) -> None:
    """本地 WS 广播（需 pip install websockets）。前端实时模式连接 ws://127.0.0.1:8777。

    实时接线方式：在集群 hooks 里 import 本模块，把构造好的事件 append 进 asyncio.Queue
    传给 events_source；前端按收到的事件即时 applyEvent（与回放共用同一 reducer）。
    """
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("实时模式需要 pip install websockets；转录模式不需要本函数") from exc

    clients: set[Any] = set()

    async def handler(ws: Any) -> None:
        clients.add(ws)
        try:
            await ws.wait_closed()
        finally:
            clients.discard(ws)

    async def broadcast(msg: str) -> None:
        for ws in list(clients):
            try:
                await ws.send(msg)
            except Exception:  # noqa: BLE001
                clients.discard(ws)

    if not hasattr(events_source, "get"):
        raise TypeError("serve() 需要 asyncio.Queue（与 docstring 一致）；"
                        "同步 for 遍历只能取尽一次，接不上实时事件流")
    _LIVE.append(events_source)
    try:
        async with websockets.serve(handler, host, port):
            while True:
                ev = await events_source.get()  # 原实现是 `for ev in events_source`：一次性遍历
                await broadcast(json.dumps(ev, ensure_ascii=False))
    finally:
        _LIVE.remove(events_source)


if __name__ == "__main__":
    # 冒烟：翻译一份真实审计文件，打印前 5 条契约事件
    import sys

    # 默认路径按 __file__ 定位：此前写 "../my-project/…"，从 bridge/ 目录运行时
    # 解析到不存在的位置，translate 静默产出 0 事件（审查发现）
    default_src = (Path(__file__).resolve().parents[2]
                   / "my-project" / "acps_cluster" / "demo_run" / "domain" / "gpu-audit.jsonl")
    src = sys.argv[1] if len(sys.argv) > 1 else str(default_src)
    events = translate_audit_file(src)
    print(f"translated {len(events)} events from {src}")
    if not events:
        print("警告: 0 事件——文件不存在或每行都未被识别", file=sys.stderr)
    for ev in events[:5]:
        print(json.dumps(ev, ensure_ascii=False))
