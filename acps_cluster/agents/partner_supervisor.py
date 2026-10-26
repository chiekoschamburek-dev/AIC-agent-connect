"""研枢-Supervisor 常驻入口：leader 常驻在线 + 任务收件箱。

为什么需要（原来只有一次性入口的痛点）：
`supervisor.py` 是**一次性任务进程**（`up_until_ready` → `run` → 打印 → 退出），
每提交一个任务都要重新登录平台账号（共享账号单会话 + 0-300s 启动抖动），而且任务
不跑的时候 leader 不在线、名册里也看不到它。

本入口把 leader 做成**常驻**：上线后不退出，改为轮询一个文件收件箱——
把任务文本写进 `ws-supervisor/inbox/<名字>.txt`，守护进程按文件名顺序执行，
结果写到 `ws-supervisor/outbox/<名字>.md`，任务文件移进 `inbox/done/`。
（用文件而不是新起 HTTP 端口：不引入新依赖、不与 AIP 的 8444/mTLS 端口打架、
systemd 的 `ProtectSystem=full` 下只写 workspace 即可。）

运行：`python acps_cluster/agents/partner_supervisor.py`（systemd 模板 `wit-yanshu@supervisor`
直接可用：`ExecStart=python partner_%i.py`）。一次性跑法仍在 `deploy/run_supervisor.sh`。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acps_cluster.core import bootstrap  # noqa: E402
from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core import cluster_roster  # noqa: E402
from acps_cluster.agents import supervisor  # noqa: E402
from wit import WitRuntime  # noqa: E402

WORKSPACE = supervisor.WORKSPACE
INBOX = WORKSPACE / "inbox"
OUTBOX = WORKSPACE / "outbox"
DONE = INBOX / "done"
POLL_SECONDS = 3.0
# 自愈(2026-10-06 深度任务实测):run 结束但仍有在途出站委派(模型"继续等待"收尾/
# 网关挂起/伙伴未回)→ 收件文件重注入再跑一轮,而不是归档了事。续跑次数有上限。
# 计数与锚点都记在 {stem}.heal.json(2026-10-14 审查修复三轮:①原先数 outbox 产物
# 文件,同名覆盖恒为 1,上限形同虚设;②锚点不能用收件文件 mtime——一次投多个任务时,
# 前一任务的孤儿会落进后一任务的窗口(跨任务串染),改用"首轮开始时刻"留痕、续跑复用;
# ③"在途"判定必须排除"发送即失败"[state 为空/带 local_error],否则重注救不回还白烧轮次)。
_MAX_RESUME = 2
_TERMINAL_STATES = {"completed", "failed", "rejected", "canceled", "cancelled", "expired"}


def _inflight_outbound(workspace: Path, since_iso: str) -> int:
    """统计 run 开始后创建、且**真正在途**的出站委派数(读 ws 的 aip-tasks.sqlite3)。

    只把"已建立且有状态、未到终态、无本地错误"的委派计为在途：
    - state 为空(NULL)= 发送即失败，重注救不回（2026-10-14 审查 N1）；
    - local_error 非空 = 本地侧已判定失败/异常（同上，避免"发送即失败"被当成"伙伴未回"）；
    - awaiting-completion = 伙伴已产出等 leader 关单，不是"卡住"；
    - 启动收敛写的 StaleAfterRestart 行 state=canceled，属终态，天然不受影响。
    """
    db = workspace / "acps" / "aip-tasks.sqlite3"
    if not db.is_file():
        return 0
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT state, local_error FROM aip_tasks"
                " WHERE direction='outbound' AND created_at >= ?",
                (since_iso,),
            ).fetchall()
        finally:
            con.close()
        return sum(
            1
            for (s, err) in rows
            if str(s or "").strip()
            and str(s or "").split("-")[0] not in _TERMINAL_STATES
            and str(s or "") not in _TERMINAL_STATES
            and str(s or "") != "awaiting-completion"
            and str(s or "") != "awaiting-input"
            and not str(err or "").strip()
        )
    except Exception as exc:  # noqa: BLE001  检测失败不阻塞归档主流程
        print(f"[supervisor] 在途检测失败(按 0 处理): {type(exc).__name__}: {exc}", flush=True)
        return 0


def _heal_state(outbox: Path, stem: str) -> dict:
    """读 {stem}.heal.json(锚点/续跑计数留痕;缺失/损坏按空)。"""
    import json

    path = outbox / f"{stem}.heal.json"
    if not path.is_file():
        return {}
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        return rec if isinstance(rec, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_heal_state(outbox: Path, stem: str, data: dict) -> None:
    import json

    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"{stem}.heal.json").write_text(json.dumps(data), encoding="utf-8")


def _task_anchor(outbox: Path, stem: str, started_iso: str) -> str:
    """任务锚点(UTC ISO)= 本任务**首轮开始处理时刻**;首轮写进 {stem}.heal.json
    的 anchor 字段,续跑轮复用。

    为什么不用收件文件 mtime(2026-10-14 审查 A):一次投多个任务、串行处理时,
    前一任务遗留的孤儿委派会落进后一任务的窗口(跨任务串染)——后一任务即使干净
    完成也被判 inflight≥1,白烧续跑轮并被误标 heal=cap-reached;mtime 还可能被
    touch/同步工具改写造成窗口漂移。也不用"本轮开始":上一轮遗留的孤儿会落在
    窗口外(双向失效)。首轮锚点天然排除早于本任务首次开跑的一切记录。
    """
    state = _heal_state(outbox, stem)
    anchor = str(state.get("anchor") or "")
    if anchor:
        return anchor
    state["anchor"] = started_iso
    state.setdefault("count", 0)
    _write_heal_state(outbox, stem, state)
    return started_iso


def _heals_done(outbox: Path, stem: str) -> int:
    """该任务已续跑次数(读留痕文件;缺失/损坏按 0)。"""
    try:
        return int(_heal_state(outbox, stem).get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _bump_heals(outbox: Path, stem: str) -> int:
    """续跑计数 +1(写留痕文件,保留 anchor),返回新值。"""
    from datetime import datetime, timezone

    state = _heal_state(outbox, stem)
    try:
        n = int(state.get("count") or 0) + 1
    except (TypeError, ValueError):
        n = 1
    state["count"] = n
    state["bumpedAt"] = datetime.now(timezone.utc).isoformat()
    _write_heal_state(outbox, stem, state)
    return n


def _heal_decision(
    *, workspace: Path, outbox: Path, stem: str, anchor_iso: str, max_resume: int = _MAX_RESUME
) -> tuple[str, int, int]:
    """续跑裁决:锚点以来仍有在途委派、且续跑次数未到上限 → "reinject";否则 "archive"。

    返回 (action, inflight, heals)。纯裁决,不写盘,供 run_inbox_task 与单测共用。
    """
    inflight = _inflight_outbound(workspace, anchor_iso)
    heals = _heals_done(outbox, stem)
    if inflight and heals < max_resume:
        return "reinject", inflight, heals
    return "archive", inflight, heals


def _annotate_heal(outbox: Path, stem: str, *, inflight: int, heals: int) -> None:
    """达上限强制归档:在产物尾部追加可 grep 的标注行(运维可区分于正常归档)。"""
    import json

    note = json.dumps(
        {"type": "HealCapReached", "inflight": inflight, "heals": heals},
        ensure_ascii=False,
    )
    for name in (f"{stem}.md", f"{stem}.error.txt"):
        path = outbox / name
        if path.is_file():
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"\n<!-- 续跑上限已到,归档时仍有在途出站委派: {note} -->\n")
                    f.write(f"heal=cap-reached inflight={inflight} heals={heals}\n")
            except OSError:
                pass
            return


def _archive_heal_marker(outbox: Path, stem: str) -> None:
    """终局归档时把留痕文件一并移入 done/(避免 outbox 残留)。"""
    src = outbox / f"{stem}.heal.json"
    if not src.is_file():
        return
    DONE.mkdir(parents=True, exist_ok=True)
    target = DONE / src.name
    if target.exists():
        target = DONE / f"{src.stem}-{os.urandom(3).hex()}{src.suffix}"
    try:
        src.replace(target)
    except FileNotFoundError:
        pass


# 启动收敛的留痕（写进 local_error；带原状态与原错误，便于账本复盘与回溯）
def _stale_marker(direction: str, prev_state: str, prior_error: str = "") -> str:
    import json

    payload = {
        "type": "StaleAfterRestart",
        "message": "常驻 supervisor 重启：该任务不可能再推进，启动时收敛为 canceled",
        "direction": direction,
        "previousState": prev_state or None,
    }
    if prior_error:
        # 收敛会整体覆盖 local_error：第一手失败原因（如 ConnectError）不能丢——
        # 且 _inflight_outbound 依赖 local_error 判在途，两处耦合需同步维护
        # （2026-10-15 审查 D）。
        payload["priorLocalError"] = prior_error[:300]
    return json.dumps(payload, ensure_ascii=False)


def _expired_marker(direction: str, prev_state: str, prior_error: str, products_len: int) -> str:
    """出站"产物已到达但无人收尾"的收敛留痕（2026-10-15 审查 C）。

    标 expired 而非 canceled：伙伴已完成并交付（产物在库），canceled="我方取消"
    与事实不符；expired 如实表达"产物到达、未被收集"。
    """
    import json

    payload = {
        "type": "ExpiredOnRestart",
        "message": "常驻 supervisor 重启：伙伴产物已到达（在库）但收集它的 run 已消失，收敛为 expired",
        "direction": direction,
        "previousState": prev_state or None,
        "productsBytes": products_len,
    }
    if prior_error:
        payload["priorLocalError"] = prior_error[:300]
    return json.dumps(payload, ensure_ascii=False)


def _cleanup_stale_tasks(workspace: Path = WORKSPACE, *, logger=print) -> list[tuple[str, str, str]]:
    """启动收敛：把上一进程遗留的非终态任务标记为 canceled（**入站已交付的除外**）。

    进程重启会带走所有在途会话（平台侧早已超时弃权、伙伴侧产物也再无人收集），
    这些任务永远不可能再推进；实测 2026-10-10 全集群攒了 91 条非终态（最早 10-02），
    会污染账本、让"某任务跑没跑完"无法回答，也让"重启前在途检查"永远报警而失效。
    启动这一刻没有任何本站点在途任务（systemd 保证单实例），故无需年龄门限。

    例外是 inbound 的两个等待态：**awaiting-completion** 意味着 `aip_submit` 已成功、
    产物已交付，下一步（`complete`）归调用方；**awaiting-input** 等的是调用方
    （叮当/用户）的补充输入，会话已持久化（`agent_state` 列），重启后对方回话仍可续话。
    两者都与本站进程存活无关——收敛成 canceled 会把"已交付/等你回话"改写成"已取消"：
    已交付误标实测 12 条、产物 0.1~32.5KB（2026-10-15 审查）；等待输入被误收敛见
    2026-10-16 现网（叮当会话任务在 supervisor 重启时被取消）。outbound 的同名状态按产物分流：
    **有产物**（伙伴已交付、在库）→ expired（审查 C：canceled 会把"伙伴已完成"
    记成"我方取消"）；**无产物** → canceled（收集它的 run 随重启消失、永远收不到，
    这也是 `_inflight_outbound` 把 awaiting-completion 排除在"在途"之外的同一理由）。
    收敛失败只告警、不阻塞启动。
    """
    db = workspace / "acps" / "aip-tasks.sqlite3"
    if not db.is_file():
        return []
    try:
        import sqlite3
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        con = sqlite3.connect(db, timeout=10)
        try:
            marks = ",".join("?" * len(_TERMINAL_STATES))
            rows = con.execute(
                "SELECT task_ref, direction, state, coalesce(local_error, ''),"
                " length(coalesce(products, '')) FROM aip_tasks"
                f" WHERE lower(coalesce(state, '')) NOT IN ({marks})"
                " AND NOT (lower(coalesce(direction, '')) = 'inbound'"
                " AND lower(coalesce(state, '')) IN ('awaiting-completion', 'awaiting-input'))",
                tuple(_TERMINAL_STATES),
            ).fetchall()
            to_expire = [
                r for r in rows
                if str(r[1]).lower() == "outbound"
                and str(r[2]).lower() == "awaiting-completion"
                and (r[4] or 0) > 2
            ]
            expire_refs = {r[0] for r in to_expire}
            to_cancel = [r for r in rows if r[0] not in expire_refs]
            if to_expire:
                con.executemany(
                    "UPDATE aip_tasks SET state='expired', updated_at=?, local_error=?"
                    " WHERE task_ref=?",
                    [
                        (now, _expired_marker(str(direction), str(state or ""),
                                              str(prior or ""), int(plen or 0)), ref)
                        for ref, direction, state, prior, plen in to_expire
                    ],
                )
            if to_cancel:
                con.executemany(
                    "UPDATE aip_tasks SET state='canceled', updated_at=?, local_error=?"
                    " WHERE task_ref=?",
                    [
                        (now, _stale_marker(str(direction), str(state or ""), str(prior or "")), ref)
                        for ref, direction, state, prior, _plen in to_cancel
                    ],
                )
            if rows:
                con.commit()
        finally:
            con.close()
        by_dir: dict[str, int] = {}
        for ref, direction, state, _prior, _plen in rows:
            by_dir[str(direction)] = by_dir.get(str(direction), 0) + 1
            shape = "expired（产物已到达）" if ref in expire_refs else "canceled"
            logger(
                f"[supervisor] 启动收敛: {ref} {direction} ({state or 'state=None'}) → {shape}",
                flush=True,
            )
        if rows:
            logger(
                f"[supervisor] 启动收敛完成: 共 {len(rows)} 条陈旧非终态"
                f"（canceled {len(to_cancel)} / expired {len(to_expire)}；"
                f"{', '.join(f'{k} {v}' for k, v in sorted(by_dir.items()))}）",
                flush=True,
            )
        return [(ref, str(direction), str(state or "")) for ref, direction, state, _p, _l in rows]
    except Exception as exc:  # noqa: BLE001  收敛失败不阻塞启动
        logger(f"[supervisor] 启动收敛失败(忽略): {type(exc).__name__}: {exc}", flush=True)
        return []


async def _seed_discovery(script: Path | None = None) -> None:
    """尽力预热发现缓存（缺了它委派会解析不到伙伴端点）；失败不阻塞常驻。

    逐行转发子进程输出到 journal 并按 rc 分档告警（2026-10-15 审查）：原先 stdout/stderr
    都进 DEVNULL、且 rc 只要有一项成功就是 0，于是"9 项掉 1 项"在日志里与全量成功
    长得一模一样（平台证书过期那天就是这么静默丢了 designer 那条）。
    """
    script = script or Path(__file__).resolve().parents[1] / "deploy" / "seed_discovery.py"
    if not script.is_file():
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        summary: list[str] = []
        if proc.stdout is not None:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                print(f"[seed] {line}", flush=True)
                if "缓存预热" in line or "缺" in line:
                    summary.append(line.strip())
        await proc.wait()
        tail = "；".join(summary[-2:]) if summary else "无输出"
        if proc.returncode == 0:
            print(f"[supervisor] 发现缓存预热完成（rc=0；{tail}）", flush=True)
        else:
            print(f"[supervisor] ⚠ 发现缓存预热不完整（rc={proc.returncode}；{tail}）"
                  f"——缺的伙伴委派时会解析不到端点", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[supervisor] 发现缓存预热失败（继续）: {type(exc).__name__}: {exc}", flush=True)


def _archive(path: Path) -> None:
    DONE.mkdir(parents=True, exist_ok=True)
    target = DONE / path.name
    if target.exists():
        target = DONE / f"{path.stem}-{os.urandom(3).hex()}{path.suffix}"
    try:
        path.replace(target)  # exists() 后 replace 不是原子的，竞态窗口靠异常兜住（审查修复）
    except FileNotFoundError:
        print(f"[supervisor] 归档跳过（收件文件已不在）: {path.name}", flush=True)


async def run_inbox_task(runtime, roster, path: Path) -> None:
    """跑一个收件箱任务，结果写 outbox/<stem>.md。任何异常都落到 <stem>.error.txt。"""
    try:
        task = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError) as exc:
        # 读取在 try 之外时，双实例抢同一收件文件（或被移走）会让异常穿过 serve() 的
        # while True 掀掉常驻进程；这里就地跳过并归档，不留 poison 文件反复触发
        print(f"[supervisor] 收件文件不可读，跳过 {path.name}: {type(exc).__name__}: {exc}", flush=True)
        _archive(path)
        return
    print(f"[supervisor] 收到任务 {path.name}: {task[:80]}", flush=True)
    from datetime import datetime, timezone
    started_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    try:
        run = await runtime.run(task, supervisor.build_run_config(roster))
    except Exception as exc:  # noqa: BLE001
        OUTBOX.mkdir(parents=True, exist_ok=True)
        (OUTBOX / f"{path.stem}.error.txt").write_text(
            f"{type(exc).__name__}: {exc}", encoding="utf-8"
        )
        print(f"[supervisor] 任务 {path.name} 异常: {type(exc).__name__}: {exc}", flush=True)
    else:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        (OUTBOX / f"{path.stem}.md").write_text(
            f"# {path.stem}\n\n任务：{task}\n\nstatus={run.status} steps={run.iterations}\n\n"
            f"{run.final_text}\n",
            encoding="utf-8",
        )
        print(f"[supervisor] 任务 {path.name} 完成 status={run.status} steps={run.iterations}", flush=True)
    # ── 自愈:任务锚点以来仍有在途出站委派 → 重注入再跑一轮(不归档,续跑有上限) ──
    # (2026-10-06 深度任务实测:模型"继续等待"收尾/网关挂起都会把在途委派变孤儿;
    #  2026-10-14 审查修复:锚点=首轮开始时刻留痕于 {stem}.heal.json[防跨任务串染],
    #  计数=同一文件的 count 字段[防同名覆盖恒 1],上限归档写 heal=cap-reached 标注)
    anchor_iso = _task_anchor(OUTBOX, path.stem, started_iso)
    action, inflight, heals = _heal_decision(
        workspace=WORKSPACE, outbox=OUTBOX, stem=path.stem, anchor_iso=anchor_iso
    )
    if action == "reinject":
        n = _bump_heals(OUTBOX, path.stem)
        print(f"[supervisor] 自愈:仍有 {inflight} 个在途出站委派 → 重注 {path.name}"
              f"(第 {n}/{_MAX_RESUME} 次续跑)", flush=True)
        return  # 不归档:主循环立即再拾取
    if inflight:
        _annotate_heal(OUTBOX, path.stem, inflight=inflight, heals=heals)
        print(f"[supervisor] 续跑上限({_MAX_RESUME})已到,仍有 {inflight} 个在途委派,"
              f"归档 {path.name} 并标注 heal=cap-reached", flush=True)
    _archive(path)
    _archive_heal_marker(OUTBOX, path.stem)


async def serve() -> None:
    # 启动收敛必须在 wit 接管账本之前：把上一进程遗留的非终态任务收敛掉，
    # 否则它们会以 awaiting-completion/working 永远挂着（见 _cleanup_stale_tasks）。
    _cleanup_stale_tasks()
    provider = bootstrap.build_provider()
    port = int(os.getenv("ACPS_SUPERVISOR_PORT", "8444"))
    roster = cluster_roster.load_roster()
    runtime = WitRuntime(
        supervisor.build_supervisor_agent(provider),
        workspace=WORKSPACE,
        acps=cluster_common.build_acps_config(port),
    )
    INBOX.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    await _seed_discovery()
    try:
        result = await runtime.up_until_ready(
            serve=True,
            host=os.getenv("ACPS_SERVE_HOST", "127.0.0.1"),
            port=port,
            credentials=cluster_common.build_credentials("SUPERVISOR"),
        )
        if not result.ready:
            print(f"[supervisor] 注册未就绪: {result.status} {result.next_action}", flush=True)
            raise SystemExit(2)
        aic = result.identity.aic if result.identity else "?"
        print(f"[supervisor] 常驻已上线 AIC={aic} 收件箱={INBOX}（投递 <名字>.txt 即可）", flush=True)
        while True:
            for path in sorted(INBOX.glob("*.txt")):
                await run_inbox_task(runtime, roster, path)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await runtime.stop_serving()
        await provider.aclose()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
