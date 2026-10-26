"""gpu-partner 本地演示：资源 agent 化 + PEP 准入 + 真实沙盒执行，一键复现。

场景（与 demo_pep.py 同款写法，本地真实 mTLS，不起 registry/CA）：
- G1  PEP 关：critic 越权直调 gpu.run → 被执行（漏洞复现，算力版）
- G2  PEP 开：critic 无凭据直调 gpu.run → 拒绝（Authentication required）
- G2b PEP 开：critic 调公开能力 gpu.env → 放行（社区互联线不断）
- G3  PEP 开：supervisor 签发委托 token（skill=gpu.run）→ critic 持 token 直调 → 放行
- G4  沙盒真实执行：直调 gpu.run capability，5060 容器内跑 CUDA matmul → 产物回传
- G5  远程数据集：调用方自托管数据源（回环 http 模拟）→ gpu.data.fetch 宿主侧代下
      + 安全解压 → gpu.run 在容器里读 /data/<name> 并用 GPU 计算
- G6  实验员全链路（真实 LLM）：experiment 持 leader 委托 token（act=实验员 AIC）
      经 aip_delegate 直连 gpu → PEP L2 放行 → gpu 侧 LLM 调 gpu.run 沙盒执行 →
      实验员 LLM 分析产物 → 结构化实验报告（.env 需可用 LLM key）

前置：
1. python acps_cluster/deploy/mint_gpu_identity.py --ca-key ../ACPs-community/ca-server/certs/ca.key
2. Docker Desktop 已启动（G4 需要；G1-G3 只需身份材料）
3. 镜像 pytorch:cu128 已在本地（第五轮已拉好；缺失时见 gpu_runner.ALLOWED_IMAGES）

    cd yanshu-AIC && python acps_cluster/delegation/demo_gpu.py
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path[:] = [p for p in _sys.path if _os.path.abspath(p or _os.getcwd()) != _HERE]
del _os, _sys, _HERE

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
CLUSTER = PROJECT / "acps_cluster"
for _p in (str(PROJECT), str(CLUSTER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wit import AcpsConfig, AgentCertificateRequest, AgentProviderProfile, WitRuntime  # noqa: E402
from wit.acps.direct import AipDirectClient  # noqa: E402


import partner_gpu  # noqa: E402
from delegation import issue_token, load_signing_context, token_data_item  # noqa: E402

GPU_WS = CLUSTER / "ws-gpu"
EXP_WS = CLUSTER / "ws-experiment"
SUPERVISOR_WS = CLUSTER / "ws-supervisor"
CRITIC_WS = CLUSTER / "ws-critic"

PRIVATE_SKILL = "gpu.run"
PUBLIC_SKILL = "gpu.env"

PORT_PEP_OFF = 18560
PORT_PEP_ON = 18561
PORT_G6 = 18562  # G6：PEP 开 + 真实 LLM（gpu 侧要真的执行 gpu.run）

ROWS: list[dict] = []


def _gpu_aic() -> str:
    enrollment = json.loads((GPU_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    aic = str(((enrollment.get("identity") or {}).get("ref") or {}).get("aic") or "")
    if not aic:
        raise SystemExit("ws-gpu/enrollment.json 里没有 AIC——先跑 mint_gpu_identity.py")
    return aic


def _leader_aic() -> str:
    enrollment = json.loads((SUPERVISOR_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    return str(((enrollment.get("identity") or {}).get("ref") or {}).get("aic") or "")


def _critic_aic() -> str:
    enrollment = json.loads((CRITIC_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    return str(((enrollment.get("identity") or {}).get("ref") or {}).get("aic") or "")


def _experiment_aic() -> str:
    enrollment = json.loads((EXP_WS / "acps" / "enrollment.json").read_text(encoding="utf-8"))
    aic = str(((enrollment.get("identity") or {}).get("ref") or {}).get("aic") or "")
    if not aic:
        raise SystemExit("ws-experiment/enrollment.json 里没有 AIC——先跑 mint_identity.py 铸造实验员身份")
    return aic


def _ensure_acs() -> None:
    """本地终稿缺失时按真实能力生成（partner_gpu 的帮助函数落在正式工作区一次）。"""
    if (GPU_WS / "acps" / "acs.final.json").is_file():
        return
    runtime = WitRuntime(
        partner_gpu.build_gpu_agent(None),
        workspace=GPU_WS,
        acps=_acps_config(int(os.getenv("ACPS_GPU_PORT", "8460"))),  # 本地演示配置，不依赖 .env
    )
    partner_gpu._ensure_local_acs(runtime)


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


def _command(*, sender_aic: str, skill: str | None, token: str | None = None,
             session_id: str | None = None, task_id: str | None = None,
             command: str = "start") -> dict:
    data_items: list[dict] = []
    if skill:
        data_items.append(
            {"type": "text", "text": "在 GPU 上跑一个小型矩阵乘实验", "metadata": {"skillId": skill}}
        )
    if token:
        data_items.append(token_data_item(token, target_aic=GPU_AIC, skill_id=skill or PRIVATE_SKILL))
    return {
        "type": "task-command",
        "id": f"msg-{uuid.uuid4()}",
        "sentAt": datetime.now(timezone.utc).isoformat(),
        "senderRole": "leader",
        "senderId": sender_aic,
        "sessionId": session_id or f"session-gpu-{uuid.uuid4()}",
        "taskId": task_id or f"task-{uuid.uuid4()}",
        "command": command,
        "commandParams": {},
        "dataItems": data_items,
    }


async def _cancel(caller_aic: str, port: int, response: dict) -> None:
    result = response.get("result") or {}
    session_id, task_id = result.get("sessionId"), result.get("taskId")
    if session_id and task_id:
        await _send(
            CRITIC_WS, caller_aic, port,
            _command(sender_aic=caller_aic, skill=None, session_id=session_id,
                     task_id=task_id, command="cancel"),
        )


async def _send(caller_ws: Path, caller_aic: str, port: int, command: dict) -> dict:
    client = AipDirectClient(
        client_certificate=caller_ws / "acps" / "client" / "certificate.pem",
        client_private_key=caller_ws / "acps" / "client" / "private-key.pem",
        trust_bundle=GPU_WS / "acps" / "trust-bundle.pem",
        local_aic=caller_aic,
        strict_interop=True,
    )
    request = {"jsonrpc": "2.0", "method": "rpc", "id": f"req-{uuid.uuid4()}", "params": {"command": command}}
    try:
        return await client.call(f"https://localhost:{port}/rpc", request, expected_partner_aic=GPU_AIC)
    finally:
        await client.aclose()


def _record(scenario: str, expect: str, response: dict, note: str = "") -> None:
    if "error" in response:
        outcome, detail = f"jsonrpc-error {(response['error'] or {}).get('code')}", str((response['error'] or {}).get('message'))
    else:
        result = response.get("result") or {}
        state = (result.get("status") or {}).get("state") or "?"
        items = ((result.get("status") or {}).get("dataItems")) or []
        outcome, detail = f"task {state}", str(items[0].get("text") if items else "")
    ROWS.append({"scenario": scenario, "expected": expect, "outcome": outcome, "detail": detail, "note": note})


async def _serve(port: int, *, enabled: bool, tmp_root: Path):
    audit = tmp_root / f"audit-{port}.jsonl"
    os.environ["ACPS_PEP_ENABLED"] = "1" if enabled else "0"
    os.environ["ACPS_PEP_LEADER_AICS"] = LEADER_AIC
    os.environ["ACPS_PEP_AUDIT_PATH"] = str(audit)
    workspace = tmp_root / f"ws-{port}"
    shutil.copytree(GPU_WS / "acps", workspace / "acps")
    runtime = WitRuntime(partner_gpu.build_gpu_agent(None), workspace=workspace, acps=_acps_config(port))
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[demo] gpu 监听 https://localhost:{port}/rpc → {status.status}（PEP {'on' if enabled else 'off'}）")
    return runtime, audit


CUDA_MATMUL_MAIN = """\
import json, time, torch

