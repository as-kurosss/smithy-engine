"""Hotkey-based recorders for UI element selectors.

Ported from ``selector-capture/src/recorder.rs``.

Modes:
    single  — CTRL alone to capture the element under cursor (one flow
              node).  ESC to cancel.
    series  — automatically record every mouse click & text input;
              Ctrl+Shift+F2 to stop.
    record  — capture one element at a time with interactive prompts
              for tool type and parameters; Ctrl+Shift+F2 to finish.

All modes write the same shape — ``{"tool": "selector-capture",
"nodes": [...]}`` — so ``single`` is just a one-node flow and every
file replays the same way.

Uses ``pynput`` for global keyboard/mouse hooks and ``pyperclip``
for clipboard access.
"""

from __future__ import annotations

import json
import logging
import queue
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from smithcore.windows.selector_rank import RankedSelector

from smithcore.windows.tools.selector_capture.capture import (
    BestSelector,
    PathNode,
    capture_at_point,
    path_to_dicts,
)
from smithcore.windows.tools.selector_capture.generate import (
    FlowNode,
    GenerateParams,
    ToolType,
    build_inline_selector,
    generate_nodes,
    generate_nodes_from_config,
)

try:
    from pynput import keyboard as _kb
    from pynput import mouse as _ms
except Exception:  # pragma: no cover — optional recorder backend
    _kb = None
    _ms = None


def _require_pynput() -> None:
    """Raise a helpful error if the pynput backend is unavailable."""
    if _kb is None or _ms is None:
        raise ImportError(
            "pynput is required for selector-capture recorders.  "
            "Install it with:  pip install pynput"
        )


try:
    import pyperclip
except ImportError:
    pyperclip = None

logger = logging.getLogger(__name__)

# ── Event models ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SharedEvent:
    """Events emitted by the shared (CTRL-based) listener."""

    kind: Literal["trigger", "escape", "stop"]


@dataclass(frozen=True)
class SeriesEvent:
    """Events emitted by the series (auto-record) listener."""

    kind: Literal["stop", "mouse_down", "input"]
    char: str | None = None


# ── Input helpers ────────────────────────────────────────────────────────────


def _is_modifier(key: Any) -> bool:
    """Return ``True`` if *key* is a modifier key."""
    if _kb is None:
        return False
    return key in (
        _kb.Key.ctrl_l,
        _kb.Key.ctrl_r,
        _kb.Key.shift_l,
        _kb.Key.shift_r,
        _kb.Key.alt_l,
        _kb.Key.alt_gr,
        _kb.Key.cmd,
        _kb.Key.cmd_l,
        _kb.Key.cmd_r,
    )


def _is_printable(key: Any) -> bool:
    """Return ``True`` if *key* represents a printable character."""
    if isinstance(key, _kb.Key):
        return key in (
            _kb.Key.space,
            _kb.Key.enter,
            _kb.Key.backspace,
            _kb.Key.tab,
        )
    return isinstance(key, _kb.KeyCode) and key.char is not None


def _event_char(key: Any) -> str:
    """Return the character *key* would type (``""`` if not printable).

    ``backspace`` is reported as ``"\\b"`` so recorders can edit the buffer.
    """
    if _kb is None:
        return ""
    if key == _kb.Key.space:
        return " "
    if key == _kb.Key.tab:
        return "\t"
    if key == _kb.Key.enter:
        return "\n"
    if key == _kb.Key.backspace:
        return "\b"
    if isinstance(key, _kb.KeyCode) and key.char is not None:
        char = key.char
        return char if isinstance(char, str) else ""
    return ""


# ── Listener factories ───────────────────────────────────────────────────────


