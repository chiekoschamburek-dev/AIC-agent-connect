"""提交前自检：新 ACS 的 skills 集合/内容必须与代码投影逐字段一致。

用法：python acps_cluster/deploy/check_resubmit_acs.py [key]   # 默认 scout
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # acps_cluster/deploy
CLUSTER = HERE.parent                            # acps_cluster
PROJECT = CLUSTER.parent                         # my-project
sys.path.insert(0, str(CLUSTER))
sys.path.insert(0, str(PROJECT))

KEY = sys.argv[1] if len(sys.argv) > 1 else "scout"

import make_acs  # noqa: E402

module_name, builder_name = make_acs.AGENTS[KEY]
module = __import__(module_name)

# 关键：必须比对**构建后的 agent**（门面在 build_*_agent 里套用），
# 而不是模块级能力常量——后者是未增强的声明。
agent = getattr(module, builder_name)(None)

acs = json.loads((CLUSTER / f"acs.{KEY}.resubmit.json").read_text(encoding="utf-8"))

code = {}
for c in agent.capabilities:
    skill = c.capability.to_acps_skill()
    if skill is not None:
        code[skill["id"]] = skill
acs_map = {s["id"]: s for s in acs["skills"]}

print("代码 acps_skill 集合:", sorted(code))
print("ACS  skills 集合 :", sorted(acs_map))
print("集合一致:", set(code) == set(acs_map))
ok = True
for sid, skill in code.items():
    got = acs_map.get(sid)
    if got is None:
        print(f"  [缺] {sid}")
        ok = False
        continue
    for key in ("description", "tags", "examples", "name"):
        if key == "version":
            continue
        mine, theirs = skill.get(key), got.get(key)
        if key == "name":
            continue  # ACS 的 name 由能力装饰器/acps_skill 覆盖，允许差异
        if mine != theirs:
            print(f"  [不一致] {sid}.{key}:\n     代码={mine}\n     ACS ={theirs}")
            ok = False
print("逐字段一致:", ok)
print()
print("顶层键:", sorted(acs.keys()))
print("aic:", repr(acs["aic"]), "| active:", acs["active"], "| version:", acs["version"])
print("description 长度:", len(acs["description"]))
print("endPoints:", json.dumps(acs["endPoints"], ensure_ascii=False))
