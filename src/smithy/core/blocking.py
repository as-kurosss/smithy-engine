"""Offload blocking calls to a worker thread with a hard timeout.

UIA/COM calls can hang indefinitely (dead dialogs, stalled COM apartments).
Every blocking offload in the windows tools goes through :func:`run_blocking`,
which bounds the wait: default 30 s, tunable via the ``SMITHY_BLOCKING_TIMEOUT``
environment variable. A timeout raises :class:`PlatformError` instead of
leaving the bot blocked forever on a starved thread pool.

Each call runs on a dedicated single-thread executor so a hung call can only
occupy its own worker thread — it can never starve a shared pool and delay
unrelated tools. A thread whose call has timed out is abandoned in place (it
is not interruptible); it will be joined at interpreter exit.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from smithy.core.errors import PlatformError


def _default_timeout() -> float:
    raw = os.environ.get("SMITHY_BLOCKING_TIMEOUT", "30")
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


async def run_blocking(
    fn: Callable[..., Any],
    /,
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Run ``fn(*args, **kwargs)`` in a dedicated worker thread with a timeout.

    Raises:
        PlatformError: If the call does not finish within *timeout* seconds
            (default: ``SMITHY_BLOCKING_TIMEOUT`` env var, else 30 s).
    """
    limit = timeout if timeout is not None else _default_timeout()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="smithy-blocking")
    try:
        future = loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))
        return await asyncio.wait_for(future, limit)
    except TimeoutError as exc:
        name = getattr(fn, "__qualname__", None) or repr(fn)
        raise PlatformError(
            f"blocking call {name} timed out after {limit:g}s "
            "(tune SMITHY_BLOCKING_TIMEOUT if this is expected)",
        ) from exc
    finally:
        executor.shutdown(wait=False)