def _shared_listener(out: queue.Queue[SharedEvent]) -> _kb.Listener:
    """Create a keyboard listener for CTRL-trigger based capture.

    Events emitted:
        * ``SharedEvent("trigger")`` — CTRL pressed and released alone.
        * ``SharedEvent("escape")`` — ESC pressed (CTRL not held).
        * ``SharedEvent("stop")`` — Ctrl+Shift+F2 pressed.

    The listener blocks the CTRL-trigger when a non-modifier key is
    pressed while CTRL is held (e.g. CTRL+C → no trigger).
    """
    _require_pynput()
    ctrl_down = False
    shift_down = False
    blocked = False
    last_key_was_modifier = False

    def on_press(key: Any) -> None:
        nonlocal ctrl_down, shift_down, blocked, last_key_was_modifier

        # ── track modifiers ──
        if key in (_kb.Key.ctrl_l, _kb.Key.ctrl_r):
            ctrl_down = True
            blocked = False
            last_key_was_modifier = True
            return
        if key in (_kb.Key.shift_l, _kb.Key.shift_r):
            shift_down = True
            last_key_was_modifier = True
            return

        last_key_was_modifier = False

        if ctrl_down and shift_down and key == _kb.Key.f2:
            out.put(SharedEvent("stop"))
            return
        if ctrl_down:
            # Non-modifier while CTRL held → combo (e.g. CTRL+C)
            blocked = True
            return
        if key == _kb.Key.esc:
            out.put(SharedEvent("escape"))

    def on_release(key: Any) -> None:
        nonlocal ctrl_down, shift_down, blocked, last_key_was_modifier
        if key in (_kb.Key.ctrl_l, _kb.Key.ctrl_r):
            if ctrl_down and not blocked and last_key_was_modifier:
                out.put(SharedEvent("trigger"))
            ctrl_down = False
            blocked = False
            last_key_was_modifier = False
        elif key in (_kb.Key.shift_l, _kb.Key.shift_r):
            shift_down = False
            last_key_was_modifier = False

    return _kb.Listener(on_press=on_press, on_release=on_release)


def _series_listener(out: queue.Queue[SeriesEvent]) -> _kb.Listener:
    """Create a keyboard listener for series (auto-record) mode.

    Events emitted:
        * ``SeriesEvent("stop")`` — Ctrl+Shift+F2 pressed.
        * ``SeriesEvent("input")`` — printable key without CTRL/ALT.
    """
    _require_pynput()
    ctrl = False
    shift = False
    alt = False

    def on_press(key: Any) -> None:
        nonlocal ctrl, shift, alt

        if key in (_kb.Key.ctrl_l, _kb.Key.ctrl_r):
            ctrl = True
            return
        if key in (_kb.Key.shift_l, _kb.Key.shift_r):
            shift = True
            return
        if key in (_kb.Key.alt_l, _kb.Key.alt_gr):
            alt = True
            return

        if ctrl and shift and key == _kb.Key.f2:
            out.put(SeriesEvent("stop"))
            return
        if not ctrl and not alt and _is_printable(key):
            out.put(SeriesEvent("input", _event_char(key)))

    def on_release(key: Any) -> None:
        nonlocal ctrl, shift, alt
        if key in (_kb.Key.ctrl_l, _kb.Key.ctrl_r):
            ctrl = False
        elif key in (_kb.Key.shift_l, _kb.Key.shift_r):
            shift = False
        elif key in (_kb.Key.alt_l, _kb.Key.alt_gr):
            alt = False

    return _kb.Listener(on_press=on_press, on_release=on_release)


def _mouse_listener(out: queue.Queue[SeriesEvent]) -> _ms.Listener:
    """Create a mouse listener for series mode.

    Events emitted:
        * ``SeriesEvent("mouse_down")`` — any mouse button pressed.
    """
    _require_pynput()

    def on_click(x: int, y: int, button: _ms.Button, pressed: bool) -> None:
        if pressed:
            out.put(SeriesEvent("mouse_down"))

    return _ms.Listener(on_click=on_click)


class _ListenerGroup:
    """Convenience wrapper that starts/stops multiple pynput listeners."""

    def __init__(self, listeners: list[Any]) -> None:
        self._listeners = listeners

    def start(self) -> None:
        """Start all listeners as daemon threads."""
        for listener in self._listeners:
            listener.daemon = True
            listener.start()

    def stop(self) -> None:
        """Stop all listeners."""
        for listener in self._listeners:
            listener.stop()

    def __enter__(self) -> _ListenerGroup:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


# ── Clipboard helper ─────────────────────────────────────────────────────────


def _copy_to_clipboard(text: str) -> bool:
    """Copy *text* to the system clipboard.

    Returns ``True`` on success, ``False`` if pyperclip is unavailable
    or the copy operation fails.
    """
    if pyperclip is None:
        logger.warning("pyperclip is not installed; clipboard copy skipped")
        return False
    try:
        pyperclip.copy(text)
        return True
    except Exception:
        logger.exception("Clipboard write failed")
        return False


