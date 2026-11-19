"""实验③ Workspace 四分区受控工作区——对照实验 + 量化。

对照=普通业务写入（裸 Path.write_text，无分区/身份/台账约束）
   vs 受控工作区（ws_create + ws_write，四分区 + 令牌派生身份 + sha256 台账）。

逐项控制变量：写入内容相同，唯一变量是"是否走受控工作区能力"。

三组指标：
1. 越界写入拒绝率：repo 只读、非四分区路径、.. 走廊越分区、products 覆盖、
   shared 乐观锁冲突——这些越界在"普通写入"里全部成功写入（无约束），
   在受控工作区里全部被拒。
2. 身份伪造拦截率：scratch 领地强制前缀——冒写别人领地 scratch/victim/x
   在普通写入里落到 victim 目录（伪造成功），在受控工作区里被服务端归位到
   调用方自己 actor 领地，victim 目录零污染。
3. 归因记录完整率：受控写入逐条落 manifest.jsonl（actor/path/bytes/sha256/prev_sha256），
   校验 sha256 与文件内容一致 + prev_sha256 链连续；普通写入零台账。

全部用模块级纯函数 + 临时目录，自包含可复现。输出 out/exp3_workspace.json。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CLUSTER = ROOT / "acps_cluster"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CLUSTER))

from acps_cluster.agents import partner_workspace as pw  # noqa: E402

TID = "task-ablation-1"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    (HERE / "out").mkdir(exist_ok=True)
    tmp = tempfile.TemporaryDirectory(prefix="ablation-ws-")
    root = Path(tmp.name)
    pw.ws_create(root, TID)
    task_dir = root / TID

    # ═══════════ 1. 越界写入拒绝率 ═══════════
    # (名称, 目标相对路径, 内容, 额外kw)
    cases = [
        ("repo 只读区写入", "repo/config.yaml", "恶意改配置", {}),
        ("非四分区路径", "outside/steal.txt", "越界", {}),
        (".. 走廊越出 shared 分区", "shared/../escape.md", "越区", {}),
        ("products 覆盖写(if_match)", "products/x/v1.md", "覆盖", {"if_match": "deadbeef"}),
    ]
    boundary_rows = []
    for name, rel, content, kw in cases:
        # 对照：普通业务写入——裸写，无分区概念，路径原样落盘（先建父目录）
        plain_target = task_dir / rel
        plain_blocked = False
        try:
            plain_target.parent.mkdir(parents=True, exist_ok=True)
            plain_target.write_text(content, encoding="utf-8")
        except Exception:
            plain_blocked = True
        plain_result = "拒绝" if plain_blocked else "成功写入"

        # 实验：受控工作区
        out = pw.ws_write(root, TID, rel, content, actor="worker", **kw)
        governed_result = "成功" if out.get("ok") else f"拒绝({out.get('error')})"

        boundary_rows.append({
            "name": name, "plain": plain_result, "governed": governed_result,
            "plain_blocked": plain_blocked, "governed_blocked": not out.get("ok", False),
        })

    bound_plain_block = sum(1 for r in boundary_rows if r["plain_blocked"])
    bound_gov_block = sum(1 for r in boundary_rows if r["governed_blocked"])

    # ═══════════ 2. 身份伪造拦截率（scratch 领地强制前缀）═══════════
    # 冒写别人（victim）的领地
    victim_rel = "scratch/victim/secret.md"
    payload = "伪造受害者身份写入"
    # 对照：普通写入——原样落到 victim 领地（伪造成功）
    plain_victim = task_dir / victim_rel
    plain_victim.parent.mkdir(parents=True, exist_ok=True)
    plain_victim.write_text(payload, encoding="utf-8")
    plain_forgery_success = plain_victim.exists()

    # 实验：受控工作区——worker 冒写 victim 领地。
    # 服务端把路径强制归位到 scratch/<actor>/ 之下：
    # scratch/victim/secret.md → scratch/worker/victim/secret.md，victim 领地零新增文件。
    # 先清掉对照侧污染，才能干净判断 victim 目录是否被受控侧写入。
    plain_victim.unlink(missing_ok=True)
    forged = pw.ws_write(root, TID, victim_rel, payload, actor="worker")
    worker_land = task_dir / "scratch" / "worker" / "victim" / "secret.md"
    victim_dir = task_dir / "scratch" / "victim"
    victim_polluted = victim_dir.exists() and any(victim_dir.rglob("*"))
    # 判定：内容落到 worker 自己领地 且 victim 领地未被写入 = 身份伪造被拦截
    gov_relocated = (worker_land.exists() and worker_land.read_text(encoding="utf-8") == payload
                     and not victim_polluted)
    gov_forgery_blocked = bool(forged.get("ok")) and gov_relocated

    # 再做 3 组同类伪造，算比例
    forge_cases = [
        ("scratch/victim/a.md", "a"), ("scratch/victim/b.md", "b"),
        ("scratch/other/c.md", "c"),
    ]
    forge_hits = 0
    for rel, txt in forge_cases:
        # rel 形如 scratch/victim/a.md → 受控侧归位到 scratch/attacker/victim/a.md
        out = pw.ws_write(root, TID, rel, txt, actor="attacker")
        segs = rel.split("/")  # [scratch, victim, a.md]
        landed = task_dir / "scratch" / "attacker" / segs[1] / segs[2]
        foreign_dir = task_dir / "scratch" / segs[1]
        foreign_polluted = foreign_dir.exists() and any(foreign_dir.rglob("*"))
        if out.get("ok") and landed.exists() and not foreign_polluted:
            forge_hits += 1
    forge_total = 1 + len(forge_cases)  # 含上面第一组
    forge_blocked = (1 if gov_forgery_blocked else 0) + forge_hits

    # ═══════════ 3. 归因完整率（sha256 留痕）═══════════
    # prev_sha256 语义 = 同一路径被覆盖前的旧哈希（首次写为空串），
    # 所以哈希链要针对同一路径重复写才测得出；不同路径首次写的 prev 自然是空。
    manifest = task_dir / "manifest.jsonl"
    before = len(manifest.read_text(encoding="utf-8").splitlines()) if manifest.exists() else 0

    # 3a) 5 个不同产物：每条 sha256 必须等于落盘文件实际内容哈希 + actor 齐全
    for i in range(5):
        content = f"产物内容-{i}-" + "x" * (i * 10)
        pw.ws_write(root, TID, f"shared/result-{i}.md", content, actor="worker")

    # 3b) 同一路径覆盖写 3 次：验证 prev_sha256 链条（第2条的 prev == 第1条的 sha256 ...）
    for v in range(3):
        pw.ws_write(root, TID, "shared/versioned.md", f"版本{v}", actor="worker")

    lines = [json.loads(x) for x in manifest.read_text(encoding="utf-8").splitlines()]
    new_records = [r for r in lines[before:] if r.get("action") == "write"]
    # 哈希一致率：每条记录的 sha256 == 文件当前实际内容哈希（对 versioned.md 取最新版本）
    integrity_ok = 0
    for rec in new_records:
        fp = task_dir / rec["path"]
        # 同一路径被覆盖后文件是最新内容，历史记录无法再对上——只对"当前仍是该记录内容"的校验
        if fp.exists() and sha(fp.read_bytes()) == rec.get("sha256") and rec.get("actor"):
            integrity_ok += 1
    # 归因完整率主指标：所有写记录都带齐 actor/path/bytes/sha256 四个字段
    fields_ok = sum(1 for r in new_records
                    if all(k in r and r[k] not in (None, "") for k in ("actor", "path", "bytes", "sha256")))
    attr_total = len(new_records)
    # 哈希链：versioned.md 的 3 条记录，prev_sha256 逐条承接上一条 sha256
    ver_records = [r for r in new_records if r["path"] == "shared/versioned.md"]
    chain_ok = all(ver_records[i]["prev_sha256"] == ver_records[i - 1]["sha256"]
                   for i in range(1, len(ver_records))) and ver_records[0]["prev_sha256"] == ""
    chain_links = len(ver_records) - 1
    chain_verified_links = sum(1 for i in range(1, len(ver_records))
                               if ver_records[i]["prev_sha256"] == ver_records[i - 1]["sha256"])
    # 普通写入：零台账（受控侧才有 manifest.jsonl）
    # 单独造一个纯普通写入目录证明零归因
    plain_root = Path(tempfile.mkdtemp(prefix="ablation-plain-"))
    for i in range(5):
        (plain_root / f"result-{i}.md").write_text(f"产物-{i}", encoding="utf-8")
    plain_has_manifest = any("manifest" in p.name.lower() for p in plain_root.iterdir())

    payload_out = {
        "experiment": "③ Workspace 四分区受控工作区对照",
        "control": "普通业务写入（裸 Path.write_text，无分区/身份/台账）",
        "treatment": "受控工作区（四分区 + 令牌派生身份 + sha256 台账）",
        "boundary": {
            "total": len(cases),
            "plain_blocked": bound_plain_block,
            "governed_blocked": bound_gov_block,
            "plain_block_rate_pct": round(bound_plain_block / len(cases) * 100, 1),
            "governed_block_rate_pct": round(bound_gov_block / len(cases) * 100, 1),
            "cases": boundary_rows,
        },
        "identity_forgery": {
            "total": forge_total,
            "blocked": forge_blocked,
            "block_rate_pct": round(forge_blocked / forge_total * 100, 1),
            "plain_forgery_success": plain_forgery_success,
            "governed_relocated_to_worker": gov_relocated,
        },
        "attribution": {
            "writes": attr_total,
            "records_with_all_fields": fields_ok,
            "field_completeness_rate_pct": round(fields_ok / attr_total * 100, 1) if attr_total else 0,
            "records_hash_matches_file": integrity_ok,
            "hash_match_rate_pct": round(integrity_ok / attr_total * 100, 1) if attr_total else 0,
            "prev_sha256_chain": {
                "versioned_writes": len(ver_records),
                "links": chain_links,
                "links_verified": chain_verified_links,
                "chain_intact": chain_ok,
            },
            "plain_has_manifest": plain_has_manifest,
        },
    }

    out_path = HERE / "out" / "exp3_workspace.json"
    out_path.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 78)
    print("实验③ Workspace 四分区受控工作区对照")
    print("=" * 78)
    print("【越界写入拒绝率】对照=普通写入 vs 受控工作区")
    for r in boundary_rows:
        print(f"  {r['name']:<26} 普通={r['plain']:<10} 受控={r['governed']}")
    print(f"  拒绝率：普通 {payload_out['boundary']['plain_block_rate_pct']}% → "
          f"受控 {payload_out['boundary']['governed_block_rate_pct']}%")
    print("-" * 78)
    print("【身份伪造拦截率】scratch 领地强制前缀")
    print(f"  普通写入：冒写 victim 领地 {'成功（伪造得逞）' if plain_forgery_success else '失败'}")
    print(f"  受控工作区：worker 冒写被归位到自己领地 = {gov_relocated}")
    print(f"  拦截率：{forge_blocked}/{forge_total} = {payload_out['identity_forgery']['block_rate_pct']}%")
    print("-" * 78)
    print("【归因记录完整率】actor/path/bytes/sha256 留痕 + prev_sha256 链")
    print(f"  受控写入 {attr_total} 条，四字段齐全 {fields_ok} 条 = "
          f"{payload_out['attribution']['field_completeness_rate_pct']}%")
    print(f"  sha256 与落盘文件一致 {integrity_ok}/{attr_total} = "
          f"{payload_out['attribution']['hash_match_rate_pct']}%")
    print(f"  同一路径覆盖 {len(ver_records)} 次的 prev_sha256 链："
          f"{chain_verified_links}/{chain_links} 环承接，完整={chain_ok}")
    print(f"  普通写入目录有台账：{plain_has_manifest}")
    print("=" * 78)
    print(f"明细：{out_path}")
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
