# 两机构域合作演示结果（domain=yanshu-edge-demo）

- 机构 A（算力提供方）: edge-leader（1.2.156.3088.1.1.EDGE01.MGTLEAD1.RUN01.0QGJ）+ gpu/cpu 两块资源

- 机构 B（研枢集群）: supervisor（1.2.156.3088.1.1.ZV8ZLB.RJ5XH2.GOA7Q6.1672）+ experiment（consumer）

## S1 域内 grant→gpu.run 真训练
- 期望: allow+训练
- 实际: awaiting-completion
- 沙盒: run-fef80864e0
- 审计: allow domain-grant 

## S2 同枚 grant→cpu.run 真执行
- 期望: allow+执行
- 实际: awaiting-completion
- 沙盒: run-19e0f5df9f
- 审计: allow domain-grant 

## S3 gpu 第 2 次（配额 2/2）
- 期望: allow+训练
- 实际: awaiting-completion
- 沙盒: run-1dac3f6b2b
- 审计: allow domain-grant 

## S4 gpu 第 3 次（超配额）
- 期望: rejected(quota-exhausted)
- 实际: rejected
- 沙盒: None
- 审计: deny  quota-exhausted acps_max_uses=2 exhausted

## S5 域冻结后新 grant
- 期望: rejected(domain-frozen, 验签前)
- 实际: rejected
- 沙盒: None
- 审计: deny  domain-frozen domain yanshu-edge-demo frozen at revision 2 (pre-signature)

## gpu 侧完整审计
- allow domain-grant 
- allow domain-grant 
- deny  quota-exhausted acps_max_uses=2 exhausted
- deny  domain-frozen domain yanshu-edge-demo frozen at revision 2 (pre-signature)
- allow domain-grant 
- allow domain-grant 
- deny  quota-exhausted acps_max_uses=2 exhausted
- deny  domain-frozen domain yanshu-edge-demo frozen at revision 2 (pre-signature)
- allow domain-grant 
- allow domain-grant 
- deny  quota-exhausted acps_max_uses=2 exhausted
- deny  domain-frozen domain yanshu-edge-demo frozen at revision 2 (pre-signature)

## cpu 侧完整审计
- allow domain-grant 
- allow domain-grant 
- allow domain-grant 

诚实口径：两机构当前同源 dev CA（密码学同信任域），机构边界为行政分离；
B 侧集群现阶段本地模拟，上平台后整栈迁移即可成为真正的跨机器双边域。