"""兼容转发 shim：把队友 guard 的模块路径接到整合后的目录布局。

队友的 ACPS Event Channel Guard 以**纯拷贝**方式放在 ``acps_cluster/guard/`` 下，
其源码、测试与 CLI 都使用 ``acps_cluster.covert_event_guard`` 这一模块名。
为了让队友代码保持**零改动**（测试与 README 里的命令都能原样运行），本文件只做一行
转发，不含任何业务逻辑。

- 新代码请直接 ``from acps_cluster.guard import covert_event_guard``；
- 本 shim 仅为兼容队友原有路径而存在，不得在此添加逻辑。
"""

from acps_cluster.guard.covert_event_guard import *  # noqa: F401,F403
from acps_cluster.guard.covert_event_guard import main  # noqa: F401

if __name__ == "__main__":  # 保留 `python -m acps_cluster.covert_event_guard` 原命令
    raise SystemExit(main())
