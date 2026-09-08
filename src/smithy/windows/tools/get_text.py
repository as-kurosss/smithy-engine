"""GetTextTool — read the visible text of a UI element."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element


class GetTextTool(AbstractTool):
    """Read an element's text for assertions and data extraction.

    Tries the UIA ValuePattern first (edit fields, WPF/UWP controls),
    then falls back to the element's ``Name`` (labels, static text).
    """

    @property
    def name(self) -> str:
        return "windows.get_text"

    @property
    def description(self) -> str:
        return "Reads the visible text of a UI element"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Element name to find"},
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
            text = await run_blocking(_read_text, element)
        except Exception as exc:
            raise PlatformError(f"Reading element text failed: {exc}", source=exc) from exc
        return {"status": "read", "text": text}


def _read_text(element: Any) -> str:
    """Read ValuePattern, falling back to Name (runs in an executor)."""
    try:
        pattern = element.GetValuePattern()
    except Exception:
        pattern = None
    if pattern is not None:
        try:
            value = pattern.Value
        except Exception:
            value = None
        if value:
            return str(value)
    try:
        name = element.Name
    except Exception:
        name = None
    return str(name) if name else ""
