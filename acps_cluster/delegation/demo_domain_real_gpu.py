"""域层 × 真 GPU 串联：双签章程 → 域内 grant 直连 gpu.run（真实沙盒训练）→ 配额 → 冻结。

与 demo_domain.py 的区别（诚实口径）：demo_domain 的 provider 是 build_agent(None)
的准入测试桩（只验裁决与审计，不执行业务）；本脚本把 provider 换成**真实 gpu 伙伴**——
真实 LLM 转写代码、真实 ws-gpu 工作区（域章程与配额账本落真实路径）、每次域内放行
都对应一次 Docker 沙盒真训练。

场景：
  G1 域内 grant 直连 gpu.run（max_uses=2 第 1 次）→ allow + 沙盒真训练
  G2 同枚 grant 第 2 次 → allow + 沙盒真训练（配额 2/2 用满）
  G3 同枚 grant 第 3 次 → 拒（使用侧配额耗尽，不触 GPU）
  G4 章程 r2 冻结（双签 revision 驱替）→ 新 grant 也被拒（验签前 domain-frozen）

用法（在 my-project 下）：python acps_cluster/delegation/demo_domain_real_gpu.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents  # noqa: E402  导入即加载 .env（真实 LLM 配置）
import partner_gpu  # noqa: E402
from delegation import (  # noqa: E402
    build_manifest,
    issue_grant,
    load_signing_context,
    sign_manifest,
    token_data_item,
    verify_manifest,
    write_manifest,
)
from wit import AcpsConfig, AgentCertificateRequest, AgentProviderProfile, WitRuntime  # noqa: E402
from wit.acps.direct import AipDirectClient  # noqa: E402

DOMAIN = "gpu-repro-demo"
GPU_SKILL = "gpu.run"

GPU_WS = CLUSTER / "ws-gpu"
SUPERVISOR_WS = CLUSTER / "ws-supervisor"
EXP_WS = CLUSTER / "ws-experiment"
TRUST = GPU_WS / "acps" / "trust-bundle.pem"
GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
LEADER_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"

PORT_GPU = 18670  # 避开常驻实例的 8460
OUT = CLUSTER / "demo_run" / "domain-real-gpu"
TASKS_DB = GPU_WS / "acps" / "aip-tasks.sqlite3"
RUNS_DIR = GPU_WS / "runs"

REF_CODE = """\
import json, os, time
import torch
import torch.nn as nn
import torch.nn.functional as F

torch.manual_seed(0)
device = 'cuda'
os.makedirs('output', exist_ok=True)

def make_data(n=512, size=32):
    xs, ys = [], []
    for i in range(n):
        cls = i % 2
        base = torch.zeros(1, size, size)
        if cls == 0:
            base[:, ::4, :] = 1.0
        else:
            base[:, :, ::4] = 1.0
        xs.append(base + 0.3 * torch.randn(1, size, size))
        ys.append(cls)
    X = torch.stack(xs)
    Y = torch.tensor(ys)
    perm = torch.randperm(n)
    return X[perm[:410]], Y[perm[:410]], X[perm[410:]], Y[perm[410:]]

class SmallCNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 8, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.Conv2d(8, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
            nn.AdaptiveAvgPool2d((4, 4)))
        self.head = nn.Linear(16 * 4 * 4, 2)

    def forward(self, x):
        return self.head(self.features(x).flatten(1))

def run_arm(name, schedule, Xtr, Ytr, Xva, Yva, epochs=12):
    model = SmallCNN().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    for ep in range(epochs):
        size = schedule(ep)
        x = Xtr if size == 32 else F.interpolate(Xtr, size=size, mode='nearest')
        model.train()
        opt.zero_grad()
        loss = F.cross_entropy(model(x.to(device)), Ytr.to(device))
        loss.backward()
        opt.step()
    model.eval()
    with torch.no_grad():
        acc = (model(Xva.to(device)).argmax(1).cpu() == Yva).float().mean().item()
    return {'arm': name, 'final_val_acc': round(acc, 4),
            'total_train_s': round(time.time() - t0, 2),
            'per_epoch_s': round((time.time() - t0) / epochs, 3),
            'peak_mem_mb': round(torch.cuda.max_memory_allocated() / 1e6, 1)}

