#!/usr/bin/env bash
# 研枢（yanshu-AIC）一键部署入口。
#
# 设计边界：
#   - 能自动化的：环境检查、venv/依赖、配置模板、前端构建、测试、systemd 单元渲染安装、启停/状态；
#   - 不能自动化的：平台账号注册、CA 签发、证书/身份（ws-*/acps）、真实 .env 值。
#     这些必须人工准备；preflight 会逐项检查并给出可执行提示。
#
# 用法：
#   ./deploy.sh preflight                 # 只检查，不改任何东西
#   ./deploy.sh init [--with-guard-api]   # 建 .venv、装依赖、生成 .env.production
#   ./deploy.sh build [--skip-frontend] [--with-tests]
#   sudo ./deploy.sh install-units        # 渲染 __ACPS_ROOT__/__ACPS_PY__ 后安装 systemd 单元
#   sudo ./deploy.sh start [--with-guard-api] [--partners "scout writer ..."]
#   sudo ./deploy.sh stop  [--with-guard-api]
#   ./deploy.sh status | logs [unit]
#
# 可用环境变量：
#   ACPS_ROOT   仓库根（默认本脚本所在目录）
#   ACPS_PY     解释器（默认 $ACPS_ROOT/.venv/bin/python，不存在则 python3）
#   ACPS_PYTHON 建 venv 用的解释器（默认 python3.12 / python3）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACPS_ROOT="${ACPS_ROOT:-$ROOT}"
PY="${ACPS_PY:-$ACPS_ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python || true)"
ENV_FILE="$ACPS_ROOT/acps_cluster/deploy/.env.production"
STAGE="$ACPS_ROOT/.runtime/systemd"

DEFAULT_PARTNERS="scout synthesis writer critic contradiction frontier experiment citation gpu cpu kb"
DEFAULT_UNITS="wit-yanshu@supervisor wit-workspace wit-amp-mirror wit-amp-sender wit-task-feeder wit-task-prune.timer wit-ledger-report.timer"

say()  { printf '\n=== %s ===\n' "$*"; }
warn() { printf '!! %s\n' "$*" >&2; }
die()  { printf '!! %s\n' "$*" >&2; exit 1; }

require_root() {
  [ "$(id -u)" -eq 0 ] || die "该子命令需要 root（要写 /etc/systemd/system 或操作服务）：sudo $0 $*"
}

preflight() {
  say "环境检查"
  local ok=1
  if [ -n "${PY:-}" ] && "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    echo "python: $($PY -V 2>&1) ($PY)"
  else
    warn "未找到 Python >= 3.12（ACPS_PY / .venv / python3）"; ok=0
  fi

  if command -v node >/dev/null 2>&1; then
    echo "node:   $(node --version)（前端构建用）"
  else
    warn "未找到 node（前端构建会跳过；可先装 Node >= 20）"
  fi

  if ls "$ACPS_ROOT"/vendor/wit_framework-*.whl >/dev/null 2>&1; then
    echo "wit_framework wheel: 已就位"
  else
    warn "vendor/ 缺少 wit_framework wheel（上游未授予再分发许可，请从官方渠道获取后放入 vendor/）"; ok=0
  fi
  if ls "$ACPS_ROOT"/vendor/acps_sdk-*.whl >/dev/null 2>&1; then
    echo "acps_sdk wheel: 已就位"
  else
    warn "vendor/ 缺少 acps_sdk wheel"; ok=0
  fi

  if [ -f "$ENV_FILE" ]; then
    echo "配置文件: $ENV_FILE"
    if ! grep -q "^ACPS_PROVIDER_NAME=." "$ENV_FILE" 2>/dev/null; then
      warn "配置缺少 ACPS_PROVIDER_NAME=（平台已批 ACS 的 provider.name）"
      ok=0
    fi
  else
    warn "缺少 $ENV_FILE；先运行 ./deploy.sh init，再按注释填写真实值"
  fi

  if command -v systemctl >/dev/null 2>&1; then
    echo "systemd: 可用"
  else
    warn "未找到 systemctl（本机可跑测试/构建，但 install-units/start 只能在 Linux systemd 主机执行）"
  fi

  if [ "$ok" -eq 1 ]; then
    echo; echo "preflight: 通过（平台身份/证书仍需人工准备，见 docs/deployment.md）"
  else
    echo; warn "preflight: 有阻塞项，按上面提示处理后重跑"
    return 1
  fi
}

init() {
  local with_guard=0
  for arg in "$@"; do
    if [ "$arg" = "--with-guard-api" ]; then with_guard=1; fi
  done

  say "创建虚拟环境"
  if [ ! -x "$ACPS_ROOT/.venv/bin/python" ]; then
    local py="${ACPS_PYTHON:-$(command -v python3.12 || command -v python3 || true)}"
    [ -n "$py" ] || die "未找到 python3.12+；可设置 ACPS_PYTHON=/path/to/python3.12"
    "$py" -m venv "$ACPS_ROOT/.venv"
  fi
  PY="$ACPS_ROOT/.venv/bin/python"
  "$PY" -m pip install --upgrade pip >/dev/null

  say "安装 Python 依赖"
  ( cd "$ACPS_ROOT" && "$PY" -m pip install -r requirements.txt )
  if [ "$with_guard" -eq 1 ]; then
    ( cd "$ACPS_ROOT" && "$PY" -m pip install -r guard-api/requirements.txt )
  fi

  say "生成配置模板"
  if [ -f "$ENV_FILE" ]; then
    echo "已存在，保留不动：$ENV_FILE"
  else
    mkdir -p "$(dirname "$ENV_FILE")"
    cp "$ACPS_ROOT/.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "已生成：$ENV_FILE（请填写真实值；勿提交）"
  fi
  if [ -e "$ACPS_ROOT/.env" ]; then
    echo "根 .env 已存在，保留不动"
  else
    ln -s "acps_cluster/deploy/.env.production" "$ACPS_ROOT/.env"
    echo "已建立根 .env -> acps_cluster/deploy/.env.production（bootstrap 读取）"
  fi
  mkdir -p "$ACPS_ROOT/acps_cluster/ws-supervisor/logs"

  echo
  echo "下一步：编辑 $ENV_FILE，然后 ./deploy.sh build && sudo ./deploy.sh install-units && sudo ./deploy.sh start"
}

