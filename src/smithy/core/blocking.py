"""Offload blocking calls to a worker thread with a hard timeout.

UIA/COM calls can hang indefinitely (dead dialogs, stalled COM apartments).
Every blocking offload in the windows tools goes through :func:`run_blocking`,
which bounds the wait: default 30 s, tunable via the ``SMITHY_BLOCKING_TIMEOUT``
environment variable. A timeout raises :class:`PlatformError` instead of
leaving the bot blocked forever on a starved thread pool.
"""

from __future__ import annotations

import asyncio
import functools
import os
from collections.abc import Callable
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
    """Run ``fn(*args, **kwargs)`` in the default executor with a timeout.

    Raises:
        PlatformError: If the call does not finish within *timeout* seconds
            (default: ``SMITHY_BLOCKING_TIMEOUT`` env var, else 30 s).
    """
    limit = timeout if timeout is not None else _default_timeout()
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))
    try:
        return await asyncio.wait_for(future, limit)
    except TimeoutError as exc:
        name = getattr(fn, "__qualname__", None) or repr(fn)
        raise PlatformError(
            f"blocking call {name} timed out after {limit:g}s "
            "(tune SMITHY_BLOCKING_TIMEOUT if this is expected)",
        ) from exc
