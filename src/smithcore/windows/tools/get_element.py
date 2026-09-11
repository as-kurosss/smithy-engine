"""GetElementTool — read attributes of a Windows UI element."""

from __future__ import annotations

from typing import Any

from smithcore.core.errors import ElementNotFound
from smithcore.core.tool import AbstractTool
from smithcore.windows.element import SafeUIElement
from smithcore.windows.tools._resolve import resolve_element


class GetElementTool(AbstractTool):
    """Read identifying attributes of a UI element."""

    @property
    def name(self) -> str:
        return "windows.get_element"

    @property
    def description(self) -> str:
        return (
            "Reads a UI element's attributes (name, control type, automation "
            "id, class name, pid, rect) and returns them as a dict"
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Element name to find"},
                "automation_id": {
                    "type": "string",
                    "description": "UI Automation identifier",
                },
                "control_type": {
                    "type": "string",
                    "description": "Control type",
                },
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
            },
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

        safe = SafeUIElement(element)
        info = await safe.get_info()
        return {"element": info}
