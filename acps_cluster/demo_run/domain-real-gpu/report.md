# 域层 × 真 GPU 串联结果

- domain: gpu-repro-demo（章程落真实 D:\桌面\project\AIC智能体互联\my-project\acps_cluster\ws-gpu\acps\domains）

## G1 域内 grant→gpu.run 真训练(1/2)
- 期望: allow+训练
- 实际: awaiting-completion
- 沙盒: run-d5a4a8d7aa
- 审计: allow rule=domain-grant       jti=9fb0e7ac-5

## G2 同枚 grant 第 2 次真训练(2/2)
- 期望: allow+训练
- 实际: awaiting-completion
- 沙盒: run-bf8392f8da
- 审计: allow rule=domain-grant       jti=9fb0e7ac-5

## G3 配额耗尽后的第 3 次
- 期望: rejected(quota)
- 实际: rejected
- 沙盒: None
- 审计: deny  rule=quota-exhausted    jti=9fb0e7ac-5

## G4 域冻结后新 grant
- 期望: rejected(domain-frozen, 验签前)
- 实际: rejected
- 沙盒: None
- 审计: deny  rule=domain-frozen      jti=-

## 完整审计轨迹
- allow rule=domain-grant       jti=9fb0e7ac-5
- allow rule=domain-grant       jti=9fb0e7ac-5
- deny  rule=quota-exhausted    jti=9fb0e7ac-5
- deny  rule=domain-frozen      jti=-