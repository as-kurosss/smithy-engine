"""HoverTool — move the mouse over a UI element or coordinates."""

from __future__ import annotations

from typing import Any

from smithcore.core.blocking import run_blocking
from smithcore.core.errors import ElementNotFound, PlatformError
from smithcore.core.tool import AbstractTool
from smithcore.windows.tools._resolve import resolve_point


class HoverTool(AbstractTool):
    """Move the mouse over a target (menus, tooltips, hover-reveal controls)."""

    @property
    def name(self) -> str:
        return "windows.hover"

    @property
    def description(self) -> str:
        return "Moves the mouse over a UI element or screen coordinates"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
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
        point = await resolve_point(config)
        if point is None:
            raise ElementNotFound(
                "No hover target: provide selector fields "
                "(name, automation_id, control_type, class_name, pid) "
                "or coordinates ('x', 'y')",
                selector=config,
            )
        try:
            await run_blocking(_move_to, point[0], point[1])
        except Exception as exc:
            raise PlatformError(f"Hover failed: {exc}", source=exc) from exc
        return {"status": "hovered", "x": point[0], "y": point[1]}


def _move_to(x: int, y: int) -> None:
    """Move the mouse (runs in an executor)."""
    import uiautomation as auto

    auto.MoveTo(x, y)
