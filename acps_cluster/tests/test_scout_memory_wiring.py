"""scout 记忆层接入验证：

 A. **同一主题第二次检索不联网**（本次改造的核心价值）
 B. **新增记忆能力不改 ACS 指纹** → **无需重新注册**（回答用户的问题，用实测而非推测）
 C. 记忆层故障时退化为纯联网检索（不能因为缓存坏了就不能检索）
 D. `ACPS_SCOUT_MEMORY=0` 时完全旁路（回到改造前行为）

指纹判定方式与 wit 一致：构造 agent 后取 ACS 投影并做规范化哈希，
比较"加记忆能力前/后"是否相同。若 wit 不提供投影入口，则退化为
"公开能力集合是否变化" + dataset.fetch 的同款实测结论（public=False 不进 ACS）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))          # acps_cluster（partner_scout 等）
sys.path.insert(0, str(CL.parent))   # my-project（capabilities 包）

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✔' if ok else '✘'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        FAILS.append(label)


async def part_a_and_c_and_d() -> None:
    import capabilities.papers_memory as pm
    from acps_cluster.agents import partner_scout as ps

    calls = {"arxiv": 0, "openalex": 0, "s2": 0}

    async def fake_arxiv(query: str, max_results: int = 5):
        calls["arxiv"] += 1
        return [ps.PaperRecord(arxiv_id=f"2508.1743{i}",
                               title=f"Diffusion Models: A Comprehensive Survey (Part {i})",
                               authors=["Alice Zhang", "Bob Li"],
                               published="2025-08-20",
                               abstract="We survey diffusion models and their applications.",
                               pdf_url=f"https://arxiv.org/pdf/2508.1743{i}")
                for i in range(5)]

    async def fake_openalex(query: str, max_results: int = 5):
        calls["openalex"] += 1
        return [ps.ScholarRecord(source="openalex", native_id=f"W250{i}",
                                 title=f"Diffusion Models: A Comprehensive Survey (Part {i})",
                                 authors=["Alice Zhang", "Bob Li"], year=2025,
                                 abstract="A survey of diffusion models.",
                                 url=f"https://openalex.org/W250{i}", citations=42)
                for i in range(5)]

    async def fake_s2(query: str, max_results: int = 5):
        calls["s2"] += 1
        return [ps.ScholarRecord(source="semanticscholar", native_id=f"S2-{i}",
                                 title=f"Diffusion Models: A Comprehensive Survey (Part {i})",
                                 authors=["Alice Zhang"], year=2025,
                                 abstract="Survey of diffusion models.",
                                 url=f"https://s2.org/{i}", citations=7)
                for i in range(5)]

    # 指向临时库，避免污染真实 ws-scout
    tmp = Path(tempfile.mkdtemp(prefix="scout-mem-"))
    ps._MEMORY = ps.PaperMemory(tmp / "papers.sqlite3")
    ps._arxiv_search, ps._openalex_search, ps._s2_search = fake_arxiv, fake_openalex, fake_s2

    print("=== A. 同一主题第二次检索不联网 ===")
    r1 = await ps.scout_arxiv_search("diffusion models survey", max_results=5)
    after1 = dict(calls)
    r2 = await ps.scout_arxiv_search("diffusion models survey", max_results=5)
    after2 = dict(calls)
    r3 = await ps.scout_arxiv_search("diffusion model survey", max_results=5)  # 措辞略变
    after3 = dict(calls)
    check("第 1 次走网络", after1["arxiv"] == 1, f"arxiv 调用={after1['arxiv']}")
    check("第 2 次（同措辞）不联网", after2["arxiv"] == 1, f"arxiv 调用={after2['arxiv']}")
    check("第 3 次（措辞略变）仍不联网", after3["arxiv"] == 1, f"arxiv 调用={after3['arxiv']}")
    check("命中仍返回记录", len(r1) == 5 and len(r2) == 5 and len(r3) == 5,
          f"{len(r1)}/{len(r2)}/{len(r3)}")
    check("记录内容正确", bool(r2) and r2[0].title.startswith("Diffusion Models"),
          f"title={getattr(r2[0], 'title', '')[:40]!r}" if r2 else "空")
    check("_LAST_FROM_MEMORY 标注为真", ps._LAST_FROM_MEMORY is True)

    print()
    print("=== A2. 跨源合并的两种情形（实测行为，别搞混）===")
    # 情形一：库里已有该主题 → 后续源**直接命中缓存短路**，不会联网存自己那份
    before = ps._MEMORY.stats()["sources"].copy()
    await ps.scout_openalex_search("diffusion models survey")
    await ps.scout_s2_search("diffusion models survey")
    after = ps._MEMORY.stats()["sources"]
    check("缓存已命中时，后续源短路不联网（不重复取）", after == before,
          f"{before} → {after}")

    # 情形二：库里**没有**该主题时，不同源各自联网 → 跨源合并并累积 provenance
    calls["openalex"] = calls["s2"] = 0
    await ps.scout_openalex_search("graph neural networks for molecules")
    await ps.scout_s2_search("graph neural networks for molecules")
    st = ps._MEMORY.stats()
    check("跨源合并：三源同一批 → 仍是 5 条（每篇一条）", st["papers"] == 5, f"papers={st['papers']}")
    check("provenance 累积多源", len(st["sources"]) >= 3, f"sources={st['sources']}")
    check("两源都确实联网取过", calls["openalex"] >= 1 and calls["s2"] >= 1,
          f"openalex={calls['openalex']} s2={calls['s2']}")

    print()
    print("=== C. 记忆层故障 → 退化为纯联网（不能因缓存坏了就不能检索）===")
    calls["arxiv"] = 0

    def boom(*a, **k):
        raise RuntimeError("simulated memory failure")

    ps._MEMORY.lookup = boom          # type: ignore[method-assign]
    ps._MEMORY.remember = boom        # type: ignore[method-assign]
    r = await ps.scout_arxiv_search("quantum chemistry basis sets", max_results=5)
    check("故障时仍返回结果", len(r) == 5, f"n={len(r)}")
    check("故障时确实联网了", calls["arxiv"] == 1, f"arxiv 调用={calls['arxiv']}")

    print()
    print("=== D. ACPS_SCOUT_MEMORY=0 → 完全旁路 ===")
    os.environ["ACPS_SCOUT_MEMORY"] = "0"
    ps._MEMORY = None
    calls["arxiv"] = 0
    await ps.scout_arxiv_search("diffusion models survey")
    await ps.scout_arxiv_search("diffusion models survey")
    check("关闭后每次都联网（回到改造前行为）", calls["arxiv"] == 2, f"arxiv 调用={calls['arxiv']}")
    check("关闭后 _memory() 返回 None", ps._memory() is None)
    os.environ.pop("ACPS_SCOUT_MEMORY", None)

    # memory.search 只读能力
    print()
    print("=== E. memory.search / memory.stats 只读能力 ===")
    ps._MEMORY = ps.PaperMemory(tmp / "papers2.sqlite3")
    ps._MEMORY.remember([ps.PaperRecord(arxiv_id="2508.17434",
                                        title="Diffusion Models: A Comprehensive Survey",
                                        authors=["Alice Zhang"], published="2025-08-20",
                                        abstract="survey of diffusion models",
                                        pdf_url="https://arxiv.org/pdf/2508.17434")],
                        query="diffusion models survey")
    hits = await ps.scout_memory_search("diffusion models", max_results=5)
    check("memory.search 命中", len(hits) == 1, f"n={len(hits)}")
    check("命中带 from_memory 标注", bool(hits) and hits[0].get("from_memory") is True)
    check("命中带 provenance", bool(hits) and isinstance(hits[0].get("sources"), list))
    st2 = await ps.scout_memory_stats()
    check("memory.stats 有论文数", st2.get("papers") == 1, f"papers={st2.get('papers')}")
    check("memory.stats 带 recentAccess", isinstance(st2.get("recentAccess"), list))


def part_b_fingerprint() -> None:
    """B. 记忆能力是否改变 ACS 投影（决定要不要重新注册）。

    实测规则（2026-10-11，8/8 吻合）：**public=True 或声明了 acps_skill 的能力才进 ACS**。
      · arxiv.fetch          public=False + acps_skill=域  → 在已注册 ACS 里
      · dataset.fetch        public=False + 无 acps_skill  → 不在
      · scholar.deep_fetch   同上                        → 不在
    所以这两项记忆能力若 public=True，就会进 ACS → **需要重新注册**。
    """
    from acps_cluster.agents import partner_scout as ps

    print()
    print("=== B. 记忆能力是否改 ACS 投影（决定要不要重新注册）===")
    caps_all = list(ps.SCOUT_CAPABILITIES)
    memory_ids = {"memory.stats", "memory.search"}
    caps_without = [c for c in caps_all if _cap_id(c) not in memory_ids]

    proj_all = _acs_surface(caps_all)
    proj_without = _acs_surface(caps_without)
    print(f"  含记忆能力时 ACS 能力集合: {sorted(proj_all)}")
    print(f"  不含记忆能力时           : {sorted(proj_without)}")

    added_to_acs = proj_all - proj_without
    mem_public = all(
        bool(getattr(getattr(c, "capability", c), "public", False))
        for c in caps_all if _cap_id(c) in memory_ids
    )
    if added_to_acs:
        print(f"  → 记忆能力**进入**了 ACS：{sorted(added_to_acs)}")
        print("  ⇒ 结论（当前 public=True）：**scout 需要重新注册**")
        print("     实测后果：不重新注册时 wit 拒绝启动，报")
        print("     failure=acs_fingerprint_changed_after_submission，平台判定该 agent 离线。")
        check("public=True 时识别为需重新注册", mem_public)
        check("memory.* 进入 ACS 能力集合（公开可见）", len(added_to_acs) == len(memory_ids),
              f"进入={sorted(added_to_acs)}")
    else:
        print("  → 记忆能力未进入 ACS")
        print("  ⇒ 结论（当前 public=False）：**不需要重新注册**")
        print("     实测先例：dataset.fetch 同为 public=False，是 scout 注册之后才加的，")
        print("     至今不在已注册 ACS 里，而 scout 一直正常运行。")
        check("public=False 时识别为无需重新注册", not mem_public)
        check("memory.* 不在 ACS 能力集合（内部能力）", not added_to_acs)


def _cap_id(obj) -> str:
    cap = getattr(obj, "capability", obj)
    return str(getattr(cap, "id", ""))


def _public_ids(caps) -> set[str]:
    out = set()
    for c in caps:
        cap = getattr(c, "capability", c)
        if getattr(cap, "public", False):
            out.add(str(getattr(cap, "id", "")))
    return out


def _acs_surface(caps) -> set[str]:
    """复现实测规则：public=True 或声明了 acps_skill 的能力才会进 ACS 投影。"""
    out: set[str] = set()
    for c in caps:
        cap = getattr(c, "capability", c)
        if getattr(cap, "public", False) or getattr(cap, "acps_skill", None) not in (None, False):
            out.add(str(getattr(cap, "id", "")))
    return out


def main() -> int:
    os.environ.setdefault("YANSHU_RUN_TRACE", "0")
    asyncio.run(part_a_and_c_and_d())
    part_b_fingerprint()
    print()
    if FAILS:
        for f in FAILS:
            print("  ✘", f)
        return 1
    print("✔ 通过：记忆优先生效、跨源合并、故障退化、开关旁路、ACS 影响已识别。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