# ── Shared capture → nodes helpers ───────────────────────────────────────────


def _log_ranked(ranked: RankedSelector | None, sel: BestSelector) -> None:
    """Log the winning selector, its confidence, and any warnings."""
    logger.info(
        "  selector: %s (confidence: %s)",
        ranked.config if ranked is not None else build_inline_selector(sel),
        ranked.confidence if ranked is not None else "unknown",
    )
    if ranked is not None:
        for warning in ranked.warnings:
            logger.warning("  ! %s", warning)


def _nodes_for_capture(
    sel: BestSelector,
    path: list[PathNode] | None,
    tool: ToolType,
    params: GenerateParams,
    ranked: RankedSelector | None,
) -> list[FlowNode]:
    """Build flow nodes for one capture, with the full UIA path attached.

    Shared by single and record modes so one capture always yields the
    same nodes shape. Uses the ranked minimal config when available,
    otherwise the unranked all-fields dump.
    """
    path_dicts = path_to_dicts(path) if path else []
    if ranked is not None and tool.needs_selector:
        generated = generate_nodes_from_config(ranked.config, tool, params)
    else:
        generated = generate_nodes(sel, tool, params)
    return [FlowNode(tool=n.tool, args=n.args, full_path=path_dicts) for n in generated]


def _input_text_nodes(
    selector: BestSelector,
    path_dicts: Sequence[Mapping[str, Any]],
) -> list[FlowNode]:
    """Build ``input_text`` nodes for series-mode keyboard flushes.

    Series mode only knows *that* text was typed (not the keys), so the
    node carries the selector of the last clicked element with its full
    path and no ``text`` — fill it in, or re-record the step in record
    mode.
    """
    return [
        FlowNode(tool=n.tool, args=n.args, full_path=list(path_dicts))
        for n in generate_nodes(selector, ToolType.INPUT_TEXT, GenerateParams())
    ]


# ── SINGLE MODE ──────────────────────────────────────────────────────────────


def run_single_mode(
    output: str,
    description: str | None = None,
    tool_type: ToolType = ToolType.CLICK,
    text: str = "",
    duration_ms: int = 1000,
    clip: bool = False,
) -> None:
    """Run single-capture mode.

    CTRL alone → capture element under cursor, generate one flow node, save.
    ESC → cancel.

    The output shape matches series/record modes (``nodes`` with a single
    entry) so every capture file replays the same way.

    Args:
        output: Path to the output JSON file (overwritten).
        description: Accepted for backward compatibility; logged but not
            persisted (flow nodes carry no description field).
        tool_type: Tool type for config generation.
        text: Text for ``input_text`` / ``set_text`` tools.
        duration_ms: Duration in ms for the ``wait`` tool.
        clip: If ``True``, copy the generated config to the clipboard.
    """
    _require_pynput()
    logger.info("Selector Capture: Single Mode")
    logger.info("Place cursor over a UI element and press CTRL to capture. Press ESC to cancel.")

    events: queue.Queue[SharedEvent] = queue.Queue()

    selector: BestSelector | None = None
    path: list[PathNode] | None = None

    with _ListenerGroup([_shared_listener(events)]):
        logger.info("Waiting for CTRL...")

        while True:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue

            if event.kind == "stop":
                break
            if event.kind == "escape":
                logger.info("Cancelled.")
                return
            if event.kind == "trigger":
                try:
                    from pynput.mouse import Controller as MouseCtrl

                    mouse = MouseCtrl()
                    x, y = mouse.position
                    path, sel = capture_at_point(float(x), float(y))
                    selector = sel
                    break
                except Exception:
                    logger.exception("Capture failed")
                    continue

    if selector is None:
        logger.info("Cancelled.")
        return

    if description:
        logger.info("Description: %s", description)
    logger.info("Captured: %s", selector.label())

    params = GenerateParams(
        text=text,
        duration_ms=duration_ms,
    )
    ranked = _rank_captured(selector)
    _log_ranked(ranked, selector)
    nodes = _nodes_for_capture(selector, path, tool_type, params, ranked)
    for node in nodes:
        logger.info(
            "    -> %s: %s",
            node.tool,
            json.dumps(node.args, ensure_ascii=False),
        )

    if clip:
        clip_text = json.dumps(nodes[0].args, ensure_ascii=False)

        if _copy_to_clipboard(clip_text):
            logger.info("Copied to clipboard (%s)", tool_type.value)

    _write_flow_output(output, nodes)
    logger.info("Saved flow to %s", output)


