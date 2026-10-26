#!/usr/bin/env python3
"""补计量断点：给集群全部 agent 挂上 AMP access 钩子（幂等，可反复运行）。

## 为什么需要

平台排行榜的计数口径是 `count = accessCount + messageCount`，而 access 记录只在
`WitRuntime._amp_direct_access_observer()` 检测到 agent 挂了
`AmpAccessLoggingHooks` 时才产生（否则该函数直接返回 None，一条都不写）。
我们此前 `amp_hooks` 恒为空 → 本地 0 个 `amp_access_*.jsonl` → 榜单恒为 0。

详见 `my-project/docs/incident-2026-10-19-ranking-zero.md`。

## 做什么

1. 每个 `partner_*.py` / `supervisor.py` 的 `WitAgent(...)` 里补一行
   `amp_hooks=[AmpAccessLoggingHooks()],`（在 `aip_hooks=[...]` 之后）；
2. 补 `from wit.hooks.acps.amp import AmpAccessLoggingHooks` 导入
   （优先复用已有的 `from wit import ...` 行，避免多一条 import）；
3. 顺带修掉"重启删日志"：把 `deploy/wit-yanshu@.service` 与
   `deploy/run_supervisor.sh` 里的 `logs/*.jsonl` 通配收窄成 `amp_heartbeat_*.jsonl`
   ——否则 access 证据每重启一次就被清空。

**不碰 ACS 指纹**：`amp_hooks` 与 import 都不进 ACS 投影，改完不会触发
`acs_fingerprint_changed_after_submission`。

## 用法

    # 1) 先看会改什么（不落盘）
    python3 deploy/patch_amp_access.py --dry-run

    # 2) 只试点 writer 一个
    python3 deploy/patch_amp_access.py --only writer

    # 3) 确认没问题后全量
    python3 deploy/patch_amp_access.py

    # 4) 回滚（有 .bak 备份）
    python3 deploy/patch_amp_access.py --revert

改完必须重启对应服务才生效（钩子是在 build_agent 时装配的）：

    systemctl restart wit-yanshu@writer
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]

# 需要挂载量的 agent 模块（partner_<key>.py + supervisor.py）
TARGETS = [
    "supervisor",
    "partner_scout",
    "partner_synthesis",
    "partner_writer",
    "partner_critic",
    "partner_contradiction",
    "partner_frontier",
    "partner_experiment",
    "partner_gpu",
    "partner_cpu",
    "partner_eval",
    "partner_kb",
    "partner_model",
    "partner_citation",
]

SERVICE_UNIT = CLUSTER / "deploy" / "wit-yanshu@.service"
RUN_SUPERVISOR = CLUSTER / "deploy" / "run_supervisor.sh"

IMPORT_LINE = "from wit.hooks.acps.amp import AmpAccessLoggingHooks"
MARK = "amp_hooks=[AmpAccessLoggingHooks()]"

# 已有的 from wit import (...) —— 优先把钩子并进去，少一行 import
WIT_IMPORT_BLOCK = re.compile(r"^from wit import \($(.*?)^\)$", re.MULTILINE | re.DOTALL)


def _has_amp_hooks(text: str) -> bool:
    return re.search(r"^\s*amp_hooks\s*=", text, re.MULTILINE) is not None


def _ensure_import(text: str) -> tuple[str, bool]:
    """确保 AmpAccessLoggingHooks 可解析。返回 (新文本, 是否改动)。"""
    if "AmpAccessLoggingHooks" in text:
        return text, False

    def _merge(m: re.Match[str]) -> str:
        body = m.group(1).rstrip()
        return f"from wit import (\n{body}\n    AmpAccessLoggingHooks,\n)"

    new_text, n = WIT_IMPORT_BLOCK.subn(_merge, text, count=1)
    if n:
        return new_text, True

    # 没有多行 import 块：找第一条 `from wit import ...` 单行，改成两行
    m = re.search(r"^from wit import ([^\n(]+)$", text, re.MULTILINE)
    if m:
        line = m.group(0)
        return text.replace(line, f"{line}\n{IMPORT_LINE}", 1), True

    # 连 from wit 都没有：退到模块顶部，但**必须让开 `from __future__`**
    # （它必须是文件里第一条语句，插到它前面会直接 SyntaxError——
    #   partner_kb.py 就是这样，2026-10-19 自测抓到）。
    lines = text.splitlines()
    insert_at = 0
    for i, ln in enumerate(lines[:60]):
        if re.match(r"^from\s+__future__\s+import\s", ln):
            insert_at = i + 1          # 紧跟最后一条 __future__ import
    if insert_at == 0:
        for i, ln in enumerate(lines[:60]):
            if re.match(r"^(import|from)\s", ln):
                insert_at = i
                break
    lines.insert(insert_at, IMPORT_LINE)
    return "\n".join(lines) + "\n", True


def _add_amp_hooks(text: str) -> tuple[str, bool]:
    """在 WitAgent(...) 的 aip_hooks= 之后插入 amp_hooks。返回 (新文本, 是否改动)。"""
    if _has_amp_hooks(text):
        return text, False

    m = re.search(r"^(\s*)aip_hooks=\[", text, re.MULTILINE)
    if not m:
        return text, False
    indent = m.group(1)
    # 找到该 aip_hooks=[...] 的结束 '],'（同一行或跨行）
    start = m.end() - 1  # 指向 '['
    depth, i = 0, start
    while i < len(text):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    else:
        return text, False

    end = i + 1
    # 吃掉紧随其后的逗号（若有）
    tail = text[end:]
    if tail.startswith(","):
        end += 1
    insertion = f"\n{indent}{MARK},"
    return text[:end] + insertion + text[end:], True


def patch_module(path: Path, dry_run: bool) -> str:
    text = path.read_text(encoding="utf-8")
    orig = text
    notes = []

    if _has_amp_hooks(text):
        return "已挂载，跳过"

    text, changed_import = _ensure_import(text)
    if changed_import:
        notes.append("+ import")

    text, changed_hooks = _add_amp_hooks(text)
    if not changed_hooks:
        return "未找到 aip_hooks 锚点，跳过（需人工确认）"
    notes.append("+ amp_hooks")

    if dry_run:
        return f"将修改（{'，'.join(notes)}）"

    # 先编译再落盘：改坏了宁可不动（2026-10-19 自测踩过 __future__ 位置问题）
    try:
        compile(text, str(path), "exec")
    except SyntaxError as exc:
        return f"✘ 跳过（改后语法错误：{exc.msg} @ line {exc.lineno}）"

    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text, encoding="utf-8")
    return f"已修改（{'，'.join(notes)}），备份 .bak"


def patch_unit(dry_run: bool) -> list[str]:
    """收窄重启清理的通配符，别把 access 证据一起删掉。"""
    out: list[str] = []
    for path in (SERVICE_UNIT, RUN_SUPERVISOR):
        if not path.is_file():
            out.append(f"{path.name}: 不存在，跳过")
            continue
        text = path.read_text(encoding="utf-8")
        if "amp_heartbeat_*.jsonl" in text:
            out.append(f"{path.name}: 已收窄，跳过")
            continue
        new = text.replace("logs/*.jsonl", "logs/amp_heartbeat_*.jsonl")
        if new == text:
            out.append(f"{path.name}: 未匹配到 logs/*.jsonl，跳过")
            continue
        if not dry_run:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            path.write_text(new, encoding="utf-8")
        out.append(f"{path.name}: 清理范围收窄为 amp_heartbeat_*.jsonl")
    return out


def revert() -> None:
    for bak in sorted(CLUSTER.glob("*.py.bak")) + sorted(CLUSTER.glob("deploy/*.bak")):
        target = Path(str(bak)[: -4])
        shutil.copy2(bak, target)
        print(f"已回滚 {target.name}")
    print("\n回滚完成。记得重启服务让旧代码生效。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给集群 agent 挂 AMP access 钩子（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么，不落盘")
    ap.add_argument("--only", action="append", default=None,
                    help="只处理指定模块，如 --only writer（可重复）")
    ap.add_argument("--revert", action="store_true", help="从 .bak 回滚")
    ap.add_argument("--skip-units", action="store_true", help="不处理 systemd/脚本的清理范围")
    args = ap.parse_args(argv)

    if args.revert:
        revert()
        return 0

    names = TARGETS
    if args.only:
        wanted = {n.removeprefix("partner_").removesuffix(".py") for n in args.only}
        names = [n for n in TARGETS
                 if (n.removeprefix("partner_") in wanted) or (n in wanted)]

    print(f"模式: {'【dry-run】只看不改' if args.dry_run else '【真改，先备份 .bak】'}")
    print(f"目标: {len(names)} 个模块\n")

    changed = 0
    for name in names:
        path = CLUSTER / f"{name}.py"
        if not path.is_file():
            print(f"  {name:22s} 文件不存在，跳过")
            continue
        result = patch_module(path, args.dry_run)
        if result.startswith(("已修改", "将修改")):
            changed += 1
        print(f"  {name:22s} {result}")

    if not args.skip_units:
        print("\n重启日志清理范围:")
        for line in patch_unit(args.dry_run):
            print(f"  {line}")

    print(f"\n合计 {changed} 个模块{'待' if args.dry_run else '已'}修改。")
    if changed and not args.dry_run:
        print("\n下一步（务必逐条确认后再全量重启）:")
        print("  1. 先只重启试点: systemctl restart wit-yanshu@writer")
        print("  2. 看日志文件:   ls -l ws-writer/logs/amp_access_*.jsonl")
        print("  3. 打一次真实调用后再看: tail -1 ws-writer/logs/amp_access_*.jsonl")
        print("  4. 确认无误再齐: systemctl restart wit-yanshu@{scout,synthesis,critic,contradiction,frontier,experiment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
