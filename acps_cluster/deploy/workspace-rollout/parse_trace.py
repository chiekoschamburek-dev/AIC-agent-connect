#!/usr/bin/env python3
"""摘要 supervisor run-trace 的事件。

用法：
    python parse_trace.py <run-trace.jsonl> [起始行号，默认 0]

只打印排障需要的字段：工具调用、委派直发、bundle 签发、镜像事件、run_end。
不打印 token 原文（trace 里本来也不该有）。
"""
from __future__ import annotations

import json
import sys

WATCH = {
    "delegate_direct", "ws_bundle_issued", "workspace_mirror",
    "delegate_auth_attached", "delegate_direct_failed",
    "ws_bundle_skipped", "ws_bundle_token_failed", "workspace_mirror_skipped",
}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    path = sys.argv[1]
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()

    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        name = event.get("event")
        if name == "tool_request":
            print("REQ ", event.get("tool"), str(event.get("args"))[:180])
        elif name == "tool_response":
            print("RESP", str(event.get("response"))[:200])
        elif name in WATCH:
            body = {k: v for k, v in event.items() if k not in ("ts", "pid")}
            print("EVT ", name, json.dumps(body, ensure_ascii=False)[:300])
        elif name == "run_end":
            print("RUN_END", event.get("status"), str(event.get("final_text"))[:200])
        elif name == "aip_submit_flush":
            print("SUBMIT", str(event.get("result"))[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