build() {
  local skip_frontend=0 with_tests=0
  for arg in "$@"; do
    if [ "$arg" = "--skip-frontend" ]; then skip_frontend=1; fi
    if [ "$arg" = "--with-tests" ]; then with_tests=1; fi
  done

  say "语法编译"
  [ -n "${PY:-}" ] || die "未找到 Python"
  "$PY" -m compileall -q "$ACPS_ROOT/acps_cluster" "$ACPS_ROOT/capabilities" "$ACPS_ROOT/guard-api"

  if [ "$skip_frontend" -eq 0 ] && command -v npm >/dev/null 2>&1; then
    say "构建前端 cluster-viz"
    ( cd "$ACPS_ROOT/cluster-viz" && npm ci && npm run build )
  else
    warn "跳过前端构建（无 npm 或 --skip-frontend）"
  fi

  if [ "$with_tests" -eq 1 ]; then
    say "运行测试"
    ( cd "$ACPS_ROOT" && "$PY" run_all_tests.py )
  fi
  echo; echo "build: 完成"
}

render_units() {
  [ -d "$ACPS_ROOT/acps_cluster/deploy" ] || die "找不到 deploy 目录"
  mkdir -p "$STAGE"
  local count=0
  for tpl in "$ACPS_ROOT"/acps_cluster/deploy/*.service; do
    local name; name="$(basename "$tpl")"
    if [ "$name" = "gjw-designer.service" ]; then continue; fi   # 队友节点，参数不归本仓库
    sed -e "s#__ACPS_ROOT__#${ACPS_ROOT}#g" -e "s#__ACPS_PY__#${PY}#g" "$tpl" > "$STAGE/$name"
    count=$((count + 1))
  done
  echo "已渲染 $count 个单元到 $STAGE"
}

install_units() {
  require_root install-units
  render_units
  say "安装 systemd 单元"
  install -m 0644 "$STAGE"/*.service /etc/systemd/system/
  systemctl daemon-reload
  echo "已安装：$(cd "$STAGE" && ls *.service | tr '\n' ' ')"
  echo "启用/启动：sudo $0 start"
}

units_for_start() {
  local partners="$1" with_guard="$2"
  local units="$DEFAULT_UNITS"
  for key in $partners; do
    if [ "$key" = "workspace" ]; then continue; fi
    units="$units wit-yanshu@$key"
  done
  if [ "$with_guard" -eq 1 ]; then units="$units wit-guard-api"; fi
  printf '%s\n' $units
}

start() {
  require_root start
  [ -f "$ENV_FILE" ] || die "缺少 $ENV_FILE；先 ./deploy.sh init 并填写真实值"
  local partners="${ACPS_PARTNERS:-$DEFAULT_PARTNERS}" with_guard=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --with-guard-api) with_guard=1 ;;
      --partners) shift; partners="${1:-${ACPS_PARTNERS:-$DEFAULT_PARTNERS}}" ;;
      *) die "未知参数: $1" ;;
    esac
    shift
  done
  for unit in $(units_for_start "$partners" "$with_guard"); do
    echo ">> enable --now $unit"
    systemctl enable --now "$unit"
  done
  echo; echo "start: 完成；用 ./deploy.sh status 查看"
}

stop() {
  require_root stop
  local with_guard=0
  for arg in "$@"; do
    if [ "$arg" = "--with-guard-api" ]; then with_guard=1; fi
  done
  # 反向停：先停伙伴，再停 supervisor
  for unit in $(units_for_start "${ACPS_PARTNERS:-$DEFAULT_PARTNERS}" "$with_guard" | tr ' ' '\n' | tac); do
    systemctl stop "$unit" 2>/dev/null || true
  done
  echo "stop: 完成（未 disable；需要开机不启动请手工 systemctl disable）"
}

status() {
  local units="$DEFAULT_UNITS wit-yanshu@supervisor wit-guard-api"
  for unit in $units; do
    printf '\n--- %s ---\n' "$unit"
    systemctl --no-pager --lines=0 status "$unit" 2>&1 | head -n 6 || true
  done
}

logs() {
  local unit="${1:-wit-yanshu@supervisor}"
  journalctl -u "$unit" -n 200 --no-pager
}

case "${1:-help}" in
  preflight) preflight ;;
  init) shift; init "$@" ;;
  build) shift; build "$@" ;;
  install-units) install_units ;;
  start) shift; start "$@" ;;
  stop) shift; stop "$@" ;;
  status) status ;;
  logs) shift; logs "$@" ;;
  help|-h|--help) sed -n '2,30p' "$0" ;;
  *) die "未知子命令: ${1:-}（可用：preflight init build install-units start stop status logs）" ;;
esac
