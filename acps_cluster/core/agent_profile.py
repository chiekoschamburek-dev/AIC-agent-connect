"""纯 domain agent 的 public profile 能力模板（不触碰现有已注册 agent）。

发现层按 capability 行过滤：一个 agent 的能力若全部是 domain 且调用方非成员，
所有行都会被丢弃，agent 会整体消失。给这类 agent 加一个 **public 非 domain**
的 `agent.info` 作为可发现锚点，就能做到"能力不可见、agent 仍可见"。

使用边界：
- 只用于**新 agent** 或**所有能力都 domain/非公开**的 agent；
- 现有已注册 agent 不要加：新增 capability 会改变 ACS 投影，可能触发
  `acs_fingerprint_changed_after_submission`，必须重新提交 ACS；
- `public_skills` 只放 public 且非 domain 的技能 id，不要把手域技能列进来。

用法（agent 模块内）：
    from acps_cluster.core.agent_profile import make_agent_info

    AGENT_INFO = make_agent_info(
        agent_id="yanshu.example",
        display_name="示例资源 agent",
        summary="公开档案：只说明身份与职责，不暴露域内能力。",
        public_skills=("example.health",),
    )
    CAPABILITIES = [example_health, AGENT_INFO]
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from wit import capability


def make_agent_info(
    *,
    agent_id: str,
    display_name: str,
    summary: str,
    public_skills: Iterable[str] = (),
) -> Callable[..., Any]:
    """构造一个 public、非 domain 的 `agent.info` capability。

    返回的是被 `@capability` 装饰过的 async handler（与普通能力同形），可直接
    放进 agent 的 capabilities 列表。handler 只返回公开元数据；不读取任何域策略。
    """
    if not str(agent_id or "").strip():
        raise ValueError("agent_id 不能为空")
    if not str(display_name or "").strip():
        raise ValueError("display_name 不能为空")
    skill_ids = tuple(sorted({
        str(skill).strip() for skill in public_skills if str(skill).strip()
    }))

    @capability(
        id="agent.info",
        description=(
            "公开档案：返回本 agent 的身份、职责与可公开调用的能力清单；"
            "不包含域内能力或任何受限资源。"
        ),
        public=True,
        tags=["agent", "profile", "public"],
        examples=["这个 agent 能做什么？", "介绍一下你的公开能力"],
    )
    async def agent_info() -> dict[str, Any]:
        return {
            "agentId": str(agent_id).strip(),
            "displayName": str(display_name).strip(),
            "summary": str(summary).strip(),
            "publicSkills": list(skill_ids),
            "profileVisibility": "public",
        }

    return agent_info
