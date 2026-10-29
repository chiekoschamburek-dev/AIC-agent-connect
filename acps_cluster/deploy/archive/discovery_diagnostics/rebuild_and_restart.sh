#!/bin/sh
# 重建两个工作区的 enrollment，然后重启验证
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

for w in contradiction frontier; do
  echo "=== 重建 ws-$w"
  $PY "$CL/deploy/discovery_diagnostics/rebuild_enrollment.py" --ws "ws-$w" --template ws-critic --apply 2>&1 | tail -8
  chown -R wit:wit "$CL/ws-$w/acps"
done

echo
echo "=== 重启 ==="
for n in contradiction frontier; do
  systemctl reset-failed "wit-yanshu@$n.service" 2>/dev/null
  systemctl restart "wit-yanshu@$n.service"
done

i=0
while [ $i -lt 14 ]; do
  i=$((i+1)); sleep 30
  c=$(systemctl is-active wit-yanshu@contradiction.service 2>/dev/null)
  f=$(systemctl is-active wit-yanshu@frontier.service 2>/dev/null)
  printf '  +%3ds: contradiction=%s frontier=%s\n' $((i*30)) "${c:-unknown}" "${f:-unknown}"
  if [ "$c" = "active" ] && [ "$f" = "active" ]; then break; fi
done

echo
for n in contradiction frontier; do
  echo "--- $n"
  journalctl -u "wit-yanshu@$n.service" -n 5 --no-pager | tail -5 | sed 's/^/    /'
done
echo
echo "=== 端口 ==="
ss -tlnp 2>/dev/null | grep -E '8449|8450' | sed 's/^/  /'
