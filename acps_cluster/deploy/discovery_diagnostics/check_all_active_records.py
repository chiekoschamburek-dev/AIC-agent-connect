"""核对：平台上每个 agent 名字对应的**活跃**记录（含新 AIC），用于确认还剩谁没注册好。"""

from __future__ import annotations

import json
import ssl
from pathlib import Path

import httpx

CL = Path("/opt/wit/my-project/acps_cluster")
ACPS = CL / "ws-scout" / "acps"
D = "https://wt.ioa.pub/discovery/acps-adp-v2/discover"
ACS = "https://wt.ioa.pub/registry-server/acps-atr-v2/acs/"

WANT = ["研枢-Scout-Partner", "研枢-Supervisor", "研枢-Critic-Partner", "研枢-Writer-Partner",
        "研枢-Synthesis-Partner", "研枢-Frontier-Partner", "研枢-Contradiction-Partner",
        "研枢-Experiment-Partner"]

# 已知的本地（旧）AIC，用于区分新旧
LOCAL_OLD = {
    "研枢-Critic-Partner": "1.2.156.3088.1.BUPT.VTF97R.71P6ML.DDF7EG.13B2",
    "研枢-Writer-Partner": "1.2.156.3088.1.BUPT.VTF97R.WATDMX.AQFZR5.0NQN",
    "研枢-Synthesis-Partner": "1.2.156.3088.1.BUPT.VTF97R.YDWSUJ.RBUQ6T.12KC",
    "研枢-Frontier-Partner": "1.2.156.3088.1.BUPT.VTF97R.ZAJ69L.IOEX2R.082D",
    "研枢-Contradiction-Partner": "1.2.156.3088.1.BUPT.VTF97R.0FQNAG.ZO888M.0FW5",
    "研枢-Experiment-Partner": "1.2.156.3088.1.BUPT.VTF97R.5CJ349.JNZ5VU.0E5E",
}


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(ACPS / "trust-bundle.pem"))
    ctx.load_cert_chain(certfile=str(ACPS / "client" / "certificate.pem"),
                        keyfile=str(ACPS / "client" / "private-key.pem"))
    return ctx


QUERIES = ["文献检索 论文", "科研任务 编排 总控", "论文评审 审稿 引用核验", "论文写作 起草 综述",
           "文献精读 结构化 萃取", "选题 研究前沿 新兴方向", "矛盾 冲突 研究假设",
           "实验执行 GPU 沙箱 验证"]

hits: dict[str, dict] = {}
with httpx.Client(verify=ssl_ctx(), timeout=40.0) as c:
    for q in QUERIES:
        r = c.post(D, json={"type": "explicit", "query": q, "limit": 20})
        if r.status_code != 200:
            continue
        for aic, acs in ((r.json() or {}).get("result") or {}).get("acsMap", {}).items():
            nm = acs.get("name") or ""
            if nm in WANT:
                ex = sum(len(s.get("examples") or []) for s in (acs.get("skills") or []))
                prev = hits.get(nm)
                if not prev or len(acs.get("description") or "") > prev["desc"]:
                    hits[nm] = {"aic": aic, "v": acs.get("version"),
                                "desc": len(acs.get("description") or ""), "ex": ex}

print(f"{'agent':28s} {'索引中的 AIC':30s} {'版本':9s} {'描述':6s} {'examples':9s} 新旧")
print("-" * 100)
for nm in WANT:
    h = hits.get(nm)
    if not h:
        print(f"{nm:28s} {'【索引里没有】':30s}")
        continue
    tag = "新" if LOCAL_OLD.get(nm) != h["aic"] else "旧（本地同一个）"
    print(f"{nm:28s} …{h['aic'][-22:]:28s} {str(h['v']):9s} {h['desc']:6d} {h['ex']:<9d} {tag}")

print()
print("=== 这些 AIC 在公开接口上的 active 状态 ===")
with httpx.Client(timeout=25.0) as c:
    for nm in WANT:
        h = hits.get(nm)
        if not h:
            continue
        r = c.get(ACS + h["aic"])
        st = "HTTP " + str(r.status_code)
        act = r.json().get("active") if r.status_code == 200 else "-"
        print(f"  {nm:28s} {st:9s} active={act}")
