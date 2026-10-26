# 测试运行须知(2026-10-06 审查)

1. **不要在服务器/生产机上跑全量测试**。部分测试有真实副作用:
   - `test_p0_security.py::test_取消任务后无孤儿容器` 会**真实启动 Docker 容器**(无 Docker 才 skip);
   - `test_p0_security.py` 的域签名用例会**读取真实 ws-supervisor/ws-gpu/ws-critic 私钥**做签名
     (签名验签必须真实信任链才有意义)。
2. 生产机上只跑无副作用的子集:
   `python -m unittest acps_cluster.tests.test_delegation acps_cluster.tests.test_domain acps_cluster.tests.test_domain_hardening acps_cluster.tests.test_visibility acps_cluster.tests.test_p0_security.ReviewGateTest acps_cluster.tests.test_p0_security.TokenFileTest`
   （`test_domain_hardening` 覆盖章程修订链/父 grant 锚点 PoC，`test_visibility` 覆盖
   能力可见性、`agent.info` 模板与每个 agent 的公开入口守门；均使用临时目录。）
3. 全量测试请在本机(有 Docker、密钥属于本地 dev CA)执行。
