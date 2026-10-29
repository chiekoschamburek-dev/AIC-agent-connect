"""研枢-Experiment-Partner（实验员）：集群里第一个"真实实验"角色——GPU 资源的智能消费者。

角色定位（区别于 gpu-partner 的"哑资源"）：
- gpu-partner：资源节点，LLM 只做协议翻译（能力签名即边界）；
- experiment：**实验员**——把"假设/方法"翻译成实验代码，持 leader 委托的 token 直连
  gpu 执行（remedy-3 的天然消费者），分析结果并产出结构化实验报告；
- supervisor：只归因选址签 token，不碰代码与 stdout（§2.1 中转死结不复发）。

协议语义对齐：实验员恰好就是委托链 leader→experiment→gpu 中 `act` claim 指向的受托人。

能力：exp.run（**公开**——2026-10-23 设计定案：gpu/cpu 走路线 B 本地身份、不注册平台，
公开的 exp.run 是算力对外**唯一可寻址入口**，公开性是接口设计而非疏漏。代价用限额
约束：免令牌（L0）路径按**调用方 AIC** 每日计次（ACPS_EXP_PUBLIC_DAILY_LIMIT，默认 3；
0=拒绝一切匿名；负数=不限），持委托令牌的委派不受此限（其 jti 由 PEP 计数）。
高成本闸门仍保留在**算力侧**：gpu/cpu 的 PEP 按令牌放行）。
token 来源（阶段三前的两级形态）：
- v1（本文件）：capability 从环境变量 ACPS_EXP_GPU_TOKEN 取 token；缺失时返回结构化
  need（对齐 awaiting-input 纪律："缺什么、怎么补"），由 leader 签发后经 continue 下发；
- 阶段三：token 经 continue dataItems 到达 → 剥离钩子落 local_context（host-only）→
  capability 从 local_context 取，全程不进模型上下文（与 delegation token 卫生同款）。

运行：python acps_cluster/agents/partner_experiment.py --local   （本地直连演示，端口默认 8461）
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wit import AcpsDirect, DataPart, TextPart, WitAgent, WitRuntime, capability  # noqa: E402

from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from acps_cluster.core import llm_tools  # noqa: E402
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks, token_data_item
from acps_cluster.delegation.bridge import BridgeHooks, current_delegation, current_runtime, current_state  # noqa: E402
from acps_cluster.agents.partner_workspace_client import (  # noqa: E402
    WORKSPACE_ACCESS_NOTE,
    WorkspaceAccessBudgetHooks,
    build_workspace_access_capabilities,
)
from acps_cluster.core.workspace_access import enabled as workspace_access_enabled  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-experiment"
EXP_PORT_ENV = "ACPS_EXPERIMENT_PORT"
EXP_PORT_DEFAULT = 8461
TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}

NEED_TOKEN = {
    "need": "delegation.token",
    "skill": "gpu.run",
    "target": "gpu-partner",
    "reason": "执行实验需要 GPU 委托令牌（高成本资源，PEP L2 门槛）",
    "how": "leader 用 issue_delegation_token 签发（delegate=本实验员 AIC，skill=gpu.run）后 continue 附带",
    "blocking": True,
}


def token_channel_hint() -> dict:
    """缺令牌时顺带回一句通道状态。

    桥未就绪 = 本机根本没挂 `DelegationTokenScrubHooks`（最常见于伙伴启动时
    `ACPS_PEP_ENABLED` 没开）：这时 leader 无论签发多少枚令牌都会在受托侧被丢弃，
    表现却是"还缺令牌"——2026-10-17 实测因此白烧了三轮现签。
    """
    from acps_cluster.delegation.bridge import bridge_ready

    if bridge_ready():
        return {}
    return {"channel": "bridge-not-ready",
            "check": "受托侧没有令牌写入通道（伙伴启动时未启用 PEP 即会如此）："
                     "请先确认本机挂了 DelegationTokenScrubHooks，再重发令牌"}


def admission_denial(result: dict) -> dict:
    """把算力侧 admission 的拒绝折成"缺一枚新令牌"的 awaiting（而不是实验失败）。"""
    detail = str((result or {}).get("summary") or (result or {}).get("error") or "")[:200]
    return {**NEED_TOKEN,
            "reason": f"上一枚令牌在算力侧 admission 被拒（{detail or '无详情'}）——重新签发一枚后 continue",
            "taskRef": (result or {}).get("task_ref")}

_CODEGEN_SYSTEM = """你是严谨的实验工程师。把研究问题转写成可在 GPU 沙盒中运行的 PyTorch 实验代码。
硬性环境约束（违反即失败）：
- 容器断网：禁止任何网络访问、禁止 pip install；
- 文件系统只读，可写目录只有当前工作目录（产物直接写到当前目录，不要建子目录）；
- 只用镜像内已有库（torch/numpy/json/time/csv 等；**不要依赖 sklearn / torchvision / keras / pandas / scipy**）；
- 数据集按任务说明二选一：
  · **托管数据集**：数据已由 gpu 宿主侧代下，容器内只读挂载在 `/data/<name>/`；
    用 numpy/csv 直接读该目录下的文件（若是 OpenML 的 `.arff`：跳过所有 `@` 开头的行、
    到 `@data` 之后按逗号切分即可，不要引 scipy.io.arff）；
  · **代码内合成**：仅当任务明确要求"合成/不读任何数据"时，用固定随机种子自造张量。
  两种情况下都**不得**用库自带数据集（load_digits/mnist/iris 等）替代；
