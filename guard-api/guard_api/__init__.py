"""ACPS Event Channel Guard HTTP API(只读旁路)。

- 独立运行:``python -m guard_api --db /path/to/aip-tasks.sqlite3``
- 进程内挂载(supervisor):``from guard_api.mount import start_guard_api``
"""

from guard_api._version import __version__
from guard_api.app import create_app
from guard_api.mount import DEFAULT_HOST, DEFAULT_PORT, start_guard_api, stop_guard_api
from guard_api.report import Alert, GuardReport, scan

__all__ = [
    "Alert",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "GuardReport",
    "__version__",
    "create_app",
    "scan",
    "start_guard_api",
    "stop_guard_api",
]
