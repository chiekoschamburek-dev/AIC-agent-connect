#!/bin/bash
# 服务器上跑本次改动相关的 unittest 子集（冒烟）。
# 注意：服务器是平台身份，本地测试按 dev AIC/CA 写断言，身份相关用例会假失败——
# 验证以「灰度重启 + 真实 E2E」为准，这里只作导入/逻辑冒烟。
# 用法（在服务器上）：bash run_server_tests.sh
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/wit/my-project}"
PY="${PY:-/opt/wit/venv/bin/python}"

cd "$PROJECT_DIR"
echo "⚠ 服务器单测可能因身份差异假失败；先看是不是 PepConfigEnvTest 这类断言" >&2
export PYTHONDONTWRITEBYTECODE=1
export YANSHU_RUN_TRACE=0

LOG=/tmp/unit-workspace-rollout.log
"$PY" -m unittest \
  acps_cluster.tests.test_delegation \
  acps_cluster.tests.test_resource_dispatch_auth \
  acps_cluster.tests.test_skill_scope \
  acps_cluster.tests.test_supervisor_tools \
  acps_cluster.tests.test_partner_workspace_client \
  acps_cluster.tests.test_partner_workspace_acs \
  > "$LOG" 2>&1
RC=$?
echo "UNITTEST_RC=$RC  (log=$LOG)"
echo "== failure/error list =="
grep -E '^(FAIL|ERROR):' "$LOG" || echo "  (none)"
tail -4 "$LOG"
exit "$RC"
