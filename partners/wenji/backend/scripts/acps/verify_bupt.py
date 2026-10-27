"""BUPT 端点端到端验证：内核三类任务（NL2SQL / RAG / 跨源）+ 时延。

用法（backend/ 目录）：
  LLM_PROVIDER=bupt ACPS_LLM_MODEL=deepseek-v4-flash \
    .venv/bin/python scripts/acps/verify_bupt.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_DIR = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))
load_dotenv(REPO_DIR / ".env", override=False)

from app.acps import bridge  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def timed(label: str, fn, *args) -> dict:
    t0 = time.time()
    result = fn(*args)
    print(f"[{label}] {time.time() - t0:.1f}s status={result.get('status')}")
    return result


def main() -> None:
    s = get_settings()
    print(f"provider={s.llm_provider} model={s.active_model} base={s.bupt_base_url or s.deepseek_base_url}")

    r = timed("NL2SQL", bridge.data_query, "销量最高的前五首曲目是什么？")
    print("   sql:", (r.get("sql") or "")[:100])
    print("   rows:", r.get("row_count"))

    r = timed("RAG", bridge.doc_qa, "员工年假有多少天？")
    print("   answer:", (r.get("answer") or "")[:80])

    r = timed("HYBRID", bridge.answer_question,
              "销量最高的销售代表是谁？请结合员工手册总结他的职责。", "bupt-e2e")
    print("   intent:", r.get("intent"))
    print("   answer:", (r.get("answer") or "")[:100])

    r = timed("FORMULA", bridge.formula_eval, "Jane 的销售提成是多少？")
    print("   value:", r.get("value"), "| ok:", r.get("ok"))


if __name__ == "__main__":
    main()
