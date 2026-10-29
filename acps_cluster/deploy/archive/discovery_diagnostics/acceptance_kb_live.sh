#!/bin/sh
# KB 上线后的完整准入验收：
#   (1) 展示能力（kb.schema）公开可调
#   (2) 干活能力（kb.query/kb.ingest）必须持令牌（外部与 leader 都被拒）
#   (3) leader 携真实令牌 → 放行；且章程生效时域路径也可用
CL=/opt/wit/my-project/acps_cluster
PY=/opt/wit/venv/bin/python

echo "=== 用 KB 的**真实**配置跑准入矩阵 ==="
cd "$CL" || exit 1
sudo -u wit YANSHU_RUN_TRACE=0 $PY - <<'PY'
import os, sys, time
from pathlib import Path
from types import SimpleNamespace

CL = Path('/opt/wit/my-project/acps_cluster')
# 按 systemd 顺序加载 env（共享 → per-agent），否则 PEP 口径不对
for p in (CL / 'deploy' / '.env.production', Path('/etc/wit-kb-pep.env')):
    if p.is_file():
        for ln in p.read_text(encoding='utf-8').splitlines():
            s = ln.strip()
            if s and not s.startswith('#') and '=' in s:
                k, v = s.split('=', 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

sys.path.insert(0, '/opt/wit/my-project'); sys.path.insert(0, str(CL))
import partner_kb
from delegation.pep import capability_visibility, evaluate, load_pep_config
from delegation.token import issue_token, load_signing_context, token_data_item

KB = '1.2.156.3088.1.BUPT.VTF97R.GIKJNY.9AL6AY.10UD'
LEADER = '1.2.156.3088.1.BUPT.VTF97R.JXN8O8.98DOO8.0EB6'
OUTSIDER = '1.2.156.3088.1.0001.00001.EZTK03.JISTB6.0P3V'

public, has_non = capability_visibility(partner_kb.KB_CAPABILITIES)
cfg = load_pep_config(CL / 'ws-kb', public_skills=public, has_non_public_skills=has_non)
print(f"  本机 AIC      = {cfg.my_aic}")
print(f"  公开能力      = {sorted(public)}")
print(f"  含非公开      = {has_non}")
print(f"  leader_direct = {cfg.leader_direct_open}   leader_aics={len(cfg.leader_aics)}")
print(f"  域章程        = {sorted(cfg.domains)}")
print()

def req(caller, skill, token=None):
    items = [{"type": "text", "text": "查询知识库"}]
    if token:
        items.append(token_data_item(token, target_aic=KB, skill_id=skill))
    return SimpleNamespace(leader_aic=caller, command_params={"skillId": skill},
                           data_items=items, peer_verified=True,
                           task_id="t", session_id="s", delivery="rpc")

cases = []
# (1) 展示能力
cases.append(("外部 指名展示能力 kb.schema（无令牌）", req(OUTSIDER, "kb.schema"), True))
# (2) 干活能力：外部与 leader 无令牌都应拒
cases.append(("外部 指名 kb.query（无令牌）", req(OUTSIDER, "kb.query"), False))
cases.append(("leader 指名 kb.query（无令牌）", req(LEADER, "kb.query"), False))
cases.append(("leader 指名 kb.ingest（无令牌）", req(LEADER, "kb.ingest"), False))
# (3) leader 携真实令牌 → 放行
sc = load_signing_context(CL / 'ws-kb' / 'acps' / 'client',
                          trust_bundle=CL / 'ws-kb' / 'acps' / 'trust-bundle.pem')
# 注意：令牌签发者必须是"合法 leader"→ 用 supervisor 的工作区签
sc_leader = load_signing_context(CL / 'ws-supervisor' / 'acps' / 'client',
                                 trust_bundle=CL / 'ws-kb' / 'acps' / 'trust-bundle.pem')
tok = issue_token(sc_leader, delegate_aic=LEADER, target_aic=KB, skill_id="kb.query",
                  ttl_seconds=300, include_intermediates=True).token
cases.append(("leader 指名 kb.query（携真实令牌）", req(LEADER, "kb.query", tok), True))

ok = True
for label, r, expect_allow in cases:
    out = evaluate(r, cfg)
    good = out.allowed == expect_allow
    ok = ok and good
    print(f"  {'✔' if good else '✘'} {label:42s} → {out.decision:6s} / {out.rule}")
print()
print("  ✔ KB 准入矩阵符合预期" if ok else "  ✘ 有偏差，需排查")
PY

echo
echo "=== 服务总览 ==="
for n in scout supervisor critic writer synthesis experiment contradiction frontier kb; do
  printf '  %-14s %s\n' "$n" "$(systemctl is-active wit-yanshu@$n.service 2>/dev/null)"
done