# ── SERIES MODE ──────────────────────────────────────────────────────────────


def run_series_mode(output: str) -> None:
    """Run series (action recorder) mode.

    Automatically records every mouse click and keyboard input.
    Ctrl+Shift+F2 → stop and save as flow nodes.

    Note: keyboard input records the *target element* (with its full
    path) but not the typed text itself — it only knows that keys were
    pressed. Fill in ``text`` afterwards, or use record mode.

    Args:
        output: Path to the output JSON file.
    """
    _require_pynput()
    logger.info("Selector Capture: Series Mode")
    logger.info("Mouse clicks -> click node pairs")
    logger.info("Keyboard -> input_text node pairs")
    logger.info("Press Ctrl+Shift+F2 to stop recording")

    events: queue.Queue[SeriesEvent] = queue.Queue()

    nodes: list[FlowNode] = []
    pending_input = False
    last_selector: BestSelector | None = None
    last_path: list[dict[str, Any]] = []

    with _ListenerGroup([_series_listener(events), _mouse_listener(events)]):
        while True:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue

            if event.kind == "stop":
                # Flush pending text input before stopping.
                if pending_input and last_selector is not None:
                    nodes.extend(_input_text_nodes(last_selector, last_path))
                break

            if event.kind == "mouse_down":
                # Flush pending input before handling click.
                if pending_input and last_selector is not None:
                    nodes.extend(_input_text_nodes(last_selector, last_path))
                pending_input = False

                try:
                    from pynput.mouse import Controller as MouseCtrl

                    mouse = MouseCtrl()
                    x, y = mouse.position
                    path, sel = capture_at_point(float(x), float(y))
                    params = GenerateParams()
                    path_dicts = path_to_dicts(path) if path else []
                    new_nodes = [
                        FlowNode(tool=n.tool, args=n.args, full_path=path_dicts)
                        for n in generate_nodes(sel, ToolType.CLICK, params)
                    ]
                    nodes.extend(new_nodes)
                    last_selector = sel
                    last_path = path_dicts
                except Exception:
                    logger.exception("Could not capture element at mouse position")

            elif event.kind == "input":
                pending_input = True

    logger.info("Recording stopped — %d nodes generated", len(nodes))

    if nodes:
        _write_flow_output(output, nodes)
        logger.info("Saved flow to %s", output)


# ── RECORD MODE ──────────────────────────────────────────────────────────────


def run_record_mode(output: str) -> None:
    """Run interactive record mode.

    CTRL → capture element → prompt for tool type and parameters → repeat.
    ESC → discard last capture.
    Ctrl+Shift+F2 → finish and save.

    Args:
        output: Path to the output JSON file.
    """
    _require_pynput()
    logger.info("Selector Capture: Record Mode")
    logger.info("Hotkeys:")
    logger.info("  CTRL          -> capture element under cursor")
    logger.info("  ESC           -> discard last capture")
    logger.info("  Ctrl+Shift+F2 -> finish and save")

    events: queue.Queue[SharedEvent] = queue.Queue()

    nodes: list[FlowNode] = []
    el_counter = 0

    with _ListenerGroup([_shared_listener(events)]):
        logger.info("Ready. Press CTRL over a UI element...")

        while True:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue

            if event.kind == "stop":
                break

            if event.kind == "escape":
                if not nodes:
                    logger.info("Discarded — no captures taken.")
                    return
                # Remove the last node.
                removed = max(0, len(nodes) - 1)
                count = len(nodes) - removed
                nodes = nodes[:removed]
                logger.info(
                    "Discarded capture #%d (%d nodes removed)",
                    el_counter,
                    count,
                )
                el_counter = max(0, el_counter - 1)
                continue

            if event.kind == "trigger":
                try:
                    from pynput.mouse import Controller as MouseCtrl

                    mouse = MouseCtrl()
                    x, y = mouse.position
                    path, sel = capture_at_point(float(x), float(y))
                except Exception:
                    logger.exception("Capture failed")
                    logger.info("[CTRL] continue...")
                    continue

                el_counter += 1
                label = sel.label()
                extra = _extra_info(sel)

                logger.info("--- Capture #%d ---", el_counter)
                logger.info("  %s%s", label, extra)

                # Rank the selector (Playwright-codegen equivalent): minimal
                # fields + uniqueness check + confidence. Falls back to the
                # all-fields dump when ranking fails (e.g. UIA walk error).
                ranked = _rank_captured(sel)
                _log_ranked(ranked, sel)

                # Prompt for tool type.
                tool = _prompt_tool_type()

                # Prompt for parameters.
                params = GenerateParams()
                if tool.needs_text:
                    params = GenerateParams(text=_prompt_text())
                if tool.needs_duration:
                    params = GenerateParams(duration_ms=_prompt_duration())

                # Generate nodes (same shape as single mode).
                new_nodes = _nodes_for_capture(sel, path, tool, params, ranked)
                for node in new_nodes:
                    logger.info(
                        "    -> %s: %s",
                        node.tool,
                        json.dumps(node.args, ensure_ascii=False),
                    )
                nodes.extend(new_nodes)

                logger.info("[CTRL] continue  [ESC] discard  [Ctrl+Shift+F2] finish...")

    if not nodes:
        logger.info("No captures taken.")
        return

    logger.info("Generated %d nodes.", len(nodes))
    _write_flow_output(output, nodes)
    logger.info("Saved flow to %s", output)


