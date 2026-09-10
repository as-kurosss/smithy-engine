"""Programmatic series recorder — record clicks and typed text to nodes.

The blocking companion to the CLI ``series`` mode, driven by a
``threading.Event`` instead of the global Ctrl+Shift+F2 hotkey so a server
(see ``smithy-designer``) can start/stop it over HTTP.

Every mouse click captures the element under the cursor and emits a
``windows.click`` node. Printable keys are buffered and flushed — when the
next click arrives or recording stops — into a ``windows.input_text`` node
targeting the element that had focus when typing started. Unlike the CLI
series mode, the **typed text is preserved**.

    import threading
    from smithy.windows.tools.selector_capture import record_series

    stop = threading.Event()
    nodes = record_series(stop)          # click around, type, then:
    stop.set()
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import cast

from smithy.windows.tools.selector_capture.capture import BestSelector, PathNode, capture_at_point
from smithy.windows.tools.selector_capture.generate import (
    FlowNode,
    GenerateParams,
    ToolType,
)
from smithy.windows.tools.selector_capture.recorder import (
    SeriesEvent,
    _ListenerGroup,
    _mouse_listener,
    _nodes_for_capture,
    _require_pynput,
    _series_listener,
)

logger = logging.getLogger(__name__)


def _reduce_events(
    events: queue.Queue[SeriesEvent],
    stop: threading.Event,
    *,
    capture_point: Callable[[float, float], tuple[list[PathNode], BestSelector]],
    mouse_position: Callable[[], tuple[float, float]],
    emit: Callable[[FlowNode], None],
    timeout: float = 0.1,
) -> None:
    """Drain recorder *events* into flow nodes until *stop* or a stop event.

    Split out from :func:`record_series` so the event handling (click →
    ``windows.click``, buffered text → ``windows.input_text``) is testable
    without pynput or a real desktop.
    """
    text: list[str] = []
    last_selector: BestSelector | None = None
    last_path: list[PathNode] | None = None

    def flush_text() -> None:
        nonlocal text
        if not text or last_selector is None:
            text = []
            return
        value = "".join(text)
        text = []
        if value:
            for node in _nodes_for_capture(
                last_selector, last_path, ToolType.INPUT_TEXT, GenerateParams(text=value), None
            ):
                emit(node)

    while not stop.is_set():
        try:
            event = events.get(timeout=timeout)
        except queue.Empty:
            continue
        if event.kind == "stop":
            break
        if event.kind == "mouse_down":
            flush_text()
            try:
                x, y = mouse_position()
                path, selector = capture_point(float(x), float(y))
            except Exception:
                logger.exception("Could not capture element at mouse position")
                continue
            last_selector = selector
            last_path = path
            for node in _nodes_for_capture(selector, path, ToolType.CLICK, GenerateParams(), None):
                emit(node)
        elif event.kind == "input":
            char = event.char or ""
            if char == "\b":
                if text:
                    text.pop()
            elif char:
                text.append(char)
    flush_text()


def record_series(
    stop: threading.Event,
    *,
    on_step: Callable[[FlowNode], None] | None = None,
) -> list[FlowNode]:
    """Record clicks and typed text until *stop* is set.

    Args:
        stop: Set from another thread to end the recording.
        on_step: Optional callback invoked for each generated node as it is
            recorded (used for live progress in a UI).

    Returns:
        The recorded nodes, in order.

    Raises:
        ImportError: ``pynput`` is not installed (``smithy[capture]``).
    """
    _require_pynput()

    events: queue.Queue[SeriesEvent] = queue.Queue()
    nodes: list[FlowNode] = []

    def emit(node: FlowNode) -> None:
        nodes.append(node)
        if on_step is not None:
            try:
                on_step(node)
            except Exception:
                logger.exception("record on_step callback failed")

    def capture_point(x: float, y: float) -> tuple[list[PathNode], BestSelector]:
        from smithy.core.blocking import _run_with_com

        result = _run_with_com(capture_at_point, x, y)
        return cast("tuple[list[PathNode], BestSelector]", result)

    def mouse_position() -> tuple[float, float]:
        from pynput.mouse import Controller as MouseCtrl

        x, y = MouseCtrl().position
        return float(x), float(y)

    with _ListenerGroup([_series_listener(events), _mouse_listener(events)]):
        _reduce_events(
            events,
            stop,
            capture_point=capture_point,
            mouse_position=mouse_position,
            emit=emit,
        )
    return nodes
