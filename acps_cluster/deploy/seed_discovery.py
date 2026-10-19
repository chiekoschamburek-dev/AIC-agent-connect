"""预热 supervisor 的 discovery 缓存：把本集群各伙伴的 ACS 写进缓存。

（2026-09-27 起 arXiv 伙伴下线，不再预热；检索职责由 scout 承担。）

为什么需要：wt 平台的发现接口比 wit 2.1.0 的客户端新——它已移除 `onlyAvailable` 字段
（改用 extFilter.alive），而 wit 客户端恰好发送该字段，导致 `acps_discover` 恒返回
400 FilterInvalid。没有发现结果 → 委派时解析不到伙伴端点 → 报
"Partner has no discovered Direct JSON-RPC endpoint"。
（2026-10-14 实测平台已 tolerate `onlyAvailable`（200），但缓存预热保留为兜底。）

策略：**优先读各伙伴本地的 ws-<key>/acps/acs.final.json**（权威终版 ACS，含 aic/endPoints，
无需网络，但必须是覆盖表现役集合里的身份）；缺失/非现役时回退平台公开的 ACS 接口（无需登录）；
外部成员（`EXTERNAL_OFFLINE_KEYS`，如队友的 designer 节点）在平台不可达时再用
`deploy/external_members_acs.json` 里的脱敏副本兜底——平台优先保现行，离线不缺件。

执行位置与安全边界（2026-10-14 审查补）：
- 预热的权威执行位置是**服务器**（读服务器的 ws-*/acs.final.json，均为现役 BUPT 身份）；
- 本地终稿只有 aic ∈ `registered_acs_overlay.json` 的**现役集合**才放行（形态前缀过滤
  防不住"同前缀的换身份前旧稿"，2026-10-14 审查 B）；不放行即告警并回落平台兜底——
  开发桩/旧稿**不得**进发现缓存；
- 同步红线：任何代码同步都不得覆盖服务器 `ws-*/acps`（证书/终稿/密钥只在服务器原地）。
"""

import json
import os
import sys
from pathlib import Path

CLUSTER = Path(__file__).resolve().parents[1]
WS = CLUSTER / "ws-supervisor"

# key（= 工作区名 ws-<key>）→ 展示名，仅用于日志
MEMBERS = {
    "scout": "Scout",
    "frontier": "Frontier",
    "synthesis": "Synthesis",
    "contradiction": "Contradiction",
    "writer": "Writer",
    "critic": "Critic",
    # 实验员（2026-10-02 平台注册通过后上线 8461）；不预热它的话 supervisor 委派会解析不到端点
    "experiment": "Experiment",
    # 知识库（2026-10-14 换身份至 1.0.1/D86STV 后补入；缺它 leader 解析不到 kb 端点）
    "kb": "KB",
    # 队友 gjw 的"研究方案设计"节点（2026-10-09 手写登记的外部成员，8590 同机）
    "designer": "Research Designer",
}

# 本地 acs.final.json 缺失（或为开发桩）时的兜底 AIC（平台公开接口可查）。
# 2026-10-14 全量刷新为现役身份（旧号已全部 403 not active）。
FALLBACK_AICS = {
    "scout": "1.2.156.3088.1.BUPT.VTF97R.GMT5S6.4SZM68.0LC5",
    "frontier": "1.2.156.3088.1.BUPT.VTF97R.V1E6AY.3YME2N.01K2",
    "synthesis": "1.2.156.3088.1.BUPT.VTF97R.9HADYH.RLVWEP.07Y5",
    "contradiction": "1.2.156.3088.1.BUPT.VTF97R.Z7TJ57.ORGGRY.0ZFO",
    "writer": "1.2.156.3088.1.BUPT.VTF97R.1OGDWX.U9K14M.0CDI",
    "critic": "1.2.156.3088.1.BUPT.VTF97R.M2NJ2G.A1BIB2.195J",
    "experiment": "1.2.156.3088.1.BUPT.VTF97R.WN4C3S.0C0YA7.0J4R",
    "kb": "1.2.156.3088.1.BUPT.VTF97R.D86STV.UBL99U.0BOQ",
    # 队友节点（外部成员，不在本集群 overlay 白名单内）：本地终稿会被"现役集合"闸门拦下
    # → 走平台公开接口取最新；平台不可达时用库内脱敏副本兜底（见 offline_acs）。
    "designer": "1.2.156.3088.1.BUPT.145MB5.3IXQIN.RXO3OT.1AQ9",
}

# 现役身份白名单来源：注册文本覆盖表（与 discovery_metadata 的投影同源）
_OVERLAY_PATH = CLUSTER / "registered_acs_overlay.json"

# 外部成员（平台在册但不由本集群注册/持有终稿）：平台不可达时允许用库内脱敏副本寻址。
EXTERNAL_OFFLINE_KEYS = frozenset({"designer"})
_EXTERNAL_PATH = Path(__file__).with_name("external_members_acs.json")


