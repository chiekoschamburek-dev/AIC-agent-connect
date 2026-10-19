#!/bin/sh
# 部署本轮全部修复并重启 8 个服务
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
stamp=$(date +%Y%m%d-%H%M%S)

echo "=== 1) 备份线上关键文件 ==="
mkdir -p "$CL/.pre-review-fixes-$stamp"
for f in supervisor.py partner_contradiction.py partner_critic.py partner_frontier.py \
         partner_synthesis.py partner_writer.py partner_scout.py eval_runner.py \
         delegation/__init__.py delegation/pep.py \
         deploy/amp_heartbeat_sender.py deploy/amp_heartbeat_mirror.py; do
  [ -f "$CL/$f" ] && mkdir -p "$CL/.pre-review-fixes-$stamp/$(dirname "$f")" \
    && cp -p "$CL/$f" "$CL/.pre-review-fixes-$stamp/$f"
done
echo "  已备份到 .pre-review-fixes-$stamp/"

echo
echo "=== 2) 校验部署源文件（语法 + 关键标记）==="
cd "$CL" || exit 1
for f in supervisor.py delegation/skill_scope.py delegation/pep.py \
         deploy/amp_heartbeat_sender.py deploy/amp_heartbeat_mirror.py eval_runner.py; do
  sudo -u wit "$PY" -c "import ast,pathlib,sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))" "$f" \
    && printf '  %-42s 语法 ✔\n' "$f" || printf '  %-42s 语法 ✘\n' "$f"
done
echo "  --- 关键标记"
grep -c '_WAIT_ROUNDS' supervisor.py | sed 's/^/    supervisor._WAIT_ROUNDS(模块级): /'
grep -c 'wrap_run' supervisor.py | sed 's/^/    supervisor.wrap_run: /'
grep -c 'meta.get(timeout=args.send_timeout)' deploy/amp_heartbeat_sender.py | sed 's/^/    sender 确认投递: /'
grep -c 'except OSError as exc' deploy/amp_heartbeat_mirror.py | sed 's/^/    mirror 每文件容错: /'
grep -c 'skill_scope' delegation/__init__.py | sed 's/^/    pep_hooks 装配收窄: /'
grep -c 'except BaseException' eval_runner.py | sed 's/^/    eval_runner 杀容器: /'

echo
echo "=== 3) 单测 ==="
sudo -u wit "$PY" -m unittest discover -s tests -p 'test_*.py' 2>&1 | grep -E '^(OK|FAILED|Ran )' | sed 's/^/  /'

echo
echo "=== 4) 重启 8 个服务 ==="
for n in scout supervisor critic writer synthesis experiment contradiction frontier; do
  systemctl reset-failed "wit-yanshu@$n.service" 2>/dev/null
  systemctl restart "wit-yanshu@$n.service"
done
i=0
while [ $i -lt 22 ]; do
  i=$((i+1)); sleep 30
  line=""; allok=1
  for n in scout supervisor critic writer synthesis experiment contradiction frontier; do
    s=$(systemctl is-active "wit-yanshu@$n.service" 2>/dev/null)
    if [ "$s" = "active" ]; then line="$line $n✔"; else line="$line $n:$s"; allok=0; fi
  done
  printf '  +%3ds:%s\n' $((i*30)) "$line"
  [ $allok -eq 1 ] && break
done

echo
echo "=== 5) 端口 ==="
ss -tlnp 2>/dev/null | grep -E '844[4-9]|8450|8461' | awk '{print "  "$4}' | sort

echo
echo "=== 6) 审计路径是否已按工作区分开（重启后应有新文件，且不再集中）==="
for w in scout supervisor critic writer synthesis experiment contradiction frontier; do
  f="$CL/ws-$w/logs/pep-audit.jsonl"
  printf '  ws-%-14s %s\n' "$w" "$([ -f "$f" ] && echo "有（$(wc -l < "$f") 行）" || echo '（尚未生成，等首次入站）')"
done
