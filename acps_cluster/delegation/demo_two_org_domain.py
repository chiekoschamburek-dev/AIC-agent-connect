"""两机构域合作演示：本地"算力提供方"机构 × 研枢集群机构，双边签署算力域。

机构划分（行政边界，两套 leader、两套工作区；身份当前同源 dev CA——诚实口径见报告）：
- 机构 A（算力提供方，本地新建）：edge-leader（管理 leader：双签章程、签发域 grant）
  + 两块异构资源 gpu-partner（已有）/ cpu-partner（新建）
- 机构 B（消费方，研枢集群）：supervisor（B 侧 leader，双签章程）
  + experiment（域内 consumer，直接调用资源的 agent）

场景（域闸门 × 真实执行；配额按 provider 各自记账——同枚 grant 在 gpu/cpu 各有 2 次）：
  S1 experiment 持 grant → gpu.run   → allow + 真实沙盒训练
  S2 experiment 持 grant → cpu.run   → allow + 真实沙盒执行（纯 CPU 任务）
  S3 experiment 持 grant → gpu.run   → allow（gpu 侧配额 2/2 用满）
  S4 experiment 第 3 次调 gpu.run     → 拒（quota-exhausted）
  S5 章程 r2 冻结（双签驱替）→ 新 grant 也被拒（domain-frozen，验签前）

用法（在 my-project 下）：python acps_cluster/delegation/demo_two_org_domain.py
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

import agents  # noqa: E402  导入即加载 .env
import partner_cpu  # noqa: E402
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

DOMAIN = "yanshu-edge-demo"
GPU_SKILL = "gpu.run"
CPU_SKILL = "cpu.run"

GPU_WS = CLUSTER / "ws-gpu"
CPU_WS = CLUSTER / "ws-cpu"
EXP_WS = CLUSTER / "ws-experiment"
SUPERVISOR_WS = CLUSTER / "ws-supervisor"
EDGE_WS = CLUSTER / "ws-edge-leader"
TRUST = GPU_WS / "acps" / "trust-bundle.pem"

GPU_AIC = "1.2.156.3088.1.1.GP5060.SANDBOX1.RUN01.0IGR"
CPU_AIC = "1.2.156.3088.1.1.CPUNOD.CPUNODE1.RUN01.0905"
EXP_AIC = "1.2.156.3088.1.1.EXPRMT.LABEXE01.RUN01.1207"
SUPERVISOR_AIC = "1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672"
EDGE_AIC = "1.2.156.3088.1.1.EDGE01.MGTLEAD1.RUN01.0QGJ"

PORT_GPU = 18670
PORT_CPU = 18672
OUT = CLUSTER / "demo_run" / "two-org-domain"

GPU_REF_CODE = """\
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

model = SmallCNN().to(device)
opt = torch.optim.Adam(model.parameters(), lr=1e-3)
X, Y, Xv, Yv = make_data()
for ep in range(12):
    model.train()
    opt.zero_grad()
    loss = F.cross_entropy(model(X.to(device)), Y.to(device))
    loss.backward()
    opt.step()
model.eval()
with torch.no_grad():
    acc = (model(Xv.to(device)).argmax(1).cpu() == Yv).float().mean().item()
summary = {'task': 'gpu-train-small-cnn', 'epochs': 12, 'final_val_acc': round(acc, 4),
           'peak_mem_mb': round(torch.cuda.max_memory_allocated() / 1e6, 1)}
