"""Config generation per tool type.

Takes a captured ``BestSelector`` and produces the JSON config dict
that the corresponding ``smithcore.windows.tools`` tool expects.

Ported from ``selector-capture/src/generate.rs``.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import assert_never

from smithcore.windows.tools.selector_capture.capture import BestSelector


class ToolType(enum.StrEnum):
    """Target tool type for config generation.

    Attributes:
        CLICK: Click a UI element.
        INPUT_TEXT: Append text into an input element.
        SET_TEXT: Replace text in an input element.
        WAIT: Pause execution for a fixed duration.
    """

    CLICK = "click"
    INPUT_TEXT = "input_text"
    SET_TEXT = "set_text"
    WAIT = "wait"

    # -- helpers (ported from Rust ToolType impl) --

    @property
    def needs_text(self) -> bool:
        """Return ``True`` if this tool type requires a *text* parameter."""
        return self in (ToolType.INPUT_TEXT, ToolType.SET_TEXT)

    @property
    def needs_duration(self) -> bool:
        """Return ``True`` if this tool type requires a *duration_ms* parameter."""
        return self is ToolType.WAIT

    @property
    def needs_selector(self) -> bool:
        """Return ``True`` if this tool type targets a UI element."""
        return self in (ToolType.CLICK, ToolType.INPUT_TEXT, ToolType.SET_TEXT)


@dataclass(frozen=True, slots=True)
class GenerateParams:
    """Parameters for generating tool configs.

    Attributes:
        text: Text for ``input_text`` / ``set_text`` tools.
        duration_ms: Duration (milliseconds) for the ``wait`` tool.
    """

    text: str = ""
    duration_ms: int = 1000


@dataclass(frozen=True, slots=True)
class FlowNode:
    """A single node in a generated flow.

    Attributes:
        tool: Fully-qualified tool name (e.g. ``windows.find``).
        args: Configuration dictionary the tool expects.
        full_path: Complete UIA element path from desktop root to the target.
    """

    tool: str
    args: Mapping[str, object] = field(default_factory=dict)
    full_path: Sequence[Mapping[str, object]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_nodes(
    selector: BestSelector,
    tool: ToolType,
    params: GenerateParams,
) -> list[FlowNode]:
    """Generate a :class:`FlowNode` for a captured element.

    Mapping:
        * ``Click`` → ``[windows.click]`` (inline selectors)
        * ``InputText`` → ``[windows.input_text]`` (inline selectors)
        * ``SetText`` → ``[windows.set_text]`` (inline selectors)
        * ``Wait`` → ``[windows.wait]``

    Args:
        selector: The best-effort selector captured from a UI element.
        tool: The target tool type.
        params: Generation parameters (text, duration).

    Returns:
        A list of :class:`FlowNode` objects forming the tool sequence.
    """
    match tool:
        case ToolType.CLICK:
            return [
                FlowNode(
                    tool="windows.click",
                    args=build_inline_selector(selector),
                ),
            ]
        case ToolType.INPUT_TEXT:
            return [
                FlowNode(
                    tool="windows.input_text",
                    args=build_inline_selector(selector, text=params.text),
                ),
            ]
        case ToolType.SET_TEXT:
            return [
                FlowNode(
                    tool="windows.set_text",
                    args=build_inline_selector(selector, text=params.text),
                ),
            ]
        case ToolType.WAIT:
            return [
                FlowNode(
                    tool="windows.wait",
                    args=build_wait_config(params.duration_ms),
                ),
            ]
        case _:
            assert_never(tool)


def generate_action_config(
    selector: BestSelector,
    tool: ToolType,
    params: GenerateParams,
) -> Mapping[str, object]:
    """Generate the action config for clipboard use.

    Args:
        selector: The best-effort selector (used by element-targeting tools).
        tool: The target tool type.
        params: Generation parameters.

    Returns:
        A configuration dictionary for the action step.
    """
    match tool:
        case ToolType.CLICK:
            return build_inline_selector(selector)
        case ToolType.INPUT_TEXT | ToolType.SET_TEXT:
            return build_inline_selector(selector, text=params.text)
        case ToolType.WAIT:
            return build_wait_config(params.duration_ms)
        case _:
            assert_never(tool)


# ---------------------------------------------------------------------------
# Internal config builders
# ---------------------------------------------------------------------------


def build_inline_selector(
    selector: BestSelector,
    *,
    text: str = "",
) -> dict[str, object]:
    """Build a config with inline selector fields for a UI tool.

    Selector priority:
        1. ``automation_id`` — most stable, use as the primary field.
        2. ``name`` + ``control_type`` — when ``automation_id`` is absent.
        3. ``class_name`` + ``control_type`` — fallback.

    ``control_type`` is included when available **and** not ``Custom``.
    Numeric UIA ids from real captures (``"50000"``) are translated to
    names — the runtime rejects raw numbers.

    Args:
        selector: The best-effort selector for a UI element.
        text: Optional text parameter (for input_text / set_text).

    Returns:
        A configuration dictionary with inline selector fields.
    """
    from smithcore.windows.selector_rank import control_type_display

    config: dict[str, object] = {}

    if selector.automation_id:
        config["automation_id"] = selector.automation_id

    if selector.name:
        config["name"] = selector.name

    control_type = control_type_display(selector.control_type)
    if control_type and control_type != "Custom":
        config["control_type"] = control_type

    if selector.class_name:
        config["class_name"] = selector.class_name

    if text:
        config["text"] = text

    return config


def generate_nodes_from_config(
    config: Mapping[str, object],
    tool: ToolType,
    params: GenerateParams,
) -> list[FlowNode]:
    """Generate a :class:`FlowNode` from an already-ranked selector config.

    Same tool mapping as :func:`generate_nodes`, but the selector fields
    come from :mod:`smithcore.windows.selector_rank` (minimal + verified
    unique) instead of the maximal all-fields dump.

    Args:
        config: Ranked inline selector fields (no ``text`` key — it comes
            from *params*).
        tool: The target tool type.
        params: Generation parameters (text, duration).

    Returns:
        A list of :class:`FlowNode` objects forming the tool sequence.
    """
    match tool:
        case ToolType.CLICK:
            return [FlowNode(tool="windows.click", args=dict(config))]
        case ToolType.INPUT_TEXT:
            return [
                FlowNode(
                    tool="windows.input_text",
                    args={**dict(config), "text": params.text},
                )
            ]
        case ToolType.SET_TEXT:
            return [
                FlowNode(
                    tool="windows.set_text",
                    args={**dict(config), "text": params.text},
                )
            ]
        case ToolType.WAIT:
            return [
                FlowNode(
                    tool="windows.wait",
                    args=build_wait_config(params.duration_ms),
                )
            ]
        case _:
            assert_never(tool)


def build_wait_config(duration_ms: int) -> dict[str, int]:
    """Build a ``windows.wait`` config.

    Args:
        duration_ms: How long to wait in milliseconds (mapped to
            ``timeout_ms`` which is the field ``windows.wait`` accepts).

    Returns:
        A configuration dictionary for ``windows.wait``.
    """
    return {"timeout_ms": duration_ms}
