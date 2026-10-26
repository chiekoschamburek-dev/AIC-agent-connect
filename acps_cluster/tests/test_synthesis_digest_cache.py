"""digest 缓存回归（2026-10-16）：同篇论文不重复烧 LLM。

背景：集群检索主题重复率 ≈45%，同一批经典论文被反复精读（一次 7 篇 ≈ 79s LLM）。
按"论文"缓存精读结果后整篇秒回。键：arXiv ID（去版本号）优先，否则归一化全文哈希。

覆盖：
1. 键规则：ID 去版本 / 多 ID 退哈希 / 无 ID 退哈希 / 归一化不敏感；
2. MISS 走 LLM 一次 → HIT 秒回不再调（返回一致）；
3. TTL（默认 90 天）过期后重新调用；
4. ACPS_SYNTHESIS_DIGEST_CACHE=0 完全旁路；
5. 缓存行损坏 → 降级走 LLM 不崩；
6. **注册面契约**：paper.extract 的注册描述不得混入缓存字样（描述在 ACS 里，动了要重注册）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))
os.environ.setdefault("OPENAI_API_KEY", "sk-test")  # 仅在真调 llm_tools._instance() 前占位；本测试全走 fake

import partner_synthesis as ps  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✔' if ok else '✘'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


_DIGEST_JSON = json.dumps({
    "core_innovation": "提出 X", "research_gap": "缺口 Y",
    "methodology": "方法 Z", "experiment_setup": "配置 W",
    "keywords": ["扩散", "生成", "采样"],
}, ensure_ascii=False)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="digest-cache-"))
    ps._DIGEST_DB = tmp / "digests.sqlite3"
    calls = {"n": 0}

    async def fake_chat(system, user, *, temperature=0.4):
        calls["n"] += 1
        return _DIGEST_JSON

    orig_llm = ps.llm_tools
    ps.llm_tools = SimpleNamespace(chat=fake_chat)
    try:
        # 1) 键规则
        check("[1] arXiv ID 去版本号", ps._digest_key("x arXiv:2006.11239v2 y") == "arxiv:2006.11239")
        check("[1] 多 ID 退回全文哈希", ps._digest_key("2006.11239 与 2010.02502").startswith("txt:"))
        check("[1] 无 ID 退回全文哈希", ps._digest_key("一篇没有编号的论文").startswith("txt:"))
        check("[1] 归一化对大小写/空白不敏感",
              ps._digest_key("Hello  World\n\n2020") == ps._digest_key("hello world 2020"))

        # 2) MISS → LLM；HIT → 秒回
        txt = "Denoising Diffusion Probabilistic Models. arXiv:2006.11239. Abstract: ..."
        d1 = await ps.extract_digest(txt)
        n1 = calls["n"]
        d2 = await ps.extract_digest(txt)
        check("[2] 首次走 LLM", n1 == 1, f"n={n1}")
        check("[2] 二次命中缓存不调 LLM", calls["n"] == 1, f"n={calls['n']}")
        check("[2] 命中返回一致", d1.core_innovation == d2.core_innovation == "提出 X")
        check("[2] 命中内容字段完整", d2.methodology == "方法 Z" and list(d2.keywords) == ["扩散", "生成", "采样"])

        # 3) TTL 过期 → 重新调用
        con = sqlite3.connect(ps._DIGEST_DB)
        con.execute("UPDATE digests SET fetched_at=?", (time.time() - 100 * 86400,))
        con.commit()
        con.close()
        await ps.extract_digest(txt)
        check("[3] 过期后重新调用 LLM", calls["n"] == 2, f"n={calls['n']}")

        # 4) 开关关闭 → 每次都调
        os.environ["ACPS_SYNTHESIS_DIGEST_CACHE"] = "0"
        await ps.extract_digest(txt)
        await ps.extract_digest(txt)
        check("[4] 关闭后每次都调 LLM", calls["n"] == 4, f"n={calls['n']}")
        os.environ.pop("ACPS_SYNTHESIS_DIGEST_CACHE", None)

        # 5) 缓存行损坏 → 降级走 LLM 不崩
        con = sqlite3.connect(ps._DIGEST_DB)
        con.execute("UPDATE digests SET digest_json='{bad json'")
        con.commit()
        con.close()
        d5 = await ps.extract_digest(txt)
        check("[5] 损坏行降级走 LLM 不崩", calls["n"] == 5 and d5.core_innovation == "提出 X",
              f"n={calls['n']}")

        # 6) 注册面契约
        cap = ps.extract_digest.capability
        check("[6] 注册描述未被改动（无『缓存』字样）", "缓存" not in cap.description)
        check("[6] public/ID 不变", bool(cap.public) is True and cap.id == "paper.extract")
    finally:
        ps.llm_tools = orig_llm
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        for f in FAILS:
            print("  ✘", f)
        return 1
    print("✔ 通过：digest 缓存键/TTL/开关/故障降级均正确，注册面未动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