def offline_acs(key: str, exc: BaseException) -> tuple[dict | None, str]:
    """平台兜底失败后的离线兜底——只对外部成员启用（本集群成员有本地终稿，不缺这条）。

    AIC 与 FALLBACK_AICS 不一致即判定副本过期、拒绝使用：宁缺不造，
    免得把换身份前的端点写进发现缓存（2026-10-15 审查：平台叶子证书过期那天，
    designer 的 GET 失败被静默吞掉，而它的 AIC/端点其实一直在库里）。
    """
    if key not in EXTERNAL_OFFLINE_KEYS:
        return None, ""
    try:
        data = json.loads(_EXTERNAL_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as read_exc:
        print(f"  [seed] {key}: 库内副本不可读 {type(read_exc).__name__}: {read_exc}")
        return None, ""
    acs = (data.get("members") or {}).get(key)
    if not isinstance(acs, dict) or not acs.get("aic"):
        print(f"  [seed] {key}: 库内副本里没有该成员条目")
        return None, ""
    if str(acs["aic"]) != FALLBACK_AICS.get(key):
        print(f"  [seed] {key}: 库内副本 aic={acs['aic']} 与 FALLBACK_AICS 不一致"
              f" → 判定过期，不用（请重新导出）")
        return None, ""
    return acs, f"库内脱敏副本（平台不可达:{type(exc).__name__}）"


def _registered_aics() -> set[str] | None:
    """覆盖表里的现役 AIC 集合；读不到返回 None（调用方告警并全部走平台兜底）。"""
    try:
        data = json.loads(_OVERLAY_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    out = {str((v or {}).get("aic") or "") for v in (data.get("agents") or {}).values()}
    out.discard("")
    return out or None


def _local_acs(key: str, registered: set[str] | None) -> dict | None:
    path = CLUSTER / f"ws-{key}" / "acps" / "acs.final.json"
    if not path.is_file():
        return None
    try:
        acs = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    aic = str(acs.get("aic") or "")
    if registered is None:
        print(f"  [seed] {key}: 覆盖表不可读,校验不了本地终稿身份 → 跳过,改用平台兜底")
        return None
    if aic not in registered:
        # 形态前缀过滤防不住同前缀的换身份前旧稿,现役集合白名单才可靠(2026-10-14 审查 B)
        print(f"  [seed] {key}: 本地终稿 aic={aic or '?'} 不在覆盖表现役集合,跳过,改用平台兜底")
        return None
    return acs


def main() -> int:
    sys.path.insert(0, "/opt/wit/my-project")
    from wit.acps.discovery import cache_discovered_agent, normalize_discovered_agent

    registry = os.getenv("ACPS_REGISTRY_URL", "https://wt.ioa.pub/registry-server")
    cache_dir = WS / "acps" / "discovered"
    ok = 0
    missing: list[str] = []
    client = None
    registered = _registered_aics()
    if registered is None:
        print(f"  [seed] ⚠ {_OVERLAY_PATH.name} 不可读——本地终稿全部跳过,改走平台兜底")
    for i, (key, label) in enumerate(MEMBERS.items(), start=1):
        acs = _local_acs(key, registered)
        source = "本地 acs.final.json"
        if not acs:
            try:
                import httpx

                client = client or httpx.Client(timeout=20)
                aic = FALLBACK_AICS[key]
                r = client.get(f"{registry}/acps-atr-v2/acs/{aic}")
                r.raise_for_status()
                acs = r.json()
                source = "平台公开接口"
            except Exception as exc:  # noqa: BLE001
                acs, source = offline_acs(key, exc)
                if acs is None:
                    print(f"  {label:14s} -> 失败 {type(exc).__name__}: {str(exc)[:70]}")
                    missing.append(key)
                    continue
                print(f"  {label:14s} -> 平台不可达({type(exc).__name__})"
                      f"，改用库内脱敏副本")
        try:
            summary = normalize_discovered_agent(acs, ranking=i)
            path = cache_discovered_agent(cache_dir=cache_dir, summary=summary, raw_acs=acs)
            print(f"  {label:14s} -> {path.name[:14]}…  {acs.get('name')}  （{source}）")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:14s} -> 归一化失败 {type(exc).__name__}: {str(exc)[:70]}")
            missing.append(key)
    if client:
        client.close()
    print(f"缓存预热：{ok}/{len(MEMBERS)}")
    if missing:
        # rc≠0 让"9 项里掉了 1 项"不再伪装成全量成功（2026-10-15 审查：
        # 原先 `return 0 if ok else 1`，只有一条成功就报完成，逐条输出又被 DEVNULL 丢弃）
        print(f"  ⚠ 缺 {len(missing)} 项：{', '.join(missing)}"
              f"（委派这些伙伴会解析不到端点）")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