# ── Interactive prompts ──────────────────────────────────────────────────────


def _prompt_tool_type() -> ToolType:
    """Prompt the user to choose a tool type on stdin.

    Returns:
        The selected :class:`ToolType`.
    """
    valid = [t.value for t in ToolType]
    prompt = f"  -> Tool? [{'/'.join(valid)}] "
    while True:
        try:
            raw = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            logger.info("Input cancelled; defaulting to 'click'.")
            return ToolType.CLICK
        try:
            return ToolType(raw)
        except ValueError:
            logger.error(
                "Unknown tool type '%s'. Valid options: %s",
                raw,
                ", ".join(valid),
            )


def _prompt_text() -> str:
    """Prompt the user for text input (mandatory for input_text / set_text).

    Returns:
        The entered text (guaranteed non-empty).
    """
    while True:
        try:
            raw = input("  -> text: ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.info("Input cancelled; using empty text.")
            return ""
        if raw:
            return raw
        logger.error("Text cannot be empty.")


def _prompt_duration() -> int:
    """Prompt the user for a duration in milliseconds (for wait tool).

    Returns:
        The duration in ms (defaults to 1000 if the user presses Enter).
    """
    while True:
        try:
            raw = input("  -> duration_ms: [1000] ").strip()
        except (EOFError, KeyboardInterrupt):
            logger.info("Input cancelled; using default 1000 ms.")
            return 1000
        if not raw:
            return 1000
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
        logger.error("Must be a positive integer.")


def _rank_captured(sel: BestSelector) -> RankedSelector | None:
    """Rank a captured selector, returning ``None`` on failure.

    Ranking walks the live desktop (uniqueness check) and can fail on
    UIA hiccups — record mode must keep working then, using the
    unranked all-fields selector instead.
    """
    try:
        from smithcore.windows.selector_rank import rank_best_selector

        return rank_best_selector(sel)
    except Exception:
        logger.exception("Selector ranking failed — using unranked selector")
        return None


def _extra_info(sel: BestSelector) -> str:
    """Return extra info string for display (automation_id, class_name, etc.)."""
    if sel.automation_id is not None:
        return f" (automation_id: {sel.automation_id})"
    if sel.class_name is not None:
        return f" (class_name: {sel.class_name})"
    if sel.control_type not in ("Custom", ""):
        return f" (type: {sel.control_type})"
    return ""


# ── File I/O helpers ─────────────────────────────────────────────────────────


def _write_flow_output(output: str, nodes: list[FlowNode]) -> None:
    """Write a list of :class:`FlowNode` objects as JSON to *output*.

    The output shape matches the Rust ``FlowGraphOutput`` format::

        {
          "tool": "selector-capture",
          "nodes": [ { "tool": "windows.find", "args": {...} }, ... ]
        }

    Args:
        output: Path to the output JSON file.
        nodes: The generated flow nodes to serialise.
    """
    data: dict[str, Any] = {
        "tool": "selector-capture",
        "nodes": [
            {
                "tool": node.tool,
                "args": node.args,
                **({"full_path": node.full_path} if node.full_path else {}),
            }
            for node in nodes
        ],
    }
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