x = torch.randn(1024, 1024, device="cuda")
torch.cuda.synchronize()
t0 = time.time()
for _ in range(50):
    y = x @ x
torch.cuda.synchronize()
elapsed_ms = (time.time() - t0) * 1000
metrics = {
    "device": torch.cuda.get_device_name(0),
    "capability": list(torch.cuda.get_device_capability(0)),
    "torch": torch.__version__,
    "matmul50_ms": round(elapsed_ms, 2),
    "note": "ran in sandbox container, network=none, read-only rootfs",
}
with open("metrics.json", "w") as f:
    json.dump(metrics, f)
print("GPU-OK", json.dumps(metrics))
"""


async def _g4_sandbox() -> None:
    print("\n──── G4 沙盒真实执行（capability 直调，5060 容器内 CUDA matmul）────")
    result = await partner_gpu.gpu_run(
        files={"main.py": CUDA_MATMUL_MAIN}, timeout_seconds=180
    )
    artifacts = {a["name"]: a for a in result["artifacts"]}
    metrics = artifacts.get("metrics.json", {}).get("content", "")
    passed = result["ok"] and "GPU-OK" in result["stdout"] and "metrics.json" in artifacts
    ROWS.append({
        "scenario": "G4 沙盒执行 CUDA matmul（断网容器）",
        "expected": "exit 0 + GPU-OK + metrics.json 产物",
        "outcome": "sandbox ok" if passed else f"sandbox failed: {result['error'] or result['stderr'][:120]}",
        "detail": result["stdout"][:160],
        "note": f"runId={result['runId']} duration={result['durationSeconds']}s metrics={metrics[:120]}",
    })


G5_MAIN = """\
import json, torch

