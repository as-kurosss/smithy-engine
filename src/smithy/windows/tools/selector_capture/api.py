"""Programmatic capture API — record a selector from inside bot code.

The interactive companion to the CLI recorders::

    from smithy.windows.tools.selector_capture import capture_once

    sel = capture_once()      # blocks: hover an element, press CTRL
    await bot.click(**sel.selector)

Used by the facade's dev-capture mode (``key=`` arguments): the bot
script pauses at an unknown selector, the developer captures it, the
selector is stored and the run continues.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from smithy.core.errors import ToolError
from smithy.windows.tools.selector_capture.capture import (
    capture_at_point,
    path_to_dicts,
)
from smithy.windows.tools.selector_capture.generate import build_inline_selector
from smithy.windows.tools.selector_capture.recorder import (
    _ListenerGroup,
    _log_ranked,
    _rank_captured,
    _require_pynput,
    _shared_listener,
)

if TYPE_CHECKING:
    from smithy.windows.selector_rank import RankedSelector

logger = logging.getLogger(__name__)


class CaptureCancelled(ToolError):
    """The user aborted a capture (pressed ESC)."""


@dataclass(frozen=True)
class CapturedSelector:
    """Result of one interactive capture.

    Attributes:
        selector: Ranked minimal selector config (name / automation_id /
            control_type / class_name) — spread it into facade calls
            (``bot.click(**sel.selector)``) or store it.
        full_path: Ancestor chain of the element — a fallback anchor
            when the minimal selector goes stale.
        confidence: Ranking confidence (``high``/``medium``/``low``).
        warnings: Selector fragility hints from the ranker.
    """

    selector: dict[str, Any]
    full_path: list[dict[str, Any]] = field(default_factory=list)
    confidence: str | None = None
    warnings: tuple[str, ...] = ()


def capture_once() -> CapturedSelector:
    """Block until the user presses CTRL over a UI element; rank it.

    Global hotkeys (same as the CLI single mode): **CTRL alone**
    captures the element under the cursor, **ESC** cancels.

    Returns:
        A :class:`CapturedSelector` with the ranked config.

    Raises:
        CaptureCancelled: The user pressed ESC.
        ImportError: ``pynput`` is not installed (``smithy[capture]``).
    """
    _require_pynput()
    events: queue.Queue[Any] = queue.Queue()
    with _ListenerGroup([_shared_listener(events)]):
        logger.info("Capture: hover an element and press CTRL (ESC to cancel)...")
        while True:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if event.kind == "escape":
                raise CaptureCancelled("capture cancelled by user (ESC)")
            if event.kind == "trigger":
                break

    from pynput.mouse import Controller as MouseCtrl

    mouse = MouseCtrl()
    x, y = mouse.position
    # UIA/comtypes COM apartments are per-thread: when this runs off the
    # main thread (capture_once_async), initialize COM for it — regular
    # tools get this for free because they import uiautomation inside
    # their worker threads, but capture_at_point imports it eagerly.
    comtypes = None
    with contextlib.suppress(ImportError):  # pragma: no cover — Windows-only dep
        import comtypes  # type: ignore[no-redef]

    if comtypes is not None:
        comtypes.CoInitialize()
    try:
        path, sel = capture_at_point(float(x), float(y))
        logger.info("Captured: %s", sel.label())

        ranked = _rank_captured(sel)
        _log_ranked(ranked, sel)
    finally:
        if comtypes is not None:
            comtypes.CoUninitialize()
    if ranked is not None:
        return _from_ranked(ranked, path)
    logger.warning("Ranking failed — using the unranked all-fields selector")
    return CapturedSelector(
        selector=build_inline_selector(sel),
        full_path=path_to_dicts(path) if path else [],
    )


async def capture_once_async() -> CapturedSelector:
    """Async twin of :func:`capture_once` (runs in a worker thread)."""
    return await asyncio.to_thread(capture_once)


def _from_ranked(ranked: RankedSelector, path: list[Any] | None) -> CapturedSelector:
    return CapturedSelector(
        selector=dict(ranked.config),
        full_path=path_to_dicts(path) if path else [],
        confidence=ranked.confidence,
        warnings=ranked.warnings,
    )
