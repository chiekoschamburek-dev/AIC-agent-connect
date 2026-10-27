"""问迹对外的 ACPs 能力声明（AIP Skill 投影 + 模型工具）。

每个 ``@capability`` 同时产出两种投影：

- **模型工具**：WIT Agent 在 Direct 任务中按指令调用（工具名会把 ``.`` 规范化为 ``_``）；
- **ACPs Skill**：``acps_skill=True`` 会进入 ACS（智能体能力描述），
  供梧桐平台与其它智能体的 Discovery 检索、Leader 规划。

命名约定：
- ``wenji.ask``        综合问数/问答（内核自动路由，推荐入口）
- ``wenji.data_query`` 结构化问数（NL2SQL 专线：SQL + 数据行）
- ``wenji.doc_qa``     文档知识问答（RAG 专线：带引用答案）
- ``wenji.schema``     业务库 schema 摘要（供其它智能体规划）
"""

from __future__ import annotations

import sys
from typing import Any

from wit import capability

from . import bridge
from .task_access import CURRENT_TASK_SESSION

def _debug(tag: str, **kv: Any) -> None:
    import os

    if os.environ.get("WENJI_DEBUG_SESSION"):
        print(f"[wenji] {tag} " + " ".join(f"{k}={v}" for k, v in kv.items()), file=sys.stderr)



@capability(
    id="wenji.ask",
    description=(
        "综合问数/问答：问题可同时涉及业务数据库与知识库文档，自动规划、"
        "执行多步查询/检索并给出带引用、可追溯的答案。问数/问答类任务优先调用本能力。"
    ),
    acps_skill=True,
    public=True,
    input_modes=["text/plain"],
    tags=["问数", "问答", "跨源", "可解释"],
    examples=[
        "查询销量最高的前十个曲目，并说明统计口径",
        "销售冠军是谁？总结他的成功方法论（结合数据库与文档）",
    ],
)
async def ask(question: str) -> dict[str, Any]:
    """综合入口：原样传入用户问题，内核自动路由；会话按 AIP 任务隔离（澄清/多轮在任务内闭环）。"""
    session_id = CURRENT_TASK_SESSION.get()
    _debug("ask", session=session_id, q=question[:30])
    return await bridge.run_in_thread(bridge.answer_question, question, session_id)


@capability(
    id="wenji.data_query",
    description=(
        "结构化问数（NL2SQL）：把自然语言问题转为只读 SQL 并在业务库执行，"
        "返回 SQL、数据行、行数与图表建议。适用于销量、客户、曲目、发票、员工等数据库问题。"
    ),
    acps_skill=True,
    public=True,
    input_modes=["text/plain"],
    tags=["NL2SQL", "问数", "数据库"],
    examples=["统计 2010 年每个国家的销售额，按金额降序"],
)
async def data_query(question: str) -> dict[str, Any]:
    _debug("data_query", session=CURRENT_TASK_SESSION.get(), q=question[:30])
    return await bridge.run_in_thread(bridge.data_query, question)


@capability(
    id="wenji.doc_qa",
    description=(
        "知识库文档问答（RAG）：检索企业文档（手册/制度/销售总结等）并生成带"
        "文档名与页码引用的答案；无可靠证据时明确说明缺少上下文。"
    ),
    acps_skill=True,
    public=True,
    input_modes=["text/plain"],
    tags=["RAG", "文档问答", "引用"],
    examples=["员工年假有多少天？", "客服 SOP 中关于退货的时限要求是什么？"],
)
async def doc_qa(question: str, doc_filter: str | None = None) -> dict[str, Any]:
    """``doc_filter`` 可选：限定单个文档 ID（不含扩展名）。"""
    _debug("doc_qa", session=CURRENT_TASK_SESSION.get(), q=question[:30])
    return await bridge.run_in_thread(bridge.doc_qa, question, doc_filter)


@capability(
    id="wenji.formula_eval",
    description=(
        "文档公式计算：从知识库登记的公式（如员工手册的销售提成办法）中解析目标公式，"
        "自动绑定参数（数据库/文档/用户输入三通道），LaTeX→SymPy 双通道复算并给出步骤与引用。"
        "例：“Jane 的销售提成是多少？”“按销售提成公式估算销售额 15 万时的提成。”"
    ),
    acps_skill=True,
    public=True,
    input_modes=["text/plain"],
    tags=["公式计算", "文档抽取", "SymPy"],
    examples=["Jane 的销售提成是多少？", "销售额 150000 时按销售提成公式算提成"],
)
async def formula_eval(question: str) -> dict[str, Any]:
    _debug("formula_eval", session=CURRENT_TASK_SESSION.get(), q=question[:30])
    return await bridge.run_in_thread(bridge.formula_eval, question)


@capability(
    id="wenji.schema",
    description="返回业务数据库的表清单与字段摘要（最多 30 张表），供其它智能体规划查询。",
    acps_skill=True,
    public=True,
    input_modes=["text/plain"],
    tags=["Schema", "元数据"],
)
async def schema() -> dict[str, Any]:
    _debug("schema", session=CURRENT_TASK_SESSION.get())
    return await bridge.run_in_thread(bridge.schema_overview)


CAPABILITIES = [ask, data_query, doc_qa, formula_eval, schema]
