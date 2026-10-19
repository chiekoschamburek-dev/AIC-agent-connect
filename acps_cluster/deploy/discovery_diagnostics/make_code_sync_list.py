"""生成"只同步代码"的文件清单（排除身份/凭据/数据/文档），供打包推送。

安全约束（硬性）：
- 排除 `.env*`（服务器那份含真实凭据，绝不能被本地覆盖）
- 排除 `ws-*/acps/**`（身份：enrollment/ACS/证书/密钥/数据库）
- 排除任一目录下的 *secret*/*password*/*credential*/*token*/*eab* 文件
- 排除 `caps/`（能力实现）——服务器侧有独立副本，避免覆盖
- 只保留代码与运维件扩展名
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

MANIFEST = Path(sys.argv[1])          # sync-local.json
OUT = Path(sys.argv[2])               # 输出文件列表

KEEP_SUFFIX = (".py", ".sh", ".service", ".md", ".toml", ".cfg", ".txt", ".j2")
DROP_GLOBS = (
    "*/ws-*/*", "ws-*/*",
    "*/.env*", ".env*",
    "*/capabilities/*", "capabilities/*",
    "*/demo_run/*", "*/archive/*", "*/vendor/*", "*/tokens/*", "*/output/*",
    "*/datasets/*", "*/eval-private/*", "*/policy-projections/*",
    "*certs-backup*", "*/_platform_snapshot/*",
    "*/docs/_competition/*",
    "*/deploy/validate_acs.py",        # 依赖 registry 仓库 schema，服务器未用
)
DROP_NAME_PARTS = ("secret", "password", "credential", "registry-token", "eab")

data = json.loads(MANIFEST.read_text(encoding="utf-8"))
kept, dropped = [], 0
for rel in data["files"]:
    low = rel.lower()
    if not rel.endswith(KEEP_SUFFIX):
        dropped += 1
        continue
    if any(fnmatch.fnmatch(rel, g) for g in DROP_GLOBS):
        dropped += 1
        continue
    if any(p in Path(rel).name.lower() for p in DROP_NAME_PARTS):
        dropped += 1
        continue
    kept.append(rel)

OUT.write_text("\n".join(sorted(kept)) + "\n", encoding="utf-8")
print(f"保留 {len(kept)} 个 / 排除 {dropped} 个 → {OUT}")
for r in sorted(kept)[:12]:
    print("   ", r)
print("    ……")
