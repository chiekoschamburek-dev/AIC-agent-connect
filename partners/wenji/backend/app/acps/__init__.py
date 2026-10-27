"""ACPs / AIP 智能体互联适配层（问迹）。

本包把仓库内既有的问数/问答引擎（AgentKernel 及 NL2SQL / RAG 流水线）封装为
符合 AIP 国标（GB/Z 185）的 ACPs 智能体：

    WIT 框架（wit.acps）  ←→  capabilities.py（能力声明与桥接）
         │
         ├─ 注册 / AIC / mTLS 证书        （runtime.py + 梧桐平台）
         ├─ AIP Direct rpc 服务端         （WitRuntime.serve_direct_partner）
         ├─ Leader 侧发现与委派           （AipDelegationHooks，见 agent.py）
         └─ AMP 六类可观测日志            （WitRuntime 自动 journal）

对外入口：
- ``python -m app.acps.cli show-acs``  生成/查看 ACS 草稿（注册平台上传用）
- ``python -m app.acps.cli register``  推进一次可信注册（登录 → 草稿 → 提交 → 审核）
- ``python -m app.acps.cli serve``     注册激活后启动 AIP Direct Partner 服务
- ``python -m app.acps.cli check``     本地校验（声明、ACS、能力自检）
"""

from .settings import AcpsSettings

__all__ = ["AcpsSettings"]
