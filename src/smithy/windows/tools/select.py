"""SelectTool — select an item in a dropdown, combobox, or list."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element


class SelectTool(AbstractTool):
    """Select a selectable item via UIA SelectionItemPattern.

    The selector should identify the *item* (e.g. its ``name``), scoped by
    ``pid`` (or parent identifiers) to the right window. Raises
    ``PlatformError`` when the element does not support selection.
    """

    @property
    def name(self) -> str:
        return "windows.select"

    @property
    def description(self) -> str:
        return "Selects an item in a dropdown, combobox, or list"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Item name to select"},
                "automation_id": {"type": "string", "description": "UI Automation identifier"},
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
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
        try:
            await run_blocking(_select_item, element)
        except (ElementNotFound, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError(
                "Element does not support selection (SelectionItemPattern)",
                source=exc,
            ) from exc
        return {"status": "selected"}


def _select_item(element: Any) -> None:
    """Select the item (runs in an executor)."""
    element.GetSelectionItemPattern().Select()
