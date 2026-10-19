实验已完成（runId: 9152bf6814，退出码 0，耗时 9.9 秒）。以下为原样转达的实验报告：

---

## 实验报告：ENTR 假设复现（渐进分辨率 vs 固定 32x32）

**summary**: 假设被证伪：固定32x32训练在准确率(1.0 vs 0.4804)和每epoch耗时(0.003s vs 0.07s)上均优于渐进式低分辨率训练，且两臂峰值显存相同。

**metrics**:

| 臂 | final_val_acc | total_train_s | per_epoch_s | peak_mem_mb |
|---|---|---|---|---|
| progressive（前6 epoch 8x8 → 后6 epoch 32x32） | 0.4804 | 0.84 | 0.07 | 96.4 |
| fixed32（全程 32x32） | 1.0 | 0.03 | 0.003 | 96.4 |

**findings**:
1. B臂（fixed32）最终验证准确率1.0，显著高于A臂（progressive）的0.4804，差距达0.5196。
2. B臂每epoch平均耗时0.003秒，远低于A臂的0.07秒，效率约为A臂的23倍。
3. 两臂峰值显存完全相同（96.4 MB），说明分辨率渐进策略未带来显存优势。
4. A臂耗时异常偏高，可能源于前6个epoch中F.interpolate在CPU上对410张图逐epoch做最近邻下采样的额外开销。
5. 实验结果与ENTR假设预期方向相反：初期低分辨率训练既未提升准确率，也未提升效率。

**caveats**:
1. 实验仅运行12个epoch，训练轮次较少，结论的泛化性需更长训练验证。
2. A臂耗时偏高可能受CPU端数据预处理（F.interpolate）影响，而非模型计算本身，需区分数据管线开销与训练开销。
3. 两臂峰值显存相同可能因batch size或模型结构未随分辨率调整，不代表渐进策略在更大规模下的显存表现。
4. 仅单一数据集/任务，结论是否适用于其他视觉任务未知。

**verdict**: `refuted`（ENTR 假设在本实验设置下未获支持）

---

**stdout 原始输出**:
```json
{"hypothesis": "ENTR: 初期低分辨率训练更高效", "results": [{"arm": "progressive", "final_val_acc": 0.4804, "total_train_s": 0.84, "per_epoch_s": 0.07, "peak_mem_mb": 96.4}, {"arm": "fixed32", "final_val_acc": 1.0, "total_train_s": 0.03, "per_epoch_s": 0.003, "peak_mem_mb": 96.4}]}
```

**stderr**: （空）

**产物**: `result.json`（382 字节，内容与上述 JSON 一致）

**备注**: 沙盒执行时对参考实现做了两处非设计性调整（已在返回的 note 中说明）：去掉了 `output/` 子目录创建、结果直接写入当前目录 `result.json`，并在 stdout 末行打印 `RESULT-OK` 标记。实验设计（两臂对照、12 epoch、统一 32x32 验证、指标定义）原样保留。
