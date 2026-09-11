"""Programmatic capture API — record a selector from inside bot code.

The interactive companion to the CLI recorders::

    from smithcore.windows.tools.selector_capture import capture_once

    sel = capture_once()      # blocks: hover an element, press CTRL
    await bot.click(**sel.selector)

Used by the facade's dev-capture mode (``key=`` arguments): the bot
script pauses at an unknown selector, the developer captures it, the
selector is stored and the run continues.
"""

from __future__ import annotations

import asyncio
import logging
import queue
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from smithcore.core.errors import ToolError
from smithcore.windows.tools.selector_capture.capture import (
    BestSelector,
    PathNode,
    capture_at_point,
    path_to_dicts,
)
from smithcore.windows.tools.selector_capture.generate import build_inline_selector
from smithcore.windows.tools.selector_capture.recorder import (
    _ListenerGroup,
    _log_ranked,
    _rank_captured,
    _require_pynput,
    _shared_listener,
)

if TYPE_CHECKING:
    from smithcore.windows.selector_rank import RankedSelector

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


def _wait_for_trigger() -> tuple[float, float]:
    """Block until the user presses CTRL (or cancels with ESC); return the cursor position."""
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
    return float(x), float(y)


def _capture_at(x: float, y: float) -> CapturedSelector:
    """Capture and rank the element at *(x, y)* (needs a COM apartment)."""
    from smithcore.core.blocking import _run_with_com

    def _capture_and_rank() -> tuple[list[PathNode], BestSelector, RankedSelector | None]:
        path, sel = capture_at_point(x, y)
        logger.info("Captured: %s", sel.label())
        return path, sel, _rank_captured(sel)

    path, sel, ranked = _run_with_com(_capture_and_rank)
    _log_ranked(ranked, sel)
    if ranked is not None:
        return _from_ranked(ranked, path)
    logger.warning("Ranking failed — using the unranked all-fields selector")
    return CapturedSelector(
        selector=build_inline_selector(sel),
        full_path=path_to_dicts(path) if path else [],
    )


def capture_once() -> CapturedSelector:
    """Block until the user presses CTRL over a UI element; rank it.

    Global hotkeys (same as the CLI single mode): **CTRL alone**
    captures the element under the cursor, **ESC** cancels.

    Returns:
        A :class:`CapturedSelector` with the ranked config.

    Raises:
        CaptureCancelled: The user pressed ESC.
        ImportError: ``pynput`` is not installed (``smithcore[capture]``).
    """
    x, y = _wait_for_trigger()
    return _capture_at(x, y)


async def capture_once_async() -> CapturedSelector:
    """Async twin of :func:`capture_once`.

    The interactive CTRL wait runs on a plain worker thread; the UIA
    capture runs on the shared COM-apartment thread (no timeout — a
    human may take their time hovering).
    """
    from smithcore.core.blocking import run_on_uia_thread

    x, y = await asyncio.to_thread(_wait_for_trigger)
    return await run_on_uia_thread(_capture_at, x, y)


def _from_ranked(ranked: RankedSelector, path: list[Any] | None) -> CapturedSelector:
    return CapturedSelector(
        selector=dict(ranked.config),
        full_path=path_to_dicts(path) if path else [],
        confidence=ranked.confidence,
        warnings=ranked.warnings,
    )