# 数据集由 gpu.data.fetch 宿主侧代下（调用方自托管源），容器内只读挂载为 /data/<name>
rows = [r.split(",") for r in open("/data/__DATASET__/train.csv").read().strip().splitlines()[1:]]
values = torch.tensor([float(r[1]) for r in rows], device="cuda")
result = {
    "datasetRows": len(rows),
    "sumOnGpu": float(values.sum()),
    "gpu": torch.cuda.get_device_name(0),
    "dataCrossedNetworkOnce": "仅宿主侧代下一次，容器断网",
}
json.dump(result, open("data_result.json", "w"))
print("DATA-GPU-OK", json.dumps(result))
"""


async def _g5_remote_data() -> None:
    """远程数据集全链路：调用方自托管（回环 http 模拟）→ fetch 代下解压 → 容器内读取 + GPU 计算。"""
    import hashlib
    import threading
    import zipfile
    from functools import partial
    from http.server import HTTPServer, SimpleHTTPRequestHandler

    import gpu_runner

    print("\n──── G5 远程数据集（回环模拟调用方数据源 → fetch → 容器读取）────")
    serving = tmp_dir = None
    name = f"demo_remote_{uuid.uuid4().hex[:6]}"
    try:
        import tempfile

        tmp_dir = tempfile.mkdtemp(prefix="gpu-data-src-")
        serving = Path(tmp_dir)
        with zipfile.ZipFile(serving / "ds.zip", "w") as zf:
            zf.writestr("train.csv", "id,value\n1,10.5\n2,20.25\n3,30.125\n")
        good_sha = hashlib.sha256((serving / "ds.zip").read_bytes()).hexdigest()
        handler = partial(SimpleHTTPRequestHandler, directory=str(serving))
        handler.log_message = lambda *a, **k: None
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/ds.zip"
        try:
            fetched = await partner_gpu.gpu_data_fetch(name=name, url=url, sha256=good_sha)
        finally:
            server.shutdown()
        if not fetched.get("ok"):
            ROWS.append({"scenario": "G5 远程数据集全链路", "expected": "fetch ok + 容器读取",
                         "outcome": f"fetch failed: {fetched.get('error')}", "detail": "", "note": ""})
            return

        run = await partner_gpu.gpu_run(files={"main.py": G5_MAIN.replace("__DATASET__", name)}, timeout_seconds=180)
        artifacts = {a["name"]: a for a in run["artifacts"]}
        passed = run["ok"] and "DATA-GPU-OK" in run["stdout"] and "data_result.json" in artifacts
        ROWS.append({
            "scenario": "G5 远程数据集全链路（fetch→挂载→GPU 计算）",
            "expected": "fetch ok + 容器读取",
            "outcome": "data-pipeline ok" if passed else f"failed: {run['error'] or run['stderr'][:120]}",
            "detail": run["stdout"][:160],
            "note": f"dataset={name} fetchBytes={fetched.get('totalBytes')} sha256校验通过 "
                    f"runId={run['runId']} duration={run['durationSeconds']}s",
        })
    finally:
        import shutil

        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(gpu_runner.DATASETS_DIR / name, ignore_errors=True)



async def _g6_experiment(gpu_aic: str, tmp_root: Path) -> None:
    """G6 实验员全链路（真实 LLM）：PEP 开的 gpu（带 provider）+ 实验员持 token 直连。

    链路：experiment LLM 生成代码 → 本地沙盒预校验 → aip_delegate 持 token 直连 gpu
    （PEP L2：act.aic=实验员 AIC）→ gpu 侧 LLM 调 gpu.run 断网容器执行 → 实验员 LLM
    分析产物 → 结构化报告。
    """
    import agents  # noqa: PLC0415  加载 .env（实验员与 gpu 侧都要真 LLM）
    from wit import AgentState, WitRuntime  # noqa: PLC0415
    from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent  # noqa: PLC0415

    import partner_experiment  # noqa: PLC0415

    print("\n──── G6 实验员全链路（真实 LLM，持委托 token 直连 GPU 执行）────")
    provider = agents.build_provider()
    g6_ws = tmp_root / f"ws-{PORT_G6}"
    shutil.copytree(GPU_WS / "acps", g6_ws / "acps")  # 没有身份材料 serve 会 blocked
    runtime = WitRuntime(partner_gpu.build_gpu_agent(provider), workspace=g6_ws,
                         acps=_acps_config(PORT_G6))
    try:
        status = await runtime.serve_direct_partner(host="127.0.0.1", port=PORT_G6, amp=False)
        print(f"[demo] gpu(G6, 带LLM) 监听 https://localhost:{PORT_G6}/rpc → {status.status}")

        # 实验员 caller runtime：临时工作区 + 发现缓存指向 G6 端点
        exp_ws = tmp_root / f"ws-{PORT_G6}-exp"
        shutil.copytree(EXP_WS / "acps", exp_ws / "acps")
        cache_dir = exp_ws / "acps" / "discovered"
        cache_dir.mkdir(parents=True, exist_ok=True)
        acs = json.loads((GPU_WS / "acps" / "acs.final.json").read_text(encoding="utf-8"))
        acs["aic"] = gpu_aic
        for endpoint in acs.get("endPoints", []):
            endpoint["url"] = f"https://localhost:{PORT_G6}/rpc"
        cache_discovered_agent(cache_dir=cache_dir,
                               summary=normalize_discovered_agent(acs, ranking=1), raw_acs=acs)
        exp_runtime = WitRuntime(partner_experiment.build_experiment_agent(None),
                                 workspace=exp_ws, acps=_acps_config(18470))

        experiment_aic = _experiment_aic()
        issued = issue_token(
            load_signing_context(SUPERVISOR_WS / "acps" / "client",
                                 trust_bundle=GPU_WS / "acps" / "trust-bundle.pem"),
            delegate_aic=experiment_aic, target_aic=gpu_aic, skill_id="gpu.run", ttl_seconds=600,
        )
        try:
            result = await partner_experiment.experiment_pipeline(
                exp_runtime, AgentState(),
                question="在 RTX 5060 上验证 PyTorch CUDA 可用性并测量 1024x1024 矩阵乘 50 次的耗时",
                method="torch.randn(1024,1024,device='cuda') 循环 50 次 @ 后 synchronize 计时；结果写 result.json",
                token=issued.token, gpu_aic=gpu_aic, timeout_seconds=600,
            )
        finally:
            pass

        report = result.get("report") or {}
        passed = bool(result.get("ok")) and bool(report.get("verdict"))
        ROWS.append({
            "scenario": "G6 实验员全链路（LLM 代码生成→持 token 直连→GPU 执行→分析报告）",
            "expected": "exp-pipeline ok（报告含 verdict）",
            "outcome": "exp-pipeline ok" if passed else f"failed@{result.get('stage')}: {str(result.get('error'))[:120]}",
            "detail": str(report.get("summary", ""))[:140],
            "note": f"verdict={report.get('verdict')} metrics={json.dumps(report.get('metrics', {}), ensure_ascii=False)[:100]} "
                    f"taskRef={result.get('taskRef')}",
        })
    finally:
        await runtime.stop_serving()
        await provider.aclose()


async def main() -> None:
    global GPU_AIC, LEADER_AIC
    _ensure_acs()
    GPU_AIC = _gpu_aic()
    LEADER_AIC = _leader_aic()
    CRITIC_AIC = _critic_aic()
    tmp_root = Path(tempfile.mkdtemp(prefix="gpu-demo-"))
    print(f"[demo] gpu AIC={GPU_AIC}\n[demo] 临时工作区 {tmp_root}")

    try:
        # G1：PEP 关——越权直调 gpu.run（算力版漏洞复现）
        runtime, _ = await _serve(PORT_PEP_OFF, enabled=False, tmp_root=tmp_root)
        try:
            response = await _send(
                CRITIC_WS, CRITIC_AIC, PORT_PEP_OFF,
                _command(sender_aic=CRITIC_AIC, skill=PRIVATE_SKILL),
            )
            _record("G1 越权直调 gpu.run（PEP 关）", "task accepted（漏洞成立）", response)
            await _cancel(CRITIC_AIC, PORT_PEP_OFF, response)
        finally:
            await runtime.stop_serving()

        # G2 / G2b / G3：PEP 开
        runtime, audit = await _serve(PORT_PEP_ON, enabled=True, tmp_root=tmp_root)
        try:
            _record(
                "G2 越权直调 gpu.run（PEP 开）", "task rejected",
                await _send(CRITIC_WS, CRITIC_AIC, PORT_PEP_ON,
                            _command(sender_aic=CRITIC_AIC, skill=PRIVATE_SKILL)),
            )
            response = await _send(
                CRITIC_WS, CRITIC_AIC, PORT_PEP_ON,
                _command(sender_aic=CRITIC_AIC, skill=PUBLIC_SKILL),
            )
            _record("G2b 外部调公开能力 gpu.env（L0）", "task accepted", response)
            await _cancel(CRITIC_AIC, PORT_PEP_ON, response)
            issued = issue_token(
                load_signing_context(SUPERVISOR_WS / "acps" / "client",
                                     trust_bundle=GPU_WS / "acps" / "trust-bundle.pem"),
                delegate_aic=CRITIC_AIC, target_aic=GPU_AIC, skill_id=PRIVATE_SKILL,
            )
            response = await _send(
                CRITIC_WS, CRITIC_AIC, PORT_PEP_ON,
                _command(sender_aic=CRITIC_AIC, skill=PRIVATE_SKILL, token=issued.token),
            )
            _record("G3 持委托 token 直调 gpu.run（L2）", "task accepted", response,
                    note=f"jti={issued.jti[:8]}… delegationId={issued.delegation_id}")
            await _cancel(CRITIC_AIC, PORT_PEP_ON, response)
        finally:
            await runtime.stop_serving()

        await _g4_sandbox()
        await _g5_remote_data()
        await _g6_experiment(GPU_AIC, tmp_root)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("\n══════════════════════ 结果汇总 ══════════════════════")
    for row in ROWS:
        expect_ok = row["expected"].startswith(("task accepted", "sandbox ok", "exit 0", "fetch ok", "data-pipeline ok", "exp-pipeline ok"))
        got_ok = row["outcome"] in {"task accepted", "task working", "sandbox ok", "data-pipeline ok", "exp-pipeline ok"}
        mark = "✅" if got_ok == expect_ok else "❌"
        print(f"{mark} {row['scenario']}")
        print(f"    期望 {row['expected']} / 实际 {row['outcome']}  {row['detail'][:80]}")
        if row["note"]:
            print(f"    备注 {row['note'][:140]}")


if __name__ == "__main__":
    asyncio.run(main())
