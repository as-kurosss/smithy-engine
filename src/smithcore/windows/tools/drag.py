"""DragTool — drag from one point to another (sliders, kanban, files)."""

from __future__ import annotations

from typing import Any

from smithcore.core.blocking import run_blocking
from smithcore.core.errors import InvalidInput, PlatformError
from smithcore.core.tool import AbstractTool
from smithcore.windows.tools._resolve import resolve_point

_SELECTOR_SUFFIXES = ("name", "automation_id", "control_type", "class_name", "pid")


def _endpoint_fields(prefix: str) -> dict[str, dict[str, Any]]:
    """Schema properties for one drag endpoint (``from_`` / ``to_``)."""
    fields: dict[str, dict[str, Any]] = {
        f"{prefix}x": {"type": "integer", "description": "Screen X"},
        f"{prefix}y": {"type": "integer", "description": "Screen Y"},
    }
    for suffix in _SELECTOR_SUFFIXES:
        kind = "integer" if suffix == "pid" else "string"
        fields[f"{prefix}{suffix}"] = {"type": kind, "description": f"Source {suffix}"}
    return fields


class DragTool(AbstractTool):
    """Drag-and-drop between two endpoints.

    Each endpoint is either explicit coordinates (``from_x``/``from_y``)
    or selector fields (``from_name`` + ``from_pid``, …). Both endpoints
    must resolve, otherwise the drag would start or land who-knows-where.
    """

    @property
    def name(self) -> str:
        return "windows.drag"

    @property
    def description(self) -> str:
        return "Drags from one UI point to another (elements or coordinates)"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {**_endpoint_fields("from_"), **_endpoint_fields("to_")},
            "required": [],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        start = await _resolve_endpoint(config, "from_")
        end = await _resolve_endpoint(config, "to_")
        if start is None or end is None:
            raise InvalidInput(
                "Drag needs both endpoints: coordinates or selector fields "
                "with 'from_' and 'to_' prefixes",
                param=None,
                input_value=config,
            )
        try:
            await run_blocking(_drag_drop, start, end)
        except Exception as exc:
            raise PlatformError(f"Drag failed: {exc}", source=exc) from exc
        return {"status": "dragged", "from": list(start), "to": list(end)}


async def _resolve_endpoint(config: dict[str, Any], prefix: str) -> tuple[int, int] | None:
    """Resolve one endpoint by stripping its prefix and reusing resolve_point."""
    sub = {key[len(prefix) :]: value for key, value in config.items() if key.startswith(prefix)}
    if not sub:
        return None
    return await resolve_point(sub)


def _drag_drop(start: tuple[int, int], end: tuple[int, int]) -> None:
    """Perform the drag (runs in an executor)."""
    import uiautomation as auto

    auto.DragDrop(start[0], start[1], end[0], end[1])