- 入口文件必须叫 main.py；实验规模要小（几十秒内完成）；
- 结果写成一个 JSON 文件（名字 result.json），并在 stdout 最后一行打印 RESULT-OK 加该 JSON。
只输出一个 JSON 对象（可加 ```json 围栏），格式：
{"files": {"main.py": "<完整代码>", "note": "<一句话说明>"} }"""

_ANALYSIS_SYSTEM = """你是实验分析员。根据实验的 stdout、exit code 与产物，产出结构化实验报告。
只输出一个 JSON 对象（可加 ```json 围栏），格式：
{"summary": "<一句话结论>", "metrics": {<关键指标，来自 result.json>},
 "findings": ["<发现1>", "..."], "caveats": ["<局限/注意>", "..."],
 "verdict": "supported|refuted|inconclusive"}
不得编造结果里没有的数字。"""

# 代码里禁止出现的"拿库自带数据集冒充数据"的特征（2026-10-02 实测：镜像没有 sklearn 时
# 模型会退化成 synthetic_fallback，或改用 load_digits，产物看起来跑通了其实与数据集无关）
_CHEAT_MARKERS = ("load_digits", "load_iris", "make_classification", "fetch_openml", "torchvision.datasets")


def _dataset_spec(dataset: str) -> dict:
    """把 `dataset` 参数解析成 {mode, name, url, raw}。

    约定（任务文本由 leader 给）：`"<name> | <url>"` 或只有一个 `https://…` URL
    （此时名字取 URL 末段）；`"synthetic"`/空 → 代码内合成。
    **不再传 CSV 正文**：正文要经两侧模型逐字转写，慢网关上必然超时（实测 6 次撞 300s 上限）。
    """
    text = (dataset or "").strip()
    if not text:
        return {"mode": "none", "name": "", "url": "", "raw": ""}
    lowered = text.lower()
    if any(k in lowered for k in ("synthetic", "合成", "不读任何数据", "无数据集")):
        return {"mode": "synthetic", "name": "", "url": "", "raw": text}
    url = ""
    for token in re.split(r"[\s|,]+", text):
        if token.startswith("http://") or token.startswith("https://"):
            url = token
            break
    name = ""
    for token in re.split(r"\|", text):
        candidate = token.strip()
        if candidate and not candidate.startswith("http") and "=" not in candidate and len(candidate) <= 64:
            if re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", candidate):
                name = candidate
                break
    if not name and url:
        name = re.sub(r"\W+", "_", url.rstrip("/").rsplit("/", 1)[-1])[:32] or "dataset"
    if url:
        return {"mode": "fetch", "name": name or "dataset", "url": url, "raw": text}
    return {"mode": "named", "name": name or re.sub(r"\W+", "_", text)[:32], "url": "", "raw": text}


def _dataset_for_prompt(spec: dict) -> str:
    mode = spec.get("mode")
    if mode == "fetch":
        fname = spec["url"].rstrip("/").rsplit("/", 1)[-1] or "data"
        return (
            f"托管数据集：名称 `{spec['name']}`，来源 {spec['url']}。"
            f"宿主侧已代下，容器内只读挂载：目录 `/data/{spec['name']}/`，其中有文件 `{fname}`。"
            f"实验代码从那里读数据。"
        )
    if mode == "named":
        return f"托管数据集：名称 `{spec['name']}`，实验代码从 `/data/{spec['name']}/` 读数据（若该目录为空则报错，不要自造数据）。"
    if mode == "synthetic":
        return "代码内合成（任务明确要求）：用固定种子自造小型张量，不读任何外部数据。"
    return "无（自造小型张量）"


def dataset_followed(code: str, spec: dict) -> bool:
    """代码侧闸门：托管数据集必须被真正读取，且不得用库自带数据集/合成数据顶替。

    返回 False 时上游会带反馈重写（这是防止"实验看起来跑通、其实与数据集无关"的关键闸门）。
    """
    mode = spec.get("mode")
    if mode in ("none", "synthetic"):
        return True
    if any(marker in code for marker in _CHEAT_MARKERS):
        return False
    name = str(spec.get("name") or "")
    if "/data/" in code or (name and name in code):
        return True
    return "input/" in code  # 允许走 gpu.run 的 input 目录挂载


def _extract_json(text: str) -> dict:
    """从 LLM 输出里稳健地取 JSON（容忍围栏/前后废话）。"""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    braces = re.search(r"\{.*\}", text, re.S)
    if braces:
        candidates.append(braces.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise ValueError(f"LLM 输出里解析不出 JSON 对象: {text[:200]}")


async def _generate_code(question: str, method: str, spec: dict, *, feedback: str = "") -> dict:
    user = json.dumps(
        {
            "研究问题": question,
            "方法要点": method,
            "数据集": _dataset_for_prompt(spec),
            "上次失败的反馈（如有，务必修正）": feedback or "无",
        },
        ensure_ascii=False,
    )
    raw = await llm_tools.chat(
        _CODEGEN_SYSTEM, user, temperature=0.2,
        # 24000 是实测值：MiniMax-M3.1-Flash 在 8000 下 thinking 吃满预算、正文恒为空
        # （finish=length），24000 才自然结束并产出可解析 JSON（单次约 157s）。
        max_tokens=int(os.getenv("ACPS_EXP_CODEGEN_MAX_TOKENS", "24000")),
        strict_length=True,   # 空正文/截断=失败：让两轮重试拿到可行动的反馈
    )
    return _extract_json(raw)


async def experiment_pipeline(
    runtime,
    state,
    *,
    question: str,
    method: str,
    dataset: str = "",
    token: str,
    compute_aic: str,
    compute_skill: str = "gpu.run",
    timeout_seconds: int = 600,
) -> dict:
    """完整实验链：LLM 生成代码 → 本地预校验 → 持 token 直连指定算力 agent 执行 → LLM 出报告。

    `runtime` 须挂 AipDelegationHooks（aip_delegate 必需）。任何一步失败都收敛为
    {"ok": False, "stage": ..., "error": ...}，不抛异常。
    """
    from acps_cluster.runtime import gpu_runner

    # ① 代码生成（校验失败带反馈重试一次）
    files: dict | None = None
    feedback = ""
    spec = _dataset_spec(dataset)
    for _ in (1, 2):
        try:
            generated = await _generate_code(question, method, spec, feedback=feedback)
        except Exception as exc:  # noqa: BLE001 — LLM 网络错误也收敛为失败
            feedback = f"代码生成调用失败: {exc}"
            continue
        candidate = generated.get("files")
        if isinstance(candidate, dict) and candidate:
            trial = {str(k): str(v) for k, v in candidate.items()}
            _, error = gpu_runner.validate_job({"files": trial, "timeout_seconds": timeout_seconds})
            if error is None and not dataset_followed(str(trial.get("main.py") or ""), spec):
                error = (
                    "生成的代码没有读托管数据集（疑似换成了库自带数据集或合成数据）——"
                    "托管数据集在容器内只读挂载于 /data/<name>/，必须从那里读，"
                    "不得用 load_digits/load_iris/make_classification/torchvision 替代"
                )
            if error is None:
                files = trial
                break
            feedback = f"上次代码未过沙盒校验: {error}"
        else:
            feedback = "输出缺少非空 files 字段"
    if files is None:
        return {"ok": False, "stage": "codegen", "error": f"两轮生成的代码均未通过沙盒校验: {feedback}"}

    # ② 持 token 直连指定算力 agent（PEP L2：act.aic == 本实验员 AIC）。
    #    代码以围栏块附在任务文本里（模型对代码块转写最可靠），token 走 dataItems。
    #    托管数据集：**只把 name+url 传过去**，由宿主侧代下（沙盒外有网），
    #    容器只读挂 /data/<name> —— 数据不走任何模型上下文（把 15KB CSV 逐字转写会撞 300s 上限，实测）。
    payload = token_data_item(token, target_aic=compute_aic, skill_id=compute_skill)
    code_blocks = "\n".join(
        f"```python\n# 文件: {name}\n{content}\n```" for name, content in files.items()
    )
    fetch_hint = ""
    if spec.get("mode") == "fetch":
        fetch_hint = (
            f"【第一步】请先调用 gpu.data.fetch(name=\"{spec['name']}\", url=\"{spec['url']}\") "
            f"把数据集代下到本节点（宿主侧下载），完成后容器内只读挂载在 /data/{spec['name']}/。\n"
        )
    task_text = (
        f"请执行以下实验并原样返回 {compute_skill} 的结果：研究问题：{question}\n"
        f"{fetch_hint}"
        f"实验代码（调用 {compute_skill} 时把这些文件原样作为 files 参数传入，不要改写）：\n{code_blocks}"
    )
    if spec.get("mode") != "fetch":
        # 2026-10-29：静态资源路由（GPU 默认不跑 LLM）——普通实验直接给结构化 args，
        # 不再让对端模型从代码块里猜 files/entrypoint；fetch 模式需要先 gpu.data.fetch
        # 再 gpu.run 两步，保留自然语言交给对端（可回落到 LLM）。
        task_text = json.dumps({
            "skill": compute_skill,
            "args": {"files": files, "entrypoint": ["python", "main.py"]},
        }, ensure_ascii=False)
    try:
        result = await runtime.aip_delegate(
            state,
            partner_aic=compute_aic,
            request=[
                TextPart(task_text),
                DataPart(data=dict(payload["data"]), metadata=dict(payload["metadata"])),
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stage": "submit", "error": f"aip_delegate 失败: {exc}"}

    task_ref = result.get("task_ref")
    if result.get("state") == "rejected":
        # 令牌在算力侧 admission 被拒（重放/过期/绑定不符）**不是实验结果**：如实回
        # awaiting，让 leader 现签一枚新令牌再 continue。静态令牌跨不过长循环——
        # 2026-10-17 实测一枚 600s 令牌撑不完两轮迭代，后续尝试全死在 admission，
        # 却被记成"实验失败"，leader 据此把基础设施故障写进了研究结论。
        return {"ok": False, "awaiting": admission_denial(result)}

    # ③ 等待完成（gpu 侧 LLM 会调用 gpu.run 跑容器），leader 收尾 complete
    for _ in range(int(timeout_seconds / 5) + 12):
        if result.get("state") in TERMINAL_STATES or result.get("state") == "awaiting-completion":
            break
        await asyncio.sleep(5)
        result = await runtime.aip_task(state, action="wait", task_ref=task_ref)
    if result.get("state") == "awaiting-completion":
        result = await runtime.aip_task(state, action="complete", task_ref=task_ref)

    products = result.get("products") or []
    blob = json.dumps(products, ensure_ascii=False, default=str)
    if result.get("state") != "completed":
        return {"ok": False, "stage": "execution", "error": f"gpu 任务终态 {result.get('state')}",
                "task": {k: result.get(k) for k in ("state", "task_ref")}, "products": blob[:2000]}

    # ④ LLM 分析 → 结构化报告
    try:
        raw = await llm_tools.chat(
            _ANALYSIS_SYSTEM,
            f"实验任务终态: completed\n产物（含 stdout/artifacts）:\n{blob[:12000]}",
            temperature=0.2,
        )
        report = _extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        report = {"summary": f"分析失败: {exc}", "metrics": {}, "findings": [], "caveats": ["报告未生成"],
                  "verdict": "inconclusive"}

    artifacts_inline = harvest_artifact_contents(products)
    return {
        "ok": True,
        "stage": "done",
        "taskRef": task_ref,
        "files": files,
        "report": report,
        "products": blob[:4000],
        # 逐字产物 + 校验和：leader 原样取用交评测，一致性可验，不靠模型转写
        "artifactContents": artifacts_inline,
        "artifactSha256": {k: hashlib.sha256(v.encode("utf-8")).hexdigest()[:12]
                           for k, v in artifacts_inline.items()},
    }


def resolve_compute_target(roster, target: str) -> tuple[str | None, str | None, str | None]:
    """按 key/展示名/AIC 解析算力目标 → (aic, 沙盒技能 id, 错误原因)。

    只接受声明了 `gpu.run` / `cpu.run` 的算力成员；解析不到 AIC（未注册/无寻址桩）同样拒绝。
    """
    from acps_cluster.core import cluster_roster

    member = cluster_roster.resolve_member(roster, partner=target)
    if member is None:
        return None, None, f"算力目标 {target!r} 不在名册（可用 key/展示名/AIC 指定，如 gpu / cpu）"
    caps = set(member.capabilities)
    if "gpu.run" in caps:
        skill = "gpu.run"
    elif "cpu.run" in caps:
        skill = "cpu.run"
    else:
        return None, None, f"{member.key!r} 不是算力 agent（未声明 gpu.run/cpu.run 能力）"
    if not member.aic:
        return None, None, f"算力目标 {member.key!r} 在当前节点解析不到 AIC（未注册/无寻址桩）"
    return member.aic, skill, None


def _external_compute_grant() -> dict:
    """读取算力公司签发的 grant（第三方算力模式）。

    与 `issue_delegation_token` 的区别：这里**只加载资源方签发的凭证**，研枢
    supervisor 不参与签名。文件由算力方通过带外通道投递，0600 保护，不进入模型上下文。
    形如：{"token": "<JWS>", "targetAic": "<算力 GPU AIC>", "skill": "gpu.run"}
    """
    import json
    import os

    raw = str(os.getenv("ACPS_COMPUTE_GRANT_FILE", "")).strip()
    if not raw:
        return {}
    try:
        data = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[experiment] 外部算力 grant 读取失败 {raw}: {type(exc).__name__}: {exc}",
              flush=True)
        return {"token": "", "targetAic": "", "skill": "gpu.run",
                "error": f"外部 grant 读取失败: {type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        return {"token": "", "targetAic": "", "skill": "gpu.run",
                "error": "外部 grant 文件必须是 JSON 对象"}
    return {
        "token": str(data.get("token") or data.get("grant") or "").strip(),
        "targetAic": str(data.get("targetAic") or data.get("target_aic") or "").strip(),
        "skill": str(data.get("skill") or data.get("skillId") or "gpu.run").strip(),
    }


# ── L0 公开调用的按调用方限额（2026-10-23 设计定案，对应审查 H2 的替代整改）──
# exp.run 保持 public 是接口设计：gpu/cpu 不注册平台，公开的 exp.run 是算力对外
# 唯一可寻址入口。但宿主侧静态 grant 是"所有匿名调用方共享一枚"，不限流的话
# 任何平台 agent 都能几分钟内烧光配额（H2 的实际危害）。故公开路径按**调用方
# AIC** 做每日计次；持委托令牌的委派是另一条授权路径，不受此限。

PUBLIC_L0_LIMIT_ENV = "ACPS_EXP_PUBLIC_DAILY_LIMIT"
PUBLIC_L0_LIMIT_DEFAULT = 3
_PUBLIC_L0_STATE = WORKSPACE / "usage" / "public-l0.json"


def public_l0_gate(delegation: dict | None, *, peer_aic: str | None = None,
                   state_path: Path | None = None, now=None) -> dict | None:
    """免令牌（L0）调用的按调用方限额闸门：放行返回 None，拒绝返回结构化原因。

    - 持委托令牌的委派直接放行——令牌本身就是授权，其 jti 已由 PEP 计数；
    - 对端 AIC 取不到（本机直调、离线演示、单测）不计数：威胁模型是平台上的
      远程匿名调用方，它们必然带 mTLS 对端身份；
    - 计数按 UTC 日分窗，落盘 `ws-experiment/usage/public-l0.json`（原子写），
      重启不丢；状态损坏按空窗口重计——宁可重计，不因计数器坏掉而停摆算力接口。
    - 上限：ACPS_EXP_PUBLIC_DAILY_LIMIT，默认 3；0=拒绝一切匿名（fail-closed），
      负数=不限（显式关闭闸门）。
    """
    if (delegation or {}).get("token"):
        return None
    if not peer_aic:
        return None
    import datetime as _dt

    now = now or _dt.datetime.now(_dt.timezone.utc)
    day = now.strftime("%Y-%m-%d")
    try:
        limit = int(str(os.getenv(PUBLIC_L0_LIMIT_ENV, str(PUBLIC_L0_LIMIT_DEFAULT))).strip())
    except ValueError:
        limit = PUBLIC_L0_LIMIT_DEFAULT
    if limit < 0:
        return None
    path = state_path or _PUBLIC_L0_STATE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        if path.exists():
            print(f"[experiment] L0 限额状态损坏，按空窗口重计: {path}", flush=True)
        state = {}
    if state.get("day") != day or not isinstance(state.get("callers"), dict):
        state = {"day": day, "callers": {}}
    callers: dict = state["callers"]
    used = int(callers.get(peer_aic, 0))
    if used >= limit:
        return {
            "refused": "public-quota-exceeded",
            "reason": (f"公开（免令牌）调用达到当日上限 {limit} 次（已用 {used}）；"
                       f"请改走委托令牌路径（由 leader 签发），或 UTC 日切后重试"),
            "caller": peer_aic,
            "limit": limit,
            "used": used,
            "window": day,
        }
    callers[peer_aic] = used + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return None


@capability(
    id="exp.run",
    description=(
        "实验员：把研究问题转写为实验代码，持算力方 grant 在 gpu-partner 沙盒中"
        "执行，返回结构化实验报告（summary/metrics/findings/caveats/verdict）。"
        "公开能力（算力对外接口，设计如此）：免令牌调用按调用方每日限额；"
        "持委托令牌的委派不受此限。"
    ),
    public=True,
    acps_skill=True,
    tags=["research", "experiment", "gpu"],
)
async def exp_run(question: str, method: str = "", dataset: str = "", timeout_seconds: int = 600,
                  compute_agent: str = "") -> dict:
    """experiment 只做实验编排；算力闸门在算力侧（gpu/cpu 的 PEP 按令牌放行）。

    `compute_agent`：可选指定算力目标（名册 key/展示名/AIC，如 "gpu" / "cpu"）；
    缺省沿用令牌/令牌文件/环境变量携带的目标。
    token 来源优先级：桥 ContextVar → host 令牌文件（scrub 钩子落盘，跨 run 可靠）
    → 环境变量（离线直调兜底）。
    """
    import os

    from wit import AgentState

    delegation = current_delegation()
    runtime, state = current_runtime(), current_state()
    # 公开（免令牌）路径的按调用方限额：无委托令牌的远程调用按对端 AIC 每日计次，
    # 超额拒绝——静态算力 grant 不再是"全体匿名共享的无界资源"（H2 替代整改）。
    gate = public_l0_gate(delegation, peer_aic=cluster_common.current_peer_aic())
    if gate is not None:
        print(f"[experiment] L0 公开调用限额拒绝: {gate['caller']} 已用 {gate['used']}/{gate['limit']}",
              flush=True)
        return {"ok": False, **gate}
    # 令牌来源优先级：
    # 第三方算力 grant 文件（算力方签发，研枢不签）→ 桥 ContextVar（remedy-3）
    # → host 令牌文件 → 环境变量（离线直调兜底）
    from acps_cluster.delegation.client import current_task_id, read_token_file

    external = _external_compute_grant()
    file_token = read_token_file(WORKSPACE / "tokens", task_id=current_task_id()) or {}
    token = (external.get("token")
             or (delegation or {}).get("token")
             or file_token.get("token")
             or os.getenv("ACPS_EXP_GPU_TOKEN", "").strip())
    if not token:
        reason = external.get("error") or ""
        return {"ok": False, "awaiting": {
            **NEED_TOKEN,
            **({"reason": reason} if reason else {}),
            "how": "算力公司签发 grant 后投递到 ACPS_COMPUTE_GRANT_FILE；不要用研枢 leader 自签",
        }}
    # 算力目标：外部 grant 携带的目标优先；显式 compute_agent 再覆盖。
    compute_skill = external.get("skill") or "gpu.run"
    compute_aic = (external.get("targetAic")
                   or (delegation or {}).get("targetAic")
                   or file_token.get("targetAic")
                   or os.getenv("ACPS_GPU_AIC", "")).strip()
    compute_agent = str(compute_agent or "").strip()
    if compute_agent:
        from acps_cluster.core import cluster_roster

        compute_aic, compute_skill, reason = resolve_compute_target(
            cluster_roster.load_roster(), compute_agent.strip())
        if reason:
            return {"ok": False, "awaiting": {**NEED_TOKEN, "reason": reason}}
    if not compute_aic:
        return {"ok": False, "awaiting": {**NEED_TOKEN, "reason": "缺目标算力 agent 的 AIC（令牌未携带且未指定 compute_agent）"}}
    # 托管数据集代下（gpu.data.fetch）只在 gpu 目标存在：cpu 目标提前如实拒绝
    if compute_skill != "gpu.run" and _dataset_spec(dataset).get("mode") == "fetch":
        return {"ok": False, "awaiting": {
            **NEED_TOKEN, "reason": "托管数据集代下只在 gpu 目标可用；cpu 目标请改用合成/内嵌数据"}}
    if runtime is None or state is None:
        # 不在 inbound 任务 run 内（如本地直调）：现建 runtime
        runtime = WitRuntime(build_experiment_agent(None), workspace=WORKSPACE,
                             acps=cluster_common_local_config())
        state = AgentState()
    outcome = await experiment_pipeline(
        runtime, state, question=question, method=method, dataset=dataset,
        token=token, compute_aic=compute_aic, compute_skill=compute_skill,
        timeout_seconds=timeout_seconds,
    )
    # 登记结果供 `InboundProductFallbackHooks` 兜底：伙伴模型跑完却忘了 aip_submit 时，
    # 沙箱的真实产物不能只留在盘上（2026-10-18 第 9 轮即因此丢了 exit=0 的实测结果）。
    cluster_common.set_inbound_result(outcome)
    return outcome


def harvest_artifact_contents(products, *, limit: int = 8, max_bytes: int = 64 * 1024) -> dict:
    """从伙伴回传的产物结构里逐字取出小文本文件内容（不让模型转写数字）。

    `gpu.run` 的 `artifacts[]` 带 `content`（≤64KB 的 csv/json/txt 内联回传），但这些内容
    经过报告层就只剩描述了（2026-10-18 第 14 轮：leader 拿不到那 60 个 0/1，无法提交评测）。
    这里做结构化提取：形状随协议漂移时返回空，宁缺勿造。
    """
    out: dict[str, str] = {}

    def walk(node, depth: int = 0) -> None:
        if depth > 6 or len(out) >= limit:
            return
        if isinstance(node, dict):
            name, content = node.get("name"), node.get("content")
            if isinstance(name, str) and isinstance(content, str) and "." in name                     and "encoding" not in node and len(content) <= max_bytes:
                out.setdefault(name.rsplit("/", 1)[-1], content)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)
        elif isinstance(node, str) and "artifacts" in node:
            try:
                walk(json.loads(node), depth + 1)
            except (json.JSONDecodeError, ValueError):
                pass

    walk(products)
    return out


def cluster_common_local_config():
    """本地直连演示用的 AcpsConfig（与 demo 脚本同款，不依赖 .env）。"""
    from wit import AcpsConfig, AgentCertificateRequest, AgentProviderProfile

    port = int(os.getenv(EXP_PORT_ENV, str(EXP_PORT_DEFAULT)))
    return AcpsConfig.custom(
        registry_url="https://localhost:9001",
        ca_url="https://localhost:9003",
        discovery_url="https://localhost:9005",
        public_endpoint=f"https://localhost:{port}/rpc",
        certificate=AgentCertificateRequest(dns=("localhost",)),
        provider=AgentProviderProfile(name="yanshu", organization="Wit", email="e@e.com", country_code="CN"),
        strict_interop=True,
    )


EXPERIMENT_CAPABILITIES = [
    exp_run,
    # 不挂 agent.info 锚点：exp.run 本身公开（设计如此），ACS 因此与平台已批记录
    # （skills=['exp.run']，2026-10-09 版）等价——改动公开能力集会触发"已 APPROVED
    # 记录不能原地改 ACS"的平台硬约束（2026-10-23 部署实测：多一个 agent.info 就
    # 注册拒绝、服务起不来）。
]


def build_experiment_agent(provider) -> WitAgent:
    """装配「研枢-Experiment-Partner」agent：模型、工具与 hooks 的公开构建入口。"""
    ws_enabled = workspace_access_enabled("experiment")
    capabilities = list(EXPERIMENT_CAPABILITIES)
    extra_hooks: list = []
    if ws_enabled:
        capabilities.extend(build_workspace_access_capabilities(WORKSPACE / "tokens"))
        extra_hooks.append(WorkspaceAccessBudgetHooks(WORKSPACE / "tokens"))
    public_skills, has_non_public = capability_visibility(capabilities)
    pep_hooks = build_pep_hooks(
        capabilities=capabilities,
        workspace=WORKSPACE,
        public_skills=public_skills,
        has_non_public_skills=has_non_public,
        # 资源类 agent 不接受"未指名 skill 的无凭据入场"（H1 收口保持）：
        # exp.run 公开但必须按名调用（走 L0 每日限额），未指名请求仍拒。
        allow_unscoped_public_entry=False,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    if os.getenv("ACPS_PEP_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"[experiment] PEP 已启用: {pep_hooks[0].config.describe()}", flush=True)
    instructions = (
        "你是研枢平台的实验员。收到实验任务后调用 exp.run（提供 question/method/数据集）。"
        "**`dataset` 参数只填数据集名与 URL**（约定格式 `<name> | <https-url>`，例如 "
        "`optdigits | https://www.openml.org/data/v1/download/28/optdigits.arff`）；"
        "数据由 gpu 宿主侧代下并只读挂载在 `/data/<name>/`，**不要把 CSV 正文贴进来**"
        "（正文要两次逐字转写，慢网关上会超时）；任务明确要求『代码内合成』时才填 synthetic。"
        "把返回的实验报告原样转达；调用方可用 `compute_agent` 指定算力目标（gpu/cpu，"
        "缺省沿用令牌携带的目标）。返回 awaiting 时如实转达缺项（通常缺委托令牌），"
        "等待 leader 补发，不得在没有令牌时编造实验结果。"
    )
    if ws_enabled:
        instructions += "\n" + WORKSPACE_ACCESS_NOTE
    return WitAgent(
        id="yanshu.experiment-partner",
        display_name="研枢-Experiment-Partner",
        version="1.1.0",  # 与平台已批记录对齐（2026-10-14 换身份）
        description=agent_discovery_description('yanshu.experiment-partner', "实验员：把研究假设转写为实验代码，持委托令牌在 GPU 沙盒执行并产出结构化实验报告。"),
        instructions=instructions,
        model=provider,
        capabilities=apply_to_capabilities(capabilities, agent_id="yanshu.experiment-partner"),
        hooks=[AipDelegationHooks(), BridgeHooks(),
               cluster_common.InboundProductFallbackHooks(), *extra_hooks, *pep_run_hooks],
        # （aip_delegate 持 token 直连 gpu 必需；桥让 exp.run 在 inbound 任务内出站；
        #  兜底提交防"沙箱跑成功但模型没 aip_submit"→ 产物只留在盘上）
        aip_hooks=[AipPartnerGuardHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def _serve_local() -> None:
    port = int(os.getenv(EXP_PORT_ENV, str(EXP_PORT_DEFAULT)))
    runtime = WitRuntime(build_experiment_agent(None), workspace=WORKSPACE,
                         acps=cluster_common_local_config())
    aic = cluster_common.ensure_local_acs(runtime, WORKSPACE)
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[experiment] 本地直连模式 AIC={aic or '?'} endpoint=https://localhost:{port}/rpc → {status.status}")
    await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if args.local:
        await _serve_local()
        return
    await cluster_common.serve_partner(
        build_experiment_agent,
        workspace=WORKSPACE,
        port_env=EXP_PORT_ENV,
        default_port=EXP_PORT_DEFAULT,
        creds_prefix="EXPERIMENT",
    )


if __name__ == "__main__":
    asyncio.run(main())
