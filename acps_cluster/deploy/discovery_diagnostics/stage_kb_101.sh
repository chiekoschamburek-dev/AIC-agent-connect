#!/bin/sh
# 把"待启用"的 KB 改动从线上代码里撤出，改为暂存，避免 KB 一重启就因指纹不匹配挂掉。
#
# 背景：
#   已提交 KB 1.0.1 注册（PENDING）。但线上代码已改成"新文案 + version 1.0.1"，
#   而**运行中的进程是在改动前启动的**（version 1.0.0）。
#   一旦 KB 服务重启 → 新代码投影出 1.0.1/365 字 → 指纹不匹配 → 拒绝启动 → KB 离线。
# 做法：
#   · 生成 partner_kb.py / discovery_metadata.py 的"新变更版"副本（待启用）
#   · 把线上版本回退到与 1.0.0 注册一致的状态（version 1.0.0 + 原 209 字文案）
#   · 切换脚本 cutover_kb_101.sh 会再把两者装上（见该脚本第 0 步）
set -u
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python
STAGE=/opt/wit/tmp/kb-101-staged
stamp=$(date +%Y%m%d-%H%M%S)

mkdir -p "$STAGE"
echo "############ 1) 暂存「新变更版」为待启用集"
cp -p "$CL/agents/partner_kb.py" "$STAGE/partner_kb.py.new"
cp -p "$CL/core/discovery_metadata.py" "$STAGE/discovery_metadata.py.new"
echo "  已存到 $STAGE/"
grep -n 'version="1.0.1"' "$STAGE/partner_kb.py.new" | sed 's/^/    partner_kb: /'
$PY - <<'PY'
import sys
sys.path.insert(0, '/opt/wit/tmp/kb-101-staged')
PY

echo
echo "############ 2) 回退线上 partner_kb.py → version 1.0.0"
$PY - "$CL/agents/partner_kb.py" <<'PY'
import re, sys
from pathlib import Path
p = Path(sys.argv[1])
t = p.read_text(encoding='utf-8')
new = re.sub(r'version="1\.0\.1",[^\n]*\n', '        version="1.0.0",\n', t, count=1)
if new == t:
    print("  ⚠ 未找到 version=\"1.0.1\" 行，可能已回退")
else:
    p.write_text(new, encoding='utf-8')
    print("  ✔ partner_kb.py version → 1.0.0")
PY
chown wit:wit "$CL/agents/partner_kb.py"
grep -n 'version="1.0' "$CL/agents/partner_kb.py" | sed 's/^/    /'

echo
echo "############ 3) 回退线上 discovery_metadata.py 的 KB 文案 → 原 209 字"
$PY - "$CL/core/discovery_metadata.py" <<'PY'
import sys
from pathlib import Path
p = Path(sys.argv[1])
t = p.read_text(encoding='utf-8')

# KB agent 门面：把加宽版换回已注册的 209 字版（必须与平台注册版逐字一致）
start = t.index('    "yanshu.kb-partner": AgentFacade(')
end = t.index('    "yanshu.hpc-partner": AgentFacade(')
old_block = t[start:end]
new_block = '''    "yanshu.kb-partner": AgentFacade(
        description=(
            "知识库与文献库查询（KB）：数据不动、计算动——受限查询下推 + 字段级脱敏"
            "（open 目录 / members 摘要 / curator 全文），支持聚合统计与受控写入。"
            "适合：查机构文献库 / 结构化知识检索 / 统计与聚合查询 / 需要按权限分级看数据。"
            "典型问法：帮我查库里的文献和统计数字、按条件检索知识库、"
            "做一次聚合统计、knowledge base query、restricted data access。"
        ),
        tags=("知识库", "文献库", "受限查询", "knowledge-base", "data-query"),
    ),
'''
t = t[:start] + new_block + t[end:]

# kb.query 能力门面：换回已注册版
s2 = t.index('    "kb.query": CapabilityFacade(')
e2 = t.index('    "hpc.submit": CapabilityFacade(')
old_q = '''    "kb.query": CapabilityFacade(
        description=(
            "知识库查询：受限查询下推 + 字段级脱敏（open 目录 / members 摘要 / curator 全文），"
            "支持条件过滤、字段投影与聚合统计，可防整库导出。"
        ),
        tags=("知识库", "文献库", "受限查询", "聚合统计", "knowledge-base", "data-query"),
        examples=(
            "帮我按条件检索知识库里的文献",
            "统计库里某个字段的分布",
            "query the knowledge base with filters",
        ),
    ),
'''
t = t[:s2] + old_q + t[e2:]
p.write_text(t, encoding='utf-8')
print("  ✔ KB agent 门面与 kb.query 门面已回退到已注册版")
PY
chown wit:wit "$CL/core/discovery_metadata.py"
python3 -c "import ast,pathlib; ast.parse(pathlib.Path('$CL/core/discovery_metadata.py').read_text(encoding='utf-8')); print('  语法 OK')" 2>/dev/null \
  || $PY -c "import ast,pathlib; ast.parse(pathlib.Path('$CL/core/discovery_metadata.py').read_text(encoding='utf-8')); print('  语法 OK')"

echo
echo "############ 4) 复核：投影是否已回到已注册版（209 字）"
cd "$CL" || exit 1
sudo -u wit YANSHU_RUN_TRACE=0 $PY - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, '/opt/wit/my-project/acps_cluster')
import discovery_metadata as dm
d = dm.agent_discovery_description('yanshu.kb-partner', 'x')
print(f"  投影 agent 描述长度 = {len(d)}（已注册版 = 209）")
print(f"  含「查文献」: {'查文献' in d}（应为 False，加宽版待启用）")
import partner_kb
print(f"  partner_kb version  = {'1.0.0（已回退）' if '1.0.0' in open('/opt/wit/my-project/acps_cluster/agents/partner_kb.py', encoding='utf-8').read() else '?'}")
PY

echo
echo "############ 5) KB 服务仍在跑（无需重启）"
printf '  wit-yanshu@kb: %s\n' "$(systemctl is-active wit-yanshu@kb.service 2>/dev/null)"
echo
echo "  切换时执行：sh $CL/deploy/discovery_diagnostics/cutover_kb_101.sh"
echo "  （它会先在切换流程里重新装上 $STAGE 里的两个文件）"
