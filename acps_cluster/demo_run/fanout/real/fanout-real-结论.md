# 中转 vs 委托隧道：真实 LLM 对比（留档）

- 时间：2026-10-02T11:20:42.880654+00:00  模型：LongCat-2.0
- 与脚本化版本（demo_fanout.py，确定性）互补；本版所有模型位为真实 LLM。

| 指标 | 中转 | 委托隧道 |
|---|---|---|
| 墙钟时延 (s) | 133.1 | 76.8 |
| 实测并行度 | 1 | 3 |
| leader LLM 回合（实测） | 7 | 2 |
| leader 峰值上下文 (字符) | 9579 | 2358 |
| 材料（字节） | 最终回答 6375 B，含材料 3/3 | 直达消费方 5775 B |
| 消费方回合 | —（材料滞留 leader） | 1 |

中转模式材料经 leader 上下文搬运（材料经 leader 上下文（工具结果+最终回答逐字保留））；隧道模式 材料不经 leader；签发一次（真实工具闭环，JWS 入 vault 不过模型）+ 并发直连 + 消费方汇总。
目标侧准入审计：
{"real-audit-scout.jsonl": ["delegation-token"], "real-audit-synthesis.jsonl": ["delegation-token"], "real-audit-writer.jsonl": ["delegation-token"]}