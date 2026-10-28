"""把 guard API 挂进一个已经在跑的 asyncio 事件循环(supervisor 进程内模式)。

与 AIP 8444 服务同模型:supervisor 进程的 loop 里再起一个 uvicorn.Server,
用 ``asyncio.create_task`` 驱动,与宿主同生共死。要点:

- **不接管信号**:覆盖 ``install_signal_handlers`` 为空操作,否则 uvicorn 会在
  Windows/主线程上改写 SIGINT 处理,破坏宿主进程(supervisor)的 Ctrl+C 语义;
- 宿主退出时先 ``stop_guard_api()``,再停自己的服务,保证端口及时释放。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

import uvicorn

from guard_api.app import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8778


class _EmbeddedServer(uvicorn.Server):
    """挂载模式:信号处理归宿主进程,uvicorn 只负责 HTTP 生命周期。

    新版 uvicorn(≥0.29)经 ``capture_signals()`` 在主线程安装信号处理器;
    覆盖为空操作,否则它会短暂改写宿主(supervisor)的 Ctrl+C 语义。
    """

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield

    def install_signal_handlers(self) -> None:  # 兼容旧版 uvicorn
        return


async def start_guard_api(
    db: str | Path | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    limit: int = 1000,
    min_level: str = "MEDIUM",
    session_gap: float = 3600.0,
    token: str | None = None,
    log_level: str = "warning",
) -> tuple[_EmbeddedServer, asyncio.Task[None]]:
    """在当前事件循环里启动 guard API,返回 ``(server, task)``。

    ``db`` 为空时回退到环境变量 ``ACPS_GUARD_DB``;两者都没有则服务可启动,
    但检测端点会返回 503 ``db_not_configured``(/health 始终可用)。
    """
    resolved_db = db if db is not None else os.getenv("ACPS_GUARD_DB")
    app = create_app(
        str(resolved_db) if resolved_db else None,
        limit=limit,
        min_level=min_level,
        session_gap=session_gap,
        token=token if token is not None else os.getenv("ACPS_GUARD_TOKEN"),
    )
    config = uvicorn.Config(app, host=host, port=int(port), log_level=log_level)
    server = _EmbeddedServer(config)
    task = asyncio.create_task(server.serve(), name="guard-api-uvicorn")
    return server, task


async def stop_guard_api(server: Any, task: asyncio.Task[None], *, timeout: float = 5.0) -> None:
    """优雅停止:置 should_exit → 等待任务收尾;超时则取消,不让宿主卡死。

    任务若以异常收尾,异常原样向上抛,由宿主决定记日志还是退出。
    """
    if task.done():
        return
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
