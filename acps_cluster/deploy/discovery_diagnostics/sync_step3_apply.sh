#!/bin/sh
# 代码同步第 3 步：把暂存区落盘到线上（只覆盖代码；身份/凭据/数据不在包内）
set -e
ST=/opt/wit/sync-staging
LIVE=/opt/wit/my-project
BK=$(cat /tmp/_sync_backup_dir 2>/dev/null || echo /opt/wit/sync-backup-unknown)

echo "=== 1) 备份将被覆盖的文件（17 个）到 $BK/overwritten/ ==="
mkdir -p "$BK/overwritten"
cd "$ST" || exit 1
n=0
for f in $(find . -type f | sed 's|^\./||'); do
  if [ -e "$LIVE/$f" ] && ! cmp -s "$f" "$LIVE/$f"; then
    mkdir -p "$BK/overwritten/$(dirname "$f")"
    cp -p "$LIVE/$f" "$BK/overwritten/$f"
    echo "  备份 $f"
    n=$((n+1))
  fi
done
echo "  共备份 $n 个被覆盖文件"

echo
echo "=== 2) 落盘（rsync 语义：整包覆盖）==="
cd "$ST" || exit 1
find . -type f | sed 's|^\./||' | while read -r f; do
  mkdir -p "$LIVE/$(dirname "$f")"
  cp -f "$f" "$LIVE/$f"
done
echo "  已落盘"

echo
echo "=== 3) 修属主（服务以 wit 运行）==="
chown -R wit:wit "$LIVE/acps_cluster" "$LIVE"/*.py 2>/dev/null || true
echo "  完成"

echo
echo "=== 4) 落盘后核对：关键文件到位 ==="
for f in acps_cluster/core/discovery_metadata.py acps_cluster/core/aip_guard.py acps_cluster/agents/partner_scout.py \
         acps_cluster/delegation/__init__.py acps_cluster/deploy/amp_heartbeat_sender.py; do
  printf '  %-52s %s\n' "$f" "$([ -f "$LIVE/$f" ] && echo 存在 || echo ✘缺失)"
done
echo
echo "=== 5) 线上代码文件数（对比备份前）==="
find "$LIVE/acps_cluster" -maxdepth 1 -name '*.py' | wc -l | sed 's/^/  acps_cluster 顶层 *.py: /'
find "$LIVE/acps_cluster/delegation" -name '*.py' 2>/dev/null | wc -l | sed 's/^/  delegation/*.py: /'
