#!/bin/sh
# 真实端到端验证：真检索 → 入库 → 同主题第二次走记忆（不联网）
# 注意：这会真的访问 arXiv/OpenAlex（scout 的记忆库会因此有真实数据，符合预期用途）
CL=/opt/wit/my-project/acps_cluster
cd "$CL" || exit 1
sudo -u wit YANSHU_RUN_TRACE=0 /opt/wit/venv/bin/python - <<'PY' 2>&1 | tail -40
import asyncio, os, sys, time
from pathlib import Path

CL = Path('/opt/wit/my-project/acps_cluster')
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, str(CL))
os.environ.setdefault('YANSHU_RUN_TRACE', '0')

import partner_scout as ps

QUERY = "diffusion models medical image segmentation"

async def main():
    print(f"  查询: {QUERY!r}")
    print()
    # 第一次：应联网
    t0 = time.time()
    r1 = await ps.scout_arxiv_search(QUERY, max_results=3)
    d1 = time.time() - t0
    print(f"  [1] 第 1 次（期望联网）: {len(r1)} 条  用时 {d1*1000:.0f} ms  "
          f"from_memory={ps._LAST_FROM_MEMORY}")
    for p in r1[:3]:
        print(f"        - {p.title[:66]}")

    # 记录调用计数：把联网函数换成会抛异常的桩，验证第二次不联网
    async def must_not_call(**kw):
        raise AssertionError("第二次检索竟然联网了！")
    real = ps._arxiv_search
    ps._arxiv_search = must_not_call
    try:
        t0 = time.time()
        r2 = await ps.scout_arxiv_search(QUERY, max_results=3)
        d2 = time.time() - t0
        print(f"  [2] 第 2 次（联网被禁用）: {len(r2)} 条  用时 {d2*1000:.0f} ms  "
              f"from_memory={ps._LAST_FROM_MEMORY}")
        ok2 = len(r2) > 0 and ps._LAST_FROM_MEMORY
    except AssertionError as exc:
        print(f"  [2] ✘ {exc}")
        ok2 = False
    finally:
        ps._arxiv_search = real

    # 换措辞
    ps._arxiv_search = must_not_call
    try:
        t0 = time.time()
        r3 = await ps.scout_arxiv_search("diffusion model medical segmentation", max_results=3)
        d3 = time.time() - t0
        print(f"  [3] 换措辞（联网仍被禁用）: {len(r3)} 条  用时 {d3*1000:.0f} ms  "
              f"from_memory={ps._LAST_FROM_MEMORY}")
        ok3 = len(r3) > 0
    except AssertionError as exc:
        print(f"  [3] ✘ {exc}")
        ok3 = False
    finally:
        ps._arxiv_search = real

    print()
    print("  --- 记忆库统计")
    st = await ps.scout_memory_stats()
    for k in ("enabled", "papers", "fresh", "stale", "sources", "ttlDays", "path", "fts"):
        print(f"      {k:10s} = {st.get(k)}")
    print(f"      recentAccess = {st.get('recentAccess')}")

    print()
    if r1 and ok2 and ok3:
        print("  ✔ 通过：真实检索入库，同主题第二次起走记忆（毫秒级、不联网）")
    else:
        print("  ✘ 未达预期")

asyncio.run(main())
PY
