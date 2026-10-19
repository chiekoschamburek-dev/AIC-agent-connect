#!/bin/sh
# 在服务器执行：经笔记本反向隧道实测校园网关（模型清单 / 非流式 / 流式 / tool_calls）
# 用法: sh probe_campus_gateway.sh <key文件> [模型名]
# 不指定模型时，用 /models 返回的第一个模型做能力测试。
set -u
KEYFILE="${1:?用法: sh probe_campus_gateway.sh <key文件> [模型名]}"
MODEL="${2:-}"
PROXY="${CAMPUS_PROXY:-http://127.0.0.1:18888}"
BASE="${CAMPUS_BASE:-https://myai.bupt.edu.cn/llm-gw/v1}"

# BOM 与尾部换行都会让网关报 login fail(1004)，一律剥掉
KEY=$(sed -e '1s/^\xef\xbb\xbf//' -e 's/\r\?\n$//' "$KEYFILE" | tr -d '\n\r')
echo "  base=$BASE  proxy=$PROXY  key长度=${#KEY}"

echo
echo "=== 1) 连通性 + 鉴权（/models）==="
curl -s --proxy "$PROXY" --max-time 30 -o /tmp/_cg_models.json \
  -w '  HTTP=%{http_code} TLS=%{time_appconnect}s 总=%{time_total}s\n' \
  -H "Authorization: Bearer $KEY" "$BASE/models" 2>&1
echo "  --- 可用模型："
head -c 800 /tmp/_cg_models.json | sed 's/^/    /'
echo

if [ -z "$MODEL" ]; then
  MODEL=$(/opt/wit/venv/bin/python - <<'PY' 2>/dev/null
import json
try:
    d = json.load(open("/tmp/_cg_models.json"))
    ids = [m.get("id") for m in (d.get("data") or []) if m.get("id")]
    print(ids[0] if ids else "")
except Exception:
    print("")
PY
)
  [ -n "$MODEL" ] && echo "  自动选用第一个模型: $MODEL"
fi
[ -z "$MODEL" ] && { echo "!! 拿不到模型名，请手动传第二个参数"; exit 1; }

echo
echo "=== 2) 非流式最小补全（model=$MODEL）==="
curl -s --proxy "$PROXY" --max-time 90 -o /tmp/_cg_chat.json \
  -w '  HTTP=%{http_code} 首字节=%{time_starttransfer}s 总=%{time_total}s\n' \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"只回复两个字：收到\"}],\"max_tokens\":16}" \
  "$BASE/chat/completions" 2>&1
head -c 400 /tmp/_cg_chat.json | sed 's/^/    /'
echo

echo
echo "=== 3) 流式补全（伙伴真实用法）==="
curl -s -N --proxy "$PROXY" --max-time 120 -o /tmp/_cg_stream.txt \
  -w '  HTTP=%{http_code} 首字节=%{time_starttransfer}s 总=%{time_total}s\n' \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"数到三\"}],\"stream\":true,\"max_tokens\":64}" \
  "$BASE/chat/completions" 2>&1
echo "  SSE 分片数: $(grep -c '^data:' /tmp/_cg_stream.txt 2>/dev/null || echo 0)"
head -c 240 /tmp/_cg_stream.txt | sed 's/^/    /'
echo

echo
echo "=== 4) tool_calls 支持（PEP/委托链路依赖）==="
curl -s --proxy "$PROXY" --max-time 90 -o /tmp/_cg_tools.json \
  -w '  HTTP=%{http_code} 总=%{time_total}s\n' \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"北京天气怎么样\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"get_weather\",\"description\":\"查天气\",\"parameters\":{\"type\":\"object\",\"properties\":{\"city\":{\"type\":\"string\"}},\"required\":[\"city\"]}}}],\"max_tokens\":128}" \
  "$BASE/chat/completions" 2>&1
grep -q 'tool_calls' /tmp/_cg_tools.json && echo "  ✔ 返回了 tool_calls" || echo "  ✘ 未出现 tool_calls（不能作为 supervisor 的模型）"
head -c 400 /tmp/_cg_tools.json | sed 's/^/    /'
echo

echo
echo "=== 5) 同一请求走直连（应失败，证明只有隧道这条路）==="
curl -s --max-time 8 -o /dev/null -w '  直连 HTTP=%{http_code}\n' \
  -H "Authorization: Bearer $KEY" "$BASE/models" 2>&1 || echo "  直连失败（预期）"
