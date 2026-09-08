"""HighlightTool — flash a rectangle around an element (debugging aid)."""

from __future__ import annotations

import time
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element

_COLORS = {
    "red": 0x0000FF,
    "green": 0x00FF00,
    "blue": 0xFF0000,
    "yellow": 0x00FFFF,
}
# RedrawWindow flags: invalidate + erase + all children.
_RDW_INVALIDATE = 0x0001
_RDW_ERASE = 0x0004
_RDW_ALLCHILDREN = 0x0080


class HighlightTool(AbstractTool):
    """Draw a colored rectangle around an element for ``duration_ms``.

    Purely visual — invaluable when a selector matches but you are not
    sure *which* on-screen element it is.
    """

    @property
    def name(self) -> str:
        return "windows.highlight"

    @property
    def description(self) -> str:
        return "Flashes a colored rectangle around an element for debugging"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Element name"},
                "automation_id": {"type": "string", "description": "UI Automation identifier"},
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
                "color": {
                    "type": "string",
                    "enum": ["red", "green", "blue", "yellow"],
                    "description": "Outline color",
                },
                "duration_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000,
                    "description": "How long to show the rectangle (max 10 s)",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        element = await resolve_element(config)
        if element is None:
            raise ElementNotFound(
                "No element found: provide selector fields "
                "(name, automation_id, control_type, class_name, pid)",
                selector=config,
            )
        color = config.get("color", "red")
        if not isinstance(color, str) or color not in _COLORS:
            raise InvalidInput(
                f"Invalid 'color': expected one of {sorted(_COLORS)}",
                param="color",
                input_value=color,
            )
        duration_ms = config.get("duration_ms", 1000)
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
            or duration_ms > 10000
        ):
            raise InvalidInput(
                "Invalid 'duration_ms': expected an integer in [0, 10000]",
                param="duration_ms",
                input_value=duration_ms,
            )

        try:
            rect = await run_blocking(_bounding_rect, element)
            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
            await run_blocking(_flash_rect, left, top, right, bottom, _COLORS[color], duration_ms)
        except (InvalidInput, ElementNotFound, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError(f"Highlight failed: {exc}", source=exc) from exc
        return {"status": "highlighted", "color": color}


def _bounding_rect(element: Any) -> Any:
    """Read the bounding rectangle (runs in an executor)."""
    return element.BoundingRectangle


def _flash_rect(left: int, top: int, right: int, bottom: int, color: int, duration_ms: int) -> None:
    """Draw an outline rectangle on the screen DC, wait, then erase (runs in executor)."""
    import ctypes

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetDC(None)
    try:
        pen = gdi32.CreatePen(0, 3, color)  # PS_SOLID, 3px
        old_pen = gdi32.SelectObject(hdc, pen)
        old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(5))  # NULL_BRUSH
        gdi32.Rectangle(hdc, left, top, right, bottom)
        gdi32.SelectObject(hdc, old_pen)
        gdi32.SelectObject(hdc, old_brush)
        gdi32.DeleteObject(pen)
        time.sleep(duration_ms / 1000)
    finally:
        user32.ReleaseDC(None, hdc)
    user32.RedrawWindow(None, None, None, _RDW_INVALIDATE | _RDW_ERASE | _RDW_ALLCHILDREN)
