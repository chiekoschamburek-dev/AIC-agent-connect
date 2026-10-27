"""AIP 能力与既有引擎之间的桥接层。

设计原则：

- 复用既有流水线（AgentKernel / NL2SQLPipeline / RAGPipeline），不复制业务逻辑；
- 所有流水线都是同步阻塞调用（LLM + PostgreSQL + 向量检索），
  AIP handler 通过 ``asyncio.to_thread`` 调用，避免阻塞 WIT 事件循环；
- 对外返回精简、结构化、可审计的产物（答案 / SQL / 引用 / 行预览 / turn_id），
  完整 Trace 仍由交互式 API（``GET /api/trace/{turn_id}``）提供，避免 AIP 报文过载。
"""

from __future__ import annotations

import asyncio
import statistics
import threading
import time
from collections import deque
from typing import Any

# --------------------------------------------------------------------------
# AMP Metrics 用：进程内任务计数与滑动窗口时延（每个 AIP 调用 = 一次 task）
# --------------------------------------------------------------------------
_task_lock = threading.Lock()
_active_tasks = 0
_finished_tasks = 0
_failed_tasks = 0
_recent_latencies: deque[float] = deque(maxlen=128)  # ms
_started_at = time.monotonic()


def task_metrics() -> dict[str, Any]:
    """AmpMetricsHooks.collect 用的负载快照（真实计数与分位）。"""
    with _task_lock:
        active, finished, failed = _active_tasks, _finished_tasks, _failed_tasks
        latencies = list(_recent_latencies)
    p50 = statistics.median(latencies) if latencies else None
    p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)] if latencies else None
    return {
        "loadMetrics": {
            "activeTasks": active,
            "queuedTasks": 0,
            "maxActiveTasks": 8,
            "maxQueuedTasks": 32,
        },
        "windowMetrics": [
            {
                "window": "PT5M",
                "requestTotal": finished,
                "successRate": round(100.0 * (finished - failed) / finished, 2) if finished else 100.0,
                "p50_latency_ms": round(p50, 1) if p50 is not None else None,
                "p95_latency_ms": round(p95, 1) if p95 is not None else None,
            }
        ],
    }


class _TaskScope:
    """``with _TaskScope():`` 统计并发任务区间、成败与耗时。"""

    def __enter__(self) -> "_TaskScope":
        global _active_tasks
        with _task_lock:
            _active_tasks += 1
        self._t0 = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, *exc: Any) -> None:
        global _active_tasks, _finished_tasks, _failed_tasks
        elapsed_ms = (time.monotonic() - self._t0) * 1000
        with _task_lock:
            _active_tasks = max(0, _active_tasks - 1)
            _finished_tasks += 1
            if exc_type is not None:
                _failed_tasks += 1
            else:
                _recent_latencies.append(elapsed_ms)


# --------------------------------------------------------------------------
# 能力实现（同步；由 capabilities.py 在线程池中执行）
# --------------------------------------------------------------------------

def _trim_rows(rows: list[list], limit: int = 20) -> list[list]:
    return [list(r) for r in rows[:limit]]


def _citation_digest(citations: list[dict], limit: int = 8) -> list[dict]:
    out: list[dict] = []
    for c in citations[:limit]:
        out.append(
            {
                "doc": c.get("doc") or c.get("document") or c.get("title", ""),
                "page": c.get("page"),
                "snippet": (c.get("snippet") or c.get("text") or "")[:200],
            }
        )
    return out


def answer_question(question: str, session_id: str = "aip") -> dict[str, Any]:
    """综合问数/问答：交给 AgentKernel 自行路由（NL2SQL / RAG / HYBRID / 澄清）。"""
    from ..agent.kernel import AgentKernel

    with _TaskScope():
        turn = AgentKernel().run(question, session_id=session_id)
    result: dict[str, Any] = {
        "answer": turn.answer,
        "intent": turn.intent,
        "status": turn.status,
        "workspace": "问迹引擎（自动路由：数据库 / 文档 / 跨源）",
        "rewritten_question": turn.rewritten or None,
        "citations": _citation_digest(turn.citations),
        "latency_ms": turn.latency_ms,
    }
    if turn.data:
        # 数据类回答附 SQL 与行预览，便于调用方核验可解释性
        data = turn.data
        result["sql"] = data.get("sql")
        result["columns"] = data.get("columns")
        result["rows"] = _trim_rows(data.get("rows") or [])
        result["row_count"] = data.get("row_count")
    if turn.clarify:
        result["clarify"] = {
            "question": turn.clarify.get("question") or turn.clarify.get("prompt"),
            "options": turn.clarify.get("options"),
        }
    return result


def data_query(question: str) -> dict[str, Any]:
    """结构化问数（NL2SQL 专用通道）：返回 SQL、数据行与图表建议。"""
    from ..nl2sql.pipeline import NL2SQLPipeline

    with _TaskScope():
        r = NL2SQLPipeline().run(question)
    return {
        "question": r.question,
        "rewritten_question": r.rewritten or None,
        "sql": r.sql,
        "columns": r.columns,
        "rows": _trim_rows(r.rows),
        "row_count": r.row_count,
        "truncated": r.truncated,
        "summary": r.summary,
        "chart_hint": r.chart_hint,
        "status": r.status,
        "errors": r.errors,
        "latency_ms": r.latency_ms,
    }


def doc_qa(question: str, doc_filter: str | None = None) -> dict[str, Any]:
    """知识库文档问答（RAG 专用通道）：返回答案与文档级引用。"""
    from ..rag.pipeline import RAGPipeline

    with _TaskScope():
        r = RAGPipeline().run(question, doc_filter=doc_filter)
    return {
        "question": r.question,
        "answer": r.answer,
        "status": r.status,
        "citations": _citation_digest(r.citations),
        "retrieval_query": r.retrieval_query,
        "faithfulness": r.faithfulness,
        "latency_ms": r.latency_ms,
    }


def formula_eval(question: str) -> dict[str, Any]:
    """公式计算：从问题解析目标公式（如销售提成）→三通道绑定参数→LaTeX→SymPy 双通道复算。"""
    from ..core.tracing import TraceCollector
    from ..formula.engine import FormulaEngine

    with _TaskScope():
        r = FormulaEngine().run(question, trace=TraceCollector(question=question))
    if not r.ok:
        return {
            "question": question,
            "ok": False,
            "status": "degraded",
            "degraded_reason": r.degraded_reason,
        }
    return {
        "question": question,
        "ok": True,
        "status": "ok",
        "value": r.value,
        "steps": r.steps,
        "formula": r.formula,
        "params": r.params,
        "citations": _citation_digest(r.citations),
    }


def schema_overview(max_tables: int = 30) -> dict[str, Any]:
    """业务库 schema 摘要，供平台上的其它智能体规划使用。"""
    from ..db import schema_meta

    tables = schema_meta.load_table_meta()
    return {
        "table_count": len(tables),
        "tables": [
            {
                "name": t.name,
                "row_count": t.row_count,
                "columns": [c.name for c in t.columns],
            }
            for t in tables[:max_tables]
        ],
    }


async def run_in_thread(fn, *args: Any, **kwargs: Any) -> Any:
    """在默认线程池执行同步流水线（供 async capability handler 调用）。"""
    return await asyncio.to_thread(fn, *args, **kwargs)
