"""Offload blocking calls to a worker thread with a hard timeout.

UIA/COM calls can hang indefinitely (dead dialogs, stalled COM apartments).
Every blocking offload in the windows tools goes through :func:`run_blocking`,
which bounds the wait: default 30 s, tunable via the ``SMITHY_BLOCKING_TIMEOUT``
environment variable. A timeout raises :class:`PlatformError` instead of
leaving the bot blocked forever.

COM threading model
-------------------

UIA elements are apartment-bound COM objects: using a pointer created in
one thread's apartment from another thread (or after that apartment is
torn down) fails with ``E_FAIL`` or crashes with an access violation.
``uiautomation``'s official guidance is equally strict — "you can't use
a Control created in a different thread".

Therefore all blocking UIA work runs on a single, long-lived worker
thread that owns one COM apartment (initialized once via
``CoInitializeEx``). UIA elements are created and consumed on that same
thread, so they never cross apartments. The trade-off is serialization —
irrelevant in practice because bot and flow code is sequential.

If a call hangs past *timeout*, the thread is abandoned in place (a hung
COM call is not interruptible) and the executor is replaced before the
next call, so one dead dialog cannot poison every subsequent tool call.
Abandoned threads are joined at interpreter exit.
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from smithy.core.errors import PlatformError

_T = TypeVar("_T")

_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _default_timeout() -> float:
    raw = os.environ.get("SMITHY_BLOCKING_TIMEOUT", "30")
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


def _com_thread_init() -> None:
    """Initialize this worker thread's COM apartment (once per thread)."""
    try:
        import comtypes
    except Exception:  # pragma: no cover — non-Windows
        return
    comtypes.CoInitializeEx()


def _run_with_com(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run *fn* with a COM apartment initialized in the current thread.

    For callers outside the shared worker thread (e.g. the synchronous
    CLI capture path on the main thread). A no-op when ``comtypes`` is
    unavailable (non-Windows hosts).
    """
    try:
        import comtypes
    except Exception:  # pragma: no cover — non-Windows
        return fn(*args, **kwargs)
    comtypes.CoInitializeEx()
    try:
        return fn(*args, **kwargs)
    finally:
        comtypes.CoUninitialize()


def _get_executor() -> ThreadPoolExecutor:
    """The shared single-thread executor (created on first use)."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="smithy-blocking",
                initializer=_com_thread_init,
            )
        return _executor


def _abandon_executor() -> None:
    """Drop the executor after a hung call (a fresh one is made lazily)."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            executor, _executor = _executor, None
    executor.shutdown(wait=False)


async def run_on_uia_thread(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run ``fn(*args, **kwargs)`` on the shared UIA thread — no timeout.

    For interactive flows (e.g. waiting for a human to press CTRL during
    dev capture) that legitimately take longer than any sane timeout.
    Uses the same apartment-bound thread as :func:`run_blocking`, so
    everything it touches shares the process-wide COM apartment.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_get_executor(), functools.partial(fn, *args, **kwargs))
    return await future


async def run_blocking(
    fn: Callable[..., Any],
    /,
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Run ``fn(*args, **kwargs)`` on the shared UIA worker thread.

    The thread owns a COM apartment for the whole process lifetime, so
    UIA elements created by one call stay valid for the next. After a
    timeout the thread is abandoned (hung calls are not interruptible)
    and the next call gets a fresh thread.

    Raises:
        PlatformError: If the call does not finish within *timeout* seconds
            (default: ``SMITHY_BLOCKING_TIMEOUT`` env var, else 30 s).
    """
    limit = timeout if timeout is not None else _default_timeout()
    loop = asyncio.get_running_loop()
    executor = _get_executor()
    try:
        future = loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))
        return await asyncio.wait_for(future, limit)
    except TimeoutError as exc:
        _abandon_executor()
        name = getattr(fn, "__qualname__", None) or repr(fn)
        raise PlatformError(
            f"blocking call {name} timed out after {limit:g}s "
            "(tune SMITHY_BLOCKING_TIMEOUT if this is expected)",
        ) from exc