def main():
    DATA = make_data()
    results = [
        run_arm('progressive', lambda ep: 8 if ep < 6 else 32, *DATA),
        run_arm('fixed32', lambda ep: 32, *DATA),
    ]
    summary = {'hypothesis': 'ENTR: 初期低分辨率训练更高效', 'results': results}
    print(json.dumps(summary, ensure_ascii=False))
    with open('output/summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

main()
"""

TASK_TEXT = (
    "把下面的参考实现**原样转写**为 main.py 并调用一次 gpu.run 提交（timeout_seconds=600），"
    "把 stdout 与产物清单如实返回。沙盒断网，代码自合成数据，不要读任何外部文件。\n"
    f"```python\n{REF_CODE}\n```"
)


def _acps_config(port: int) -> AcpsConfig:
    return AcpsConfig.custom(
        registry_url="https://localhost:9001",
        ca_url="https://localhost:9003",
        discovery_url="https://localhost:9005",
        public_endpoint=f"https://localhost:{port}/rpc",
        certificate=AgentCertificateRequest(dns=("localhost",)),
        provider=AgentProviderProfile(name="yanshu", organization="Wit", email="e@e.com", country_code="CN"),
        strict_interop=True,
    )


def _manifest(*, revision: int, status: str = "active") -> dict:
    # 关键：直接调 gpu.run 的域内 consumer 必须是 experiment（不在 provider 白名单里）。
    # leader 若亲自调用，会命中 L1 白名单并在域检查之前放行——白名单是 provider 的固有
    # 权利，不受域治理约束（domain_manifest.py 的文档化语义）。
    return build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
            {"aic": LEADER_AIC, "role": "auditor"},
        ],
        assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL, "allowedMembers": ["consumer"]}],
        issuers=[LEADER_AIC],
        revision=revision,
        status=status,
    )


def _dual_sign(manifest: dict) -> dict:
    manifest = sign_manifest(manifest, load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST))
    manifest = sign_manifest(manifest, load_signing_context(GPU_WS / "acps" / "client", trust_bundle=TRUST))
    return manifest


async def _serve_gpu(audit: Path, *, truncate: bool = False) -> WitRuntime:
    """起真实 gpu 伙伴（真实 LLM + 真实 ws-gpu 工作区：域章程/配额账本落真实路径）。"""
    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = LEADER_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    audit.parent.mkdir(parents=True, exist_ok=True)
    if truncate:
        audit.write_text("", encoding="utf-8")
    # partner_gpu 的 PEP 钩子按模块常量 WORKSPACE（=真实 ws-gpu）解析域目录与配额库，
    # 不设 ACPS_PEP_DOMAINS_DIR / ACPS_PEP_QUOTA_PATH 即走真实路径
    provider = agents.build_provider()
    runtime = WitRuntime(
        partner_gpu.build_gpu_agent(provider), workspace=GPU_WS, acps=_acps_config(PORT_GPU)
    )
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=PORT_GPU, amp=False)
    print(f"[serve:gpu] 端口 {PORT_GPU} → {status.status}（真实工作区 {GPU_WS.name}）", flush=True)
    return runtime


async def _send_and_wait(token: str, *, label: str, max_wait_s: int = 480) -> tuple[str, str]:
    """experiment 以域内 consumer 身份直连 gpu.run：任务文本 + grant 凭证，轮询到终态并取产物。"""
    client = AipDirectClient(
        client_certificate=EXP_WS / "acps" / "client" / "certificate.pem",
        client_private_key=EXP_WS / "acps" / "client" / "private-key.pem",
        trust_bundle=TRUST, local_aic=EXP_AIC, strict_interop=True,
    )
    command = {
        "type": "task-command", "id": f"msg-{uuid.uuid4()}",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "senderRole": "leader", "senderId": EXP_AIC,
        "sessionId": f"session-dg-{uuid.uuid4()}", "taskId": f"task-{uuid.uuid4()}",
        "command": "start", "commandParams": {},
        "dataItems": [
            {"type": "text", "text": TASK_TEXT, "metadata": {"skillId": GPU_SKILL}},
            token_data_item(token, target_aic=GPU_AIC, skill_id=GPU_SKILL),
        ],
    }
    request = {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}", "params": {"command": command}}
    t0 = time.time()
    try:
        resp = await client.call(f"https://localhost:{PORT_GPU}/rpc", request, expected_partner_aic=GPU_AIC)
    finally:
        await client.aclose()
    result = resp.get("result") or {}
    task_id = result.get("taskId") or ""
    state = str((result.get("status") or {}).get("state") or "?")
    if state in {"rejected", "failed", "canceled"} or not task_id:
        return state, json.dumps(resp.get("result") or {}, ensure_ascii=False)[:500]

    terminal = {"completed", "failed", "canceled", "rejected", "awaiting-completion"}
    products_text = ""
    while time.time() - t0 < max_wait_s:
        await asyncio.sleep(5)
        con = sqlite3.connect(TASKS_DB)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT state, products FROM aip_tasks WHERE task_id=?", (task_id,)).fetchone()
        con.close()
        if row is None:
            continue
        state = str(row["state"])
        if state in terminal:
            try:
                for product in json.loads(row["products"] or "[]"):
                    for item in product.get("data_items") or product.get("dataItems") or []:
                        products_text += (item.get("text") or "") + "\n"
            except (ValueError, TypeError):
                pass
            break
        print(f"  [{label}] state={state} elapsed={time.time() - t0:.0f}s", flush=True)
    return state, products_text


def _audit_lines(audit: Path) -> list[str]:
    if not audit.exists():
        return []
    out = []
    for line in audit.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out.append(f"{r.get('decision'):5s} rule={r.get('rule', ''):18s} jti={(r.get('jti') or '-')[:10]}")
    return out


def _newest_run_before(ts: float) -> str | None:
    if not RUNS_DIR.is_dir():
        return None
    runs = [d for d in RUNS_DIR.glob("run-*") if d.is_dir() and d.stat().st_mtime > ts]
    return max(runs, key=lambda d: d.stat().st_mtime).name if runs else None


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    audit = OUT / "audit-gpu.jsonl"
    leader_signing = load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST)
    results: list[dict] = []

    # 章程 r1（active）落到真实 ws-gpu/acps/domains/
    # 先清掉本域的历史章程：同 domainId 取最高 revision，残留的冻结 r2 会压过新 r1
    domains_dir = GPU_WS / "acps" / "domains"
    domains_dir.mkdir(parents=True, exist_ok=True)
    for stale in domains_dir.glob(f"{DOMAIN}.r*.json"):
        stale.unlink()
    r1 = _dual_sign(_manifest(revision=1, status="active"))
    body, signers = verify_manifest(r1, trust_bundle=TRUST)
    print(f"[D1] 双签章程验证: {len(signers)} 个签名方 domain={DOMAIN} revision=1", flush=True)
    write_manifest(r1, domains_dir)

    gpu_rt = await _serve_gpu(audit, truncate=True)
    try:
        # G1：域内 grant 直连 gpu.run（第 1 次使用）——受托 consumer=experiment（非白名单成员）
        grant1 = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=2, ttl_seconds=3600,
        )
        t0 = time.time()
        state, products = await _send_and_wait(grant1.token, label="G1")
        run1 = _newest_run_before(t0)
        results.append({"scenario": "G1 域内 grant→gpu.run 真训练(1/2)", "expect": "allow+训练", "state": state,
                        "sandbox_run": run1, "audit": _audit_lines(audit)[-1:]})
        print(f"[G1] state={state} sandbox={run1}", flush=True)

        # G2：同枚 grant 第 2 次（配额 2/2）
        t0 = time.time()
        state, products2 = await _send_and_wait(grant1.token, label="G2")
        run2 = _newest_run_before(t0)
        results.append({"scenario": "G2 同枚 grant 第 2 次真训练(2/2)", "expect": "allow+训练", "state": state,
                        "sandbox_run": run2, "audit": _audit_lines(audit)[-1:]})
        print(f"[G2] state={state} sandbox={run2}", flush=True)

        # G3：第 3 次 → 使用侧配额拒绝（不触 GPU）
        state, _ = await _send_and_wait(grant1.token, label="G3", max_wait_s=30)
        results.append({"scenario": "G3 配额耗尽后的第 3 次", "expect": "rejected(quota)", "state": state,
                        "sandbox_run": None, "audit": _audit_lines(audit)[-1:]})
        print(f"[G3] state={state}", flush=True)

        # G4：r2 冻结（双签驱替）→ 重启实例加载新章程 → 新 grant 也死（验签前）
        r2 = _dual_sign(_manifest(revision=2, status="frozen"))
        verify_manifest(r2, trust_bundle=TRUST)
        write_manifest(r2, domains_dir)
        await gpu_rt.stop_serving()
        gpu_rt = await _serve_gpu(audit)
        grant2 = issue_grant(
            leader_signing, domain=DOMAIN, domain_revision=2, subject_aic=EXP_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5, ttl_seconds=3600,
        )
        state, _ = await _send_and_wait(grant2.token, label="G4", max_wait_s=30)
        results.append({"scenario": "G4 域冻结后新 grant", "expect": "rejected(domain-frozen, 验签前)",
                        "state": state, "sandbox_run": None, "audit": _audit_lines(audit)[-1:]})
        print(f"[G4] state={state}", flush=True)
    finally:
        try:
            await gpu_rt.stop_serving()
        except Exception:  # noqa: BLE001
            pass

    # 汇总落盘
    (OUT / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 域层 × 真 GPU 串联结果", "", f"- domain: {DOMAIN}（章程落真实 {domains_dir}）", ""]
    for r in results:
        lines.append(f"## {r['scenario']}")
        lines.append(f"- 期望: {r['expect']}")
        lines.append(f"- 实际: {r['state']}")
        lines.append(f"- 沙盒: {r['sandbox_run']}")
        lines.append(f"- 审计: {'; '.join(r['audit'])}")
        lines.append("")
    lines.append("## 完整审计轨迹")
    lines.extend(f"- {x}" for x in _audit_lines(audit))
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[done] 产物在 {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
