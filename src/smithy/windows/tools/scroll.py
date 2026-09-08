"""ScrollTool — scroll with the mouse wheel over an element or coordinates."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_point

_DIRECTIONS = ("up", "down")


class ScrollTool(AbstractTool):
    """Scroll with the mouse wheel.

    When a target is given (selector or ``x``/``y``) the mouse moves there
    first so the right pane scrolls; otherwise the wheel turns at the
    current mouse position.
    """

    @property
    def name(self) -> str:
        return "windows.scroll"

    @property
    def description(self) -> str:
        return "Scrolls with the mouse wheel over a UI element or coordinates"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "default": "down",
                    "description": "Scroll direction",
                },
                "wheel_clicks": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 3,
                    "description": "Number of wheel ticks",
                },
                "name": {"type": "string", "description": "Element name to find"},
                "automation_id": {"type": "string", "description": "UI Automation identifier"},
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
                "x": {"type": "integer", "description": "Screen X (needs 'y')"},
                "y": {"type": "integer", "description": "Screen Y (needs 'x')"},
            },
            "required": [],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        direction = config.get("direction", "down")
        if not isinstance(direction, str) or direction not in _DIRECTIONS:
            raise InvalidInput(
                f"Invalid 'direction': expected one of {_DIRECTIONS}",
                param="direction",
                input_value=direction,
            )
        wheel_clicks = config.get("wheel_clicks", 3)
        if isinstance(wheel_clicks, bool) or not isinstance(wheel_clicks, int):
            raise InvalidInput(
                "Invalid 'wheel_clicks': expected an integer >= 1",
                param="wheel_clicks",
                input_value=wheel_clicks,
            )
        if wheel_clicks < 1:
            raise InvalidInput(
                "'wheel_clicks' must be >= 1",
                param="wheel_clicks",
                input_value=wheel_clicks,
            )

        point = await resolve_point(config)
        try:
            await run_blocking(_scroll_at, point, direction, wheel_clicks)
        except Exception as exc:
            raise PlatformError(f"Scroll failed: {exc}", source=exc) from exc
        return {"status": "scrolled", "direction": direction, "wheel_clicks": wheel_clicks}


def _scroll_at(point: tuple[int, int] | None, direction: str, wheel_clicks: int) -> None:
    """Turn the wheel (runs in an executor)."""
    import uiautomation as auto

    if point is not None:
        auto.MoveTo(point[0], point[1])
    if direction == "up":
        auto.WheelUp(wheel_clicks)
    else:
        auto.WheelDown(wheel_clicks)