print(json.dumps(summary, ensure_ascii=False))
with open('output/summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
"""

CPU_REF_CODE = """\
import json, os, time

os.makedirs('output', exist_ok=True)

def sieve(n):
    is_p = [True] * (n + 1)
    is_p[0] = is_p[1] = False
    for i in range(2, int(n ** 0.5) + 1):
        if is_p[i]:
            for j in range(i * i, n + 1, i):
                is_p[j] = False
    return sum(is_p)

t0 = time.time()
primes = sieve(1_000_000)
t1 = time.time()
a = [[i * 1.0 for i in range(150)] for _ in range(150)]
b = [[(j + 1) * 1.0 for j in range(150)] for _ in range(150)]
c = [[sum(a[i][k] * b[k][j] for k in range(150)) for j in range(150)] for i in range(150)]
t2 = time.time()
summary = {'task': 'cpu-benchmark', 'primes_below_1m': primes,
           'sieve_s': round(t1 - t0, 3), 'matmul_150_pure_py_s': round(t2 - t1, 3),
           'checksum': round(c[0][0] + c[-1][-1], 2)}
print(json.dumps(summary, ensure_ascii=False))
with open('output/summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)
"""


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


def _manifest(*, revision: int, status: str = "active", previous: dict | None = None) -> dict:
    return build_manifest(
        domain_id=DOMAIN,
        members=[
            {"aic": GPU_AIC, "role": "provider"},
            {"aic": CPU_AIC, "role": "provider"},
            {"aic": EXP_AIC, "role": "consumer"},
            {"aic": EDGE_AIC, "role": "auditor"},
            {"aic": SUPERVISOR_AIC, "role": "auditor"},
        ],
        assets=[
            {"providerAic": GPU_AIC, "skill": GPU_SKILL, "allowedMembers": ["consumer"]},
            {"providerAic": CPU_AIC, "skill": CPU_SKILL, "allowedMembers": ["consumer"]},
        ],
        issuers=[EDGE_AIC],
        revision=revision,
        status=status,
        previous=previous,
    )


def _dual_sign(manifest: dict) -> dict:
    """双边签署：机构 A 的管理 leader + 机构 B 的研枢 supervisor。"""
    manifest = sign_manifest(manifest, load_signing_context(EDGE_WS / "acps" / "client", trust_bundle=TRUST))
    manifest = sign_manifest(manifest, load_signing_context(SUPERVISOR_WS / "acps" / "client", trust_bundle=TRUST))
    return manifest


async def _serve_provider(build_agent, ws: Path, port: int, audit: Path) -> WitRuntime:
    os.environ["ACPS_PEP_ENABLED"] = "1"
    os.environ["ACPS_PEP_LEADER_AICS"] = EDGE_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    audit.parent.mkdir(parents=True, exist_ok=True)
    provider = agents.build_provider()
    runtime = WitRuntime(build_agent(provider), workspace=ws, acps=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[serve] {ws.name} 端口 {port} → {status.status}", flush=True)
    return runtime


def _write_charter(manifest: dict) -> None:
    """章程落到两个 provider 的真实域目录（同 domainId 取最高 revision；先清本域旧版）。"""
    for ws in (GPU_WS, CPU_WS):
        domains_dir = ws / "acps" / "domains"
        domains_dir.mkdir(parents=True, exist_ok=True)
        for stale in domains_dir.glob(f"{DOMAIN}.r*.json"):
            stale.unlink()
        write_manifest(manifest, domains_dir)


def _newest_run(runs_root: Path, after_ts: float) -> str | None:
    if not runs_root.is_dir():
        return None
    runs = [d for d in runs_root.glob("run-*") if d.is_dir() and d.stat().st_mtime > after_ts]
    return max(runs, key=lambda d: d.stat().st_mtime).name if runs else None


async def _send_and_wait(caller_aic: str, port: int, partner_aic: str, tasks_db: Path,
                         runs_root: Path, *, skill: str, text: str, token: str,
                         label: str, max_wait_s: int = 480) -> tuple[str, str, str | None]:
    client = AipDirectClient(
        client_certificate=EXP_WS / "acps" / "client" / "certificate.pem",
        client_private_key=EXP_WS / "acps" / "client" / "private-key.pem",
        trust_bundle=TRUST, local_aic=caller_aic, strict_interop=True,
    )
    command = {
        "type": "task-command", "id": f"msg-{uuid.uuid4()}",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "senderRole": "leader", "senderId": caller_aic,
        "sessionId": f"session-2org-{uuid.uuid4()}", "taskId": f"task-{uuid.uuid4()}",
        "command": "start", "commandParams": {},
        "dataItems": [
            {"type": "text", "text": text, "metadata": {"skillId": skill}},
            token_data_item(token, target_aic=partner_aic, skill_id=skill),
        ],
    }
    request = {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}", "params": {"command": command}}
    t0 = time.time()
    try:
        resp = await client.call(f"https://localhost:{port}/rpc", request, expected_partner_aic=partner_aic)
    finally:
        await client.aclose()
    result = resp.get("result") or {}
    task_id = result.get("taskId") or ""
    state = str((result.get("status") or {}).get("state") or "?")
    if state in {"rejected", "failed", "canceled"} or not task_id:
        return state, "", None

    terminal = {"completed", "failed", "canceled", "rejected", "awaiting-completion"}
    products_text = ""
    while time.time() - t0 < max_wait_s:
        await asyncio.sleep(5)
        con = sqlite3.connect(tasks_db)
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
    return state, products_text, _newest_run(runs_root, t0)


def _audit_rules(audit: Path) -> list[str]:
    if not audit.exists():
        return []
    return [
        f"{r.get('decision'):5s} {r.get('rule', '')} {((r.get('reason') or '')[:60])}"
        for r in (json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip())
    ]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    gpu_audit = OUT / "audit-gpu.jsonl"
    cpu_audit = OUT / "audit-cpu.jsonl"
    edge_signing = load_signing_context(EDGE_WS / "acps" / "client", trust_bundle=TRUST)
    results: list[dict] = []

    # D1：双边签署章程 r1，落到两个 provider 的真实域目录
    r1 = _dual_sign(_manifest(revision=1, status="active"))
    _, signers = verify_manifest(r1, trust_bundle=TRUST)
    print(f"[D1] 双签章程: {len(signers)} 个签名方（A 侧 edge-leader + B 侧 supervisor）", flush=True)
    _write_charter(r1)

    gpu_rt = await _serve_provider(partner_gpu.build_gpu_agent, GPU_WS, PORT_GPU, gpu_audit)
    cpu_rt = await _serve_provider(partner_cpu.build_cpu_agent, CPU_WS, PORT_CPU, cpu_audit)
    try:
        grant = issue_grant(
            edge_signing, domain=DOMAIN, domain_revision=1, subject_aic=EXP_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL},
                    {"providerAic": CPU_AIC, "skill": CPU_SKILL}],
            max_uses=2, ttl_seconds=3600,
        )
        print(f"[grant] 已签发 jti={grant.jti[:10]}…（gpu/cpu 两 provider 各 2 次配额）", flush=True)

        # S1：gpu.run 真训练
        t0 = time.time()
        state, products, run_id = await _send_and_wait(
            EXP_AIC, PORT_GPU, GPU_AIC, GPU_WS / "acps" / "aip-tasks.sqlite3", GPU_WS / "runs",
            skill=GPU_SKILL, text=f"把参考实现原样转写为 main.py 并调用一次 gpu.run（timeout_seconds=600），"
                                  f"如实返回 stdout 与产物。沙盒断网。\n```python\n{GPU_REF_CODE}\n```",
            token=grant.token, label="S1")
        results.append({"s": "S1 域内 grant→gpu.run 真训练", "expect": "allow+训练", "state": state,
                        "run": run_id, "audit": _audit_rules(gpu_audit)[-1:]})
        print(f"[S1] state={state} run={run_id}", flush=True)

        # S2：cpu.run 真执行（同一枚 grant 的第二类资产）
        state, products_cpu, run_id = await _send_and_wait(
            EXP_AIC, PORT_CPU, CPU_AIC, CPU_WS / "acps" / "aip-tasks.sqlite3", CPU_WS / "runs",
            skill=CPU_SKILL, text=f"把参考实现原样转写为 main.py 并调用一次 cpu.run（timeout_seconds=600，entrypoint 不要指定、用缺省），"
                                  f"如实返回 stdout 与产物。沙盒断网。\n```python\n{CPU_REF_CODE}\n```",
            token=grant.token, label="S2")
        results.append({"s": "S2 同枚 grant→cpu.run 真执行", "expect": "allow+执行", "state": state,
                        "run": run_id, "audit": _audit_rules(cpu_audit)[-1:]})
        print(f"[S2] state={state} run={run_id}", flush=True)

        # S3：gpu 第 2 次（gpu 侧配额 2/2 用满）
        state, _, run_id = await _send_and_wait(
            EXP_AIC, PORT_GPU, GPU_AIC, GPU_WS / "acps" / "aip-tasks.sqlite3", GPU_WS / "runs",
            skill=GPU_SKILL, text=f"把参考实现原样转写为 main.py 并调用一次 gpu.run（timeout_seconds=600）。\n"
                                  f"```python\n{GPU_REF_CODE}\n```",
            token=grant.token, label="S3")
        results.append({"s": "S3 gpu 第 2 次（配额 2/2）", "expect": "allow+训练", "state": state,
                        "run": run_id, "audit": _audit_rules(gpu_audit)[-1:]})
        print(f"[S3] state={state} run={run_id}", flush=True)

        # S4：gpu 第 3 次 → 配额拒绝
        state, _, _ = await _send_and_wait(
            EXP_AIC, PORT_GPU, GPU_AIC, GPU_WS / "acps" / "aip-tasks.sqlite3", GPU_WS / "runs",
            skill=GPU_SKILL, text="调用一次 gpu.run 跑一个 hello world（这次会被配额拒绝）",
            token=grant.token, label="S4", max_wait_s=30)
        results.append({"s": "S4 gpu 第 3 次（超配额）", "expect": "rejected(quota-exhausted)", "state": state,
                        "run": None, "audit": _audit_rules(gpu_audit)[-1:]})
        print(f"[S4] state={state}", flush=True)

        # S5：r2 冻结（双签驱替）→ 重启两个 provider → 新 grant 也死
        r2 = _dual_sign(_manifest(revision=2, status="frozen", previous=r1))
        verify_manifest(r2, trust_bundle=TRUST)
        _write_charter(r2)
        await gpu_rt.stop_serving()
        await cpu_rt.stop_serving()
        gpu_rt = await _serve_provider(partner_gpu.build_gpu_agent, GPU_WS, PORT_GPU, gpu_audit)
        cpu_rt = await _serve_provider(partner_cpu.build_cpu_agent, CPU_WS, PORT_CPU, cpu_audit)
        grant2 = issue_grant(
            edge_signing, domain=DOMAIN, domain_revision=2, subject_aic=EXP_AIC,
            assets=[{"providerAic": GPU_AIC, "skill": GPU_SKILL}], max_uses=5, ttl_seconds=3600,
        )
        state, _, _ = await _send_and_wait(
            EXP_AIC, PORT_GPU, GPU_AIC, GPU_WS / "acps" / "aip-tasks.sqlite3", GPU_WS / "runs",
            skill=GPU_SKILL, text="调用一次 gpu.run（域已冻结，应被验签前拒绝）",
            token=grant2.token, label="S5", max_wait_s=30)
        results.append({"s": "S5 域冻结后新 grant", "expect": "rejected(domain-frozen, 验签前)", "state": state,
                        "run": None, "audit": _audit_rules(gpu_audit)[-1:]})
        print(f"[S5] state={state}", flush=True)
    finally:
        for rt in (gpu_rt, cpu_rt):
            try:
                await rt.stop_serving()
            except Exception:  # noqa: BLE001
                pass

    (OUT / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 两机构域合作演示结果（domain={DOMAIN}）", "",
             f"- 机构 A（算力提供方）: edge-leader（{EDGE_AIC}）+ gpu/cpu 两块资源", "",
             f"- 机构 B（研枢集群）: supervisor（{SUPERVISOR_AIC}）+ experiment（consumer）", ""]
    for r in results:
        lines += [f"## {r['s']}", f"- 期望: {r['expect']}", f"- 实际: {r['state']}",
                  f"- 沙盒: {r['run']}", f"- 审计: {'; '.join(r['audit'])}", ""]
    lines.append("## gpu 侧完整审计")
    lines += [f"- {x}" for x in _audit_rules(gpu_audit)]
    lines.append("")
    lines.append("## cpu 侧完整审计")
    lines += [f"- {x}" for x in _audit_rules(cpu_audit)]
    lines += ["", "诚实口径：两机构当前同源 dev CA（密码学同信任域），机构边界为行政分离；",
              "B 侧集群现阶段本地模拟，上平台后整栈迁移即可成为真正的跨机器双边域。"]
    (OUT / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[done] 产物在 {OUT}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
