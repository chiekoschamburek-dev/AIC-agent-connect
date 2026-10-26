"""研枢-KB-Partner：知识库 agent（资源 agent 化第四实例——数据不动、计算动、字段分级）。

底座：SQLite 单文件库（ws-kb/kb.sqlite3）+ 受限查询 DSL（filter 翻页/排序、compute 聚合、
get 取单条），无任意代码执行面——比沙盒更轻更安全；
防泄露粒度三档叠加：资产级（PEP 准入）+ 行级（where 过滤）+ **字段级**（角色脱敏：
open 目录 / members 摘要 / curator 全文与内部笔记）。

能力：
- kb.schema  public（橱窗）：字段/类型/档位/索引档/规则——只有结构没有内容
- kb.query   非公开（保险柜）：search 全文检索（按档位分索引）/ filter 取行（脱敏+行上限+翻页）/
             compute 聚合（行不下发）/ get 取单条
- kb.ingest  非公开（策展）：受控写入（校验+溯源+幂等去重，每次写入即审计事件）

身份：ws-kb（平台注册 AIC …VTF97R.D86STV.UBL99U.0BOQ，端点 8599，v1.0.1；
KB0001.AGENT01.RUN01.012I 为早前本地桩身份，已废弃）。
端口：默认 8599（可用 ACPS_KB_PORT 覆盖；与注册端点、firewalld 放行口径一致）。
库路径：ACPS_KB_PATH 可覆盖（演示与单测指向独立库，不碰现网数据）。
运行：python acps_cluster/agents/partner_kb.py --local
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from wit import (  # noqa: E402
    AcpsDirect,
    WitAgent,
    capability,
)

from acps_cluster.core import cluster_common  # noqa: E402
from acps_cluster.core.discovery_metadata import (  # noqa: E402
    agent_discovery_description,
    apply_to_capabilities,
)
from acps_cluster.runtime import kb_runner  # noqa: E402
from wit.hooks.acps.amp import AmpAccessLoggingHooks  # noqa: E402
from acps_cluster.core.aip_guard import AipPartnerGuardHooks  # noqa: E402
from acps_cluster.delegation import build_pep_hooks, capability_visibility, split_pep_hooks  # noqa: E402
from acps_cluster.delegation.bridge import BridgeHooks  # noqa: E402
from wit.hooks.acps.delegation import AipDelegationHooks  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[1] / "ws-kb"
KB_PORT_ENV = "ACPS_KB_PORT"
KB_PORT_DEFAULT = 8599  # 与注册端点/现网监听一致（原 8465 为换端口前遗留，2026-10-15 审查 E）
KB_WORKSPACE_ENV = "ACPS_KB_WORKSPACE"


def kb_workspace() -> Path:
    """工作区可指向演示副本。

    字段档位是从**本机已验证章程**派生的，而角色表原先固定读 `ws-kb/acps/domains/`——
    于是 K 系列跑的其实是现网章程（tmp 里那份演示章程根本没被读）。隔离不彻底，
    改现网章程会静默改变演示结果。
    """
    env = os.getenv(KB_WORKSPACE_ENV)
    return Path(env) if env else WORKSPACE


def _role_for_peer() -> str:
    """对端 AIC → 章程角色;无对端/非成员 → "?"(fail-closed 到最严档)。

    身份由 cluster_common.PeerIdentityHooks 从 mTLS 对端捕获，不在本模块另存一份。
    """
    peer = cluster_common.current_peer_aic()
    return _role_table_from_domains().get(peer or "", "?")


def _role_table_from_domains() -> dict[str, str]:
    """从本机域章程（已验证投影）解析 aic→role，供字段级脱敏。

    生产形态：`<工作区>/acps/domains/` 放本库章程（双签验证过的策略投影）。
    角色不再有调用方入参通道（2026-10-06 审查：伪造 role_table 即提权）。
    """
    from acps_cluster.delegation.domain_manifest import load_domain_policies

    workspace = kb_workspace()
    policies = load_domain_policies(workspace / "acps" / "domains",
                                    trust_bundle=workspace / "acps" / "trust-bundle.pem")
    table: dict[str, str] = {}
    for policy in policies.values():
        table.update(policy.members)
    return table


@capability(
    id="kb.schema",
    description="知识库结构卡：字段/档位（open目录/members摘要/curator全文）/查询规则/限额。只有结构，没有内容。",
    public=True,
    tags=["kb", "schema"],
)
async def kb_schema() -> dict:
    return {**kb_runner.schema_card(), "records": kb_runner.record_count()}


@capability(
    id="kb.query",
    description=(
        "受限 DSL 查询（数据不动、计算动）。mode='search'：全文检索（trigram 分词，中英文都可，"
        "bm25 排序，返回有界摘录 ≤320 字）——**按档位分索引**，你看不见的字段根本不进你这条"
        "检索通道，所以命中数/得分/摘录都无从透出越权内容；mode='filter'：where 条件数组 + "
        "fields 投影 + limit(≤25) + offset 翻页 + sort 排序（sort={'field':'year','dir':'desc'}），"
        "返回脱敏后的行（受限字段按角色丢弃并注明，hasMore 表示还有下一页）；mode='compute'："
        "groupby + aggs（count/min/max/avg/sum 白名单），聚合在本机执行，原始行不下发；"
        "mode='get'：按 id 取单条。检索还能叠加 where 缩小范围。"
        "whereOps/aggFns/索引档见 kb.schema。高成本能力：查询次数受配额约束。"
    ),
    acps_skill={"visibility": "domain", "domains": ["yanshu-kb"]},
    tags=["kb", "query"],
)
async def kb_query(query_spec: dict) -> dict:
    """角色只从 mTLS 对端 AIC(PEP 已验证)+ 本机已验证章程投影派生——
    2026-10-06 审查修复:caller/role_table 不再是调用方入参(否则伪造
    role_table={"我的AIC":"provider"} 即拿 curator 档)。"""
    role = _role_for_peer()
    return {**kb_runner.query(query_spec, role=role), "callerRole": role}


@capability(
    id="kb.ingest",
    description="受控写入（策展）：records 数组按 schema 校验，自动赋 id 并附溯源（提交者/时间）；每次写入都是审计事件。",
    acps_skill={"visibility": "domain", "domains": ["yanshu-kb"]},
    tags=["kb", "ingest"],
)
async def kb_ingest(records: list, submitter: str = "?") -> dict:
    who, verified = cluster_common.resolve_submitter(submitter)  # 溯源以 mTLS 对端为准
    return {**kb_runner.ingest(records, submitter=who), "submitter": who,
            "submitterVerified": verified}


KB_CAPABILITIES = [kb_schema, kb_query, kb_ingest]


def build_kb_agent(provider) -> WitAgent:
    public_skills, has_non_public = capability_visibility(KB_CAPABILITIES)
    pep_hooks = build_pep_hooks(
        capabilities=KB_CAPABILITIES,
        workspace=kb_workspace(), public_skills=public_skills, has_non_public_skills=has_non_public,
    )
    pep_run_hooks, pep_aip_hooks = split_pep_hooks(pep_hooks)
    return WitAgent(
        id="yanshu.kb-partner",
        display_name="研枢-KB-Partner",
        version="1.0.1",  # 与平台已批记录对齐（2026-10-14 换身份）
        description=agent_discovery_description('yanshu.kb-partner', "知识库：数据不动计算动——受限查询下推 + 字段级脱敏（open/members/curator 三档）。"),
        instructions=(
            "你是知识库接口员。结构查询用 kb.schema；**按主题找资料先用 kb.query 的 search 模式**"
            "（query 给自然语言词组，中英文都行，返回按相关度排序的有界摘录），需要精确条件/翻页/"
            "排序用 filter 模式（where + fields + limit + offset + sort），统计任务用 compute 模式"
            "（groupby + aggs），已知编号用 mode='get' 取单条。把返回的 hits/rows/table 原样转达并"
            "注明 redactedFields；matched 大于 returned 时如实说明还有多少条未取、可用 offset 继续翻。"
            "写入任务调用 kb.ingest。受限字段被脱敏是设计行为，检索面也按档位分索引，"
            "如实转达，不得声称能取回越权内容。"
        ),
        model=provider,
        capabilities=apply_to_capabilities(KB_CAPABILITIES, agent_id="yanshu.kb-partner"),
        hooks=[AipDelegationHooks(), BridgeHooks(), *pep_run_hooks],
        aip_hooks=[AipPartnerGuardHooks(), cluster_common.PeerIdentityHooks(), *pep_aip_hooks],
        amp_hooks=[AmpAccessLoggingHooks()],
        protocols=[AcpsDirect()],
    )


async def _serve_local() -> None:
    from wit import WitRuntime

    workspace = kb_workspace()
    kb_runner.open_store()  # 建库（不注入合成数据；真实语料用 kb_admin.py import 灌）
    port = int(os.getenv(KB_PORT_ENV, str(KB_PORT_DEFAULT)))
    runtime = WitRuntime(build_kb_agent(None), workspace=workspace, acps=cluster_common.build_local_acps_config(port))
    cluster_common.ensure_local_acs(runtime, workspace)
    status = await runtime.serve_direct_partner(host="127.0.0.1", port=port, amp=False)
    print(f"[kb] 本地直连模式 endpoint=https://localhost:{port}/rpc → {status.status}")
    await asyncio.sleep(int(os.getenv("ACPS_PARTNER_TTL", "864000")))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    if args.local:
        await _serve_local()
        return
    await cluster_common.serve_partner(
        build_kb_agent,
        workspace=kb_workspace(),
        port_env=KB_PORT_ENV,
        default_port=KB_PORT_DEFAULT,
        creds_prefix="KB",
    )


if __name__ == "__main__":
    asyncio.run(main())
