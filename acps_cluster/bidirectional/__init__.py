"""双向互调（bidirectional）验证与取证工具。

比赛评分项「跨智能体双向通信」要的不是"我们调用过别人"，而是**两个方向都有可
对账的平台任务**：本机作为 Leader 调用对端（出站），以及对端作为 Leader 调用本机
（入站）。本包只做三件事，不碰 PEP / 委派 / 运行时语义：

1. `ledger`：从本机 `ws-*/acps/aip-tasks.sqlite3` 的 `aip_tasks` / `aip_task_events`
   **只读**提取入站与出站两个方向的任务回执（taskId、双方 AIC、终态、时间、产物摘要）；
2. `probe`：以本机工作区的 mTLS 身份**真实发起**一次 AIP 任务（出站方向），
   支持 `commandParams={"skillId": ..., "args": ...}` 技能指名，产出探针回执；
3. `evidence`：把两个方向的回执合并成证据包，做方向对称性自检，输出 JSON + Markdown
   + SHA256SUMS，供答辩材料/佐证材料直接引用。

设计边界（写进文档，避免被读成"更多"）：

- 只读：不写任务库、不改任务状态、不签发令牌；`complete` 只在探针自己发起的任务
  `awaiting-completion` 时调用（与手工点"确认产出"等价）；
- 证据包只证明"本机侧两条 AIP 任务的双方身份、方向、终态与产物"，平台后台截图
  仍需人工截取；任何一方终态不是 `completed` 时判定为 `incomplete`，不凑数。

用法（仓库根目录）：

    python -m acps_cluster.bidirectional doctor --workspace acps_cluster/ws-supervisor
    python -m acps_cluster.bidirectional probe  --workspace acps_cluster/ws-supervisor \
        --peer-aic <对端 AIC> --skill research.orchestrate --args '{"task":"..."}'
    python -m acps_cluster.bidirectional pack   --workspace acps_cluster/ws-supervisor \
        --peer-aic <对端 AIC> --out-dir artifacts/bidirectional/2026-11-17
"""

from __future__ import annotations

__all__ = ["models", "ledger", "probe", "evidence", "identity", "skill_entry"]