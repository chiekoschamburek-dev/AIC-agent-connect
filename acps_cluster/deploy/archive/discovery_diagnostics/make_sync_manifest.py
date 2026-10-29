#!/usr/bin/env python3
"""只读生成文件清单（path/size/sha256），供本地↔服务器同步审计比对。

用法：
    python make_sync_manifest.py --root /path/to/my-project [--root2 ...] --out manifest.json

只读：不修改、不创建任何业务文件（只写 --out 指定的清单文件）。
排除：.git/.venv/__pycache__/.pytest_cache/工作区身份与数据（ws-*/acps、logs、runs、
      tokens、vendor、output、demo_run 产物等）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
from pathlib import Path

SKIP_DIR_NAMES = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".wit", "logs", "runs", "tokens", "vendor", "output", "datasets",
    "eval-private", "_platform_snapshot", "certs-backup-2026-09-24",
}
SKIP_SUFFIXES = (
    ".pyc", ".pyo", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal", ".db", ".log",
    ".tar.gz", ".zip", ".pem", ".key", ".crt", ".jsonl",
)
SKIP_NAME_PARTS = (
    "secret", "password", "credential", "registry-token", "eab",
    # 2026-10-21：与 make_code_sync_list.py 保持同一份排除口径。
    "key", "campus", ".env", "private",
)


def skipped(path: Path) -> bool:
    if path.suffix in SKIP_SUFFIXES or any(path.name.endswith(s) for s in SKIP_SUFFIXES):
        return True
    low = path.name.lower()
    return any(part in low for part in SKIP_NAME_PARTS)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        dirnames[:] = [
            x for x in dirnames
            if x not in SKIP_DIR_NAMES and not x.startswith(".git")
        ]
        for name in filenames:
            f = d / name
            if skipped(f):
                continue
            try:
                rel = f.relative_to(root).as_posix()
                out[rel] = {"size": f.stat().st_size, "sha256": sha256(f)}
            except OSError:
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", required=True, help="要扫描的根目录（可多次）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    files: dict[str, dict] = {}
    for r in args.root:
        root = Path(r)
        if not root.is_dir():
            print(f"（跳过不存在的根：{root}）")
            continue
        for rel, meta in collect(root).items():
            files[f"{root.name}/{rel}" if len(args.root) > 1 else rel] = meta

    payload = {
        "label": args.label or platform.node(),
        "host": platform.node(),
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
        "file_count": len(files),
        "files": dict(sorted(files.items())),
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{payload['label']}: 清单 {len(files)} 个文件 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
