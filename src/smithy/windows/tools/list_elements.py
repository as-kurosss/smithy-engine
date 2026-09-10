"""ListElementsTool — dump the direct children of an element."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element

_DEFAULT_MAX_ITEMS = 50


class ListElementsTool(AbstractTool):
    """List direct children (name/type/id) of an element.

    The workhorse for *discovering* automation IDs before writing the
    actual bot: point it at a window or panel, get the children with
    stable IDs, then target them precisely.
    """

    @property
    def name(self) -> str:
        return "windows.list_elements"

    @property
    def description(self) -> str:
        return "Lists direct child elements (name, control type, automation ID)"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Parent element name"},
                "automation_id": {"type": "string", "description": "UI Automation identifier"},
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
                "max_items": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Max children to return",
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
        max_items = config.get("max_items", _DEFAULT_MAX_ITEMS)
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise InvalidInput(
                "Invalid 'max_items': expected an integer >= 1",
                param="max_items",
                input_value=max_items,
            )

        def _collect_bounded() -> list[dict[str, Any]]:
            """Walk children until *max_items*, describing each in-apartment."""
            items: list[dict[str, Any]] = []
            try:
                child = element.GetFirstChildControl()
            except Exception:
                return items
            while child is not None and len(items) < max_items:
                items.append(_describe(child))
                try:
                    child = child.GetNextSiblingControl()
                except Exception:
                    break
            return items

        items = await run_blocking(_collect_bounded)
        return {"items": items, "count": len(items)}


def _describe(child: Any) -> dict[str, Any]:
    """Best-effort child description (never raises)."""
    info: dict[str, Any] = {}
    for attr in ("Name", "ClassName", "ControlTypeName", "AutomationId", "ProcessId"):
        try:
            value = getattr(child, attr, None)
        except Exception:
            value = None
        if value:
            info[_CAMEL_TO_SNAKE[attr]] = value
    info["has_children"] = bool(getattr(child, "HasChildren", False))
    return info


_CAMEL_TO_SNAKE = {
    "Name": "name",
    "ClassName": "class_name",
    "ControlTypeName": "control_type",
    "AutomationId": "automation_id",
    "ProcessId": "pid",
}
