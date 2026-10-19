"""scout 论文记忆层单测：canonical 去重 / 命中口径 / TTL / provenance。

依据（2026-10-11 实测）：集群历史检索主题重复率 ≈45%（唯一主题 58 / 105），
所以"同一主题第二次检索命中记忆、不再联网"是核心价值；本文件把这几点钉住。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

CL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CL))
sys.path.insert(0, str(CL.parent))

from capabilities.papers_memory import (  # noqa: E402
    PaperMemory,
    canonical_key,
    keywords,
    normalize_title,
    record_fields,
    title_fingerprint,
)


@dataclass
class _Paper:
    arxiv_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    published: str = ""
    abstract: str = ""
    pdf_url: str = ""


@dataclass
class _Scholar:
    source: str
    native_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    abstract: str = ""
    url: str = ""
    citations: int | None = None


def main() -> int:
    failures: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="papers-memory-"))
    db = tmp / "papers.sqlite3"
    mem = PaperMemory(db)

    # 1) canonical 键优先级
    k1 = canonical_key(record_fields(_Paper("2508.17434", "A Survey", ["Alice"])))
    k2 = canonical_key(record_fields(_Scholar("openalex", "W123", "A Survey", ["Alice"], 2025)))
    k3 = canonical_key(record_fields(_Scholar("openalex", "W999", "", ["Bob"])))
    print(f"  [1] arXiv 记录 → {k1}")
    print(f"      OpenAlex 记录 → {k2}")
    print(f"      无 id 无标题 → {k3}")
    if k1 != "arxiv:2508.17434":
        failures.append(f"[1] arXiv canonical 键异常：{k1}")
    if not k2.startswith("openalex:"):
        failures.append(f"[1] source:native_id 键异常：{k2}")

    # 2) 同一篇论文从两个源进来 → 只存一条，provenance 累积两个源
    mem.remember([_Paper("2508.17434", "Diffusion Models: A Survey",
                         ["Alice Zhang"], "2025-08-20",
                         "We survey diffusion models.", "https://arxiv.org/pdf/2508.17434")],
                 query="diffusion models survey")
    mem.remember([_Scholar("openalex", "W2508", "Diffusion Models: A Survey",
                           ["Alice Zhang"], 2025,
                           "We survey diffusion models in depth.",
                           "https://openalex.org/W2508", 42)],
                 query="diffusion models survey")
    st = mem.stats()
    print(f"  [2] 两源各存一次 → papers={st['papers']}（期望 1，跨源合并）")
    print(f"      sources={st['sources']}")
    if st["papers"] != 1:
        failures.append(f"[2] 跨源未合并：papers={st['papers']}")
    if len(st["sources"]) < 2:
        failures.append(f"[2] provenance 未累积两个源：{st['sources']}")

    # 3) 同查询再存一次 → merged，不新增
    r = mem.remember([_Paper("2508.17434", "Diffusion Models: A Survey", ["Alice Zhang"])],
                     query="diffusion models survey")
    print(f"  [3] 重复 remember → added={r['added']} merged={r['merged']}（期望 0/1）")
    if r["added"] != 0 or r["merged"] != 1:
        failures.append(f"[3] 去重失败：{r}")

    # 4) 命中口径：同主题不同措辞应命中
    for q, want in (("diffusion models survey", True),
                    ("diffusion model survey", True),
                    ("survey of diffusion models", True),
                    ("quantum chemistry basis sets", False)):
        hit = mem.lookup(q, limit=5)
        got = bool(hit)
        print(f"  [4] lookup({q!r}) → {len(hit)} 条  {'✔' if got == want else '✘'}")
        if got != want:
            failures.append(f"[4] 命中口径不符: {q!r} → {len(hit)}")

    # 5) 命中记录带出处字段（供 from_memory 标注与排查）
    row = mem.lookup("diffusion models survey", limit=1)
    if row:
        keys = {"title", "url", "sources", "refreshed_at", "citations"} & set(row[0])
        print(f"  [5] 命中记录含字段 {sorted(keys)}  citations={row[0].get('citations')}")
        if "sources" not in row[0]:
            failures.append("[5] 命中记录缺 provenance")
    else:
        failures.append("[5] 应命中却为空")

    # 6) TTL：超过 TTL 的记录不再命中
    mem2 = PaperMemory(tmp / "papers2.sqlite3", ttl_days=1.0)
    mem2.remember([_Paper("2401.00001", "Ancient Paper On Diffusion", ["Old Author"])],
                  query="ancient paper diffusion")
    hit_now = mem2.lookup("ancient paper diffusion", limit=5)
    # 手动把 refreshed_at 推到 10 天前
    mem2._con.execute("UPDATE papers SET refreshed_at=?", (time.time() - 10 * 86400,))
    mem2._con.commit()
    hit_old = mem2.lookup("ancient paper diffusion", limit=5)
    print(f"  [6] TTL=1天：刚存命中={len(hit_now)}  10 天前={len(hit_old)}（期望 >0 与 =0）")
    if not hit_now or hit_old:
        failures.append(f"[6] TTL 未生效：新鲜={len(hit_now)} 过期={len(hit_old)}")
    st2 = mem2.stats()
    print(f"      stats: papers={st2['papers']} fresh={st2['fresh']} stale={st2['stale']}")
    if st2["stale"] < 1:
        failures.append("[6] stats 未体现 stale")

    # 7) 关键词/归一化工具
    kw = keywords("Diffusion Models — A Survey!!")
    nt = normalize_title("Diffusion Models: A Survey")
    print(f"  [7] keywords={kw}")
    print(f"      normalize_title={nt!r}")
    if "diffusion" not in kw or nt != "diffusion models a survey":
        failures.append(f"[7] 归一化异常：{kw} / {nt!r}")
    fp1 = title_fingerprint("Diffusion Models: A Survey", "Alice")
    fp2 = title_fingerprint("diffusion  models  a survey ", "Bob")
    print(f"      标题指纹对大小写/空白/标点不敏感: {fp1 == fp2}")
    if fp1 != fp2:
        failures.append("[7] 标题指纹不够鲁棒")

    # 8) 空查询不得命中（避免"查什么都能命中"）
    empty = mem.lookup("   ", limit=5)
    print(f"  [8] 空查询 → {len(empty)} 条（期望 0）")
    if empty:
        failures.append("[8] 空查询命中了记录")

    # 9) 相关性下限（2026-10-16 现网事故回归）：泛词（diffusion/generative…）少量命中
    #    不得算"命中"——否则弱相关记录短路联网检索。事故实况：扩散查询（12 词）被
    #    仅含 diffusion+generative 的医学记录以 2 词命中截胡。
    mem3 = PaperMemory(tmp / "papers3.sqlite3")
    mem3.remember([_Paper("2406.17608",
                          "Test-time generative augmentation for medical image segmentation",
                          ["Xiao Ma"], "2024-06-25",
                          "We use diffusion inversion for test-time augmentation in segmentation.",
                          "https://arxiv.org/abs/2406.17608")],
                  query="medical segmentation")
    q_long = ("denoising diffusion probabilistic models score-based generative modeling "
              "latent diffusion diffusion transformer sampling distillation")  # 12 词 → 下限 3
    weak = mem3.lookup(q_long, limit=10)
    print(f"  [9] 泛词弱命中（仅 diffusion+generative 两词）→ {len(weak)} 条（期望 0）")
    if weak:
        failures.append(f"[9] 弱命中未被相关性下限过滤：{len(weak)} 条")
    strong = mem3.lookup("medical image segmentation augmentation", limit=10)
    print(f"      同库强命中（画像查询）→ {len(strong)} 条（期望 >0，下限不误伤真命中）")
    if not strong:
        failures.append("[9] 相关性下限误伤了真命中")
    mem3.close()

    # 10) 泛词污染行（models/based/score-only 命中）不得被长查询命中（2026-10-16 标定）
    mem4 = PaperMemory(tmp / "papers4.sqlite3")
    mem4.remember([_Paper("2510.13343", "AOAD-MAT: Transformer-based multi-agent decision model",
                          ["Shota Takayama"], "2025-10-15",
                          "Multi-agent models based on transformer score strong results.",
                          "https://arxiv.org/abs/2510.13343")], query="multi-agent rl")
    q7 = "denoising diffusion probabilistic models DDPM score-based"
    got4 = mem4.lookup(q7, limit=10)
    print(f"  [10] 泛词污染行 vs 7 词查询 → {len(got4)} 条（期望 0）")
    if got4:
        failures.append(f"[10] 泛词行仍被命中：{len(got4)}")
    mem4.close()

    mem.close()
    mem2.close()
    print()
    if failures:
        for f in failures:
            print("  ✘", f)
        return 1
    print("✔ 通过：canonical 去重、跨源合并、命中口径、TTL、provenance、边界均正确。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
