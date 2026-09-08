"""Shared helpers for resolving a UI element from a tool config."""

from __future__ import annotations

import contextlib
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.windows.selector import ElementSelector


def build_selector(config: dict[str, Any]) -> ElementSelector | None:
    """Build a selector from inline config fields, or None if none are present."""
    return ElementSelector.from_config(config)


async def resolve_point(config: dict[str, Any]) -> tuple[int, int] | None:
    """Resolve screen coordinates from ``x``/``y`` fields or a UI element.

    Explicit coordinates win over selector fields. Returns ``None`` when
    the config carries neither — tools that can act on the current mouse
    position (e.g. scroll) treat that as "right here".

    Raises:
        InvalidInput: If only one of ``x``/``y`` is an integer.
    """
    if "x" in config or "y" in config:
        x = config.get("x")
        y = config.get("y")
        if (
            isinstance(x, bool)
            or not isinstance(x, int)
            or isinstance(y, bool)
            or not isinstance(y, int)
        ):
            raise InvalidInput(
                "Invalid 'x'/'y': expected integers",
                param="x",
                input_value={"x": x, "y": y},
            )
        return (x, y)
    element = await resolve_element(config)
    if element is None:
        return None
    point: tuple[int, int] = await run_blocking(_element_center, element)
    return point


def _element_center(element: Any) -> tuple[int, int]:
    """Clickable point of *element*, falling back to the rect center."""
    point: Any = None
    with contextlib.suppress(Exception):
        point = element.GetClickablePoint()
    if point is not None:
        with contextlib.suppress(TypeError, IndexError, ValueError):
            return (int(point[0]), int(point[1]))
    rect = element.BoundingRectangle
    return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)


async def resolve_element(config: dict[str, Any], *, strict: bool = False) -> Any:
    """Resolve a UI element from inline selector fields.

    Args:
        config: Inline selector fields (name, automation_id, control_type,
            class_name, pid).
        strict: When ``True``, fail on ambiguous selectors that match more
            than one element (Playwright strict-mode equivalent) instead of
            silently taking the first match. Costs an extra bounded tree
            walk — a validation aid, not the default runtime path.

    Returns:
        A raw ``uiautomation`` control, or ``None`` if no selector
        fields were given.

    Raises:
        InvalidInput: If ``element_key`` is used, or (with ``strict``) the
            selector matches more than one element.
        ElementNotFound: If nothing matches (with ``strict``, raised here
            instead of inside ``find_from_desktop``).
    """
    if "element_key" in config:
        raise InvalidInput(
            "element_key is not supported: pass inline selector fields "
            "(name, automation_id, control_type, class_name, pid) instead",
            param="element_key",
            input_value=config.get("element_key"),
        )
    selector = build_selector(config)
    if selector is None:
        return None

    if strict:
        matches = await run_blocking(selector.count_from_desktop, 2)
        if matches == 0:
            raise ElementNotFound(
                "No element found matching selector",
                selector=config,
            )
        if matches > 1:
            raise InvalidInput(
                "Ambiguous selector: matches 2+ elements — "
                "narrow it (add automation_id or control_type)",
                param=None,
                input_value=config,
            )
    return await run_blocking(selector.find_from_desktop)
