"""InputTextTool — type plain text into a UI element or focused window."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import InvalidInput
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element


def _send(text: str) -> None:
    """Send text via uiautomation.SendKeys."""
    import uiautomation as auto

    auto.SendKeys(text)


class InputTextTool(AbstractTool):
    """Type plain text into a UI element or the focused window.

    Can work with or without a target element:
    - With element: focuses it first, then types.
    - Without element: types into the currently focused window.

    Examples:
    - ``"Hello World"`` — type plain text
    - ``"CTRL"`` — type literal text "CTRL"
    """

    @property
    def name(self) -> str:
        return "windows.input_text"

    @property
    def description(self) -> str:
        return "Types plain text into a UI element or the focused window"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Plain text to type.",
                },
                "name": {"type": "string", "description": "Element name to find"},
                "automation_id": {
                    "type": "string",
                    "description": "UI Automation identifier",
                },
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
            },
            "required": ["text"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        raw = config.get("text")
        if not isinstance(raw, str) or not raw:
            raise InvalidInput(
                "Missing required parameter: text (expected a non-empty string)",
                param="text",
                input_value=raw,
            )

        element = await resolve_element(config)
        if element is not None:
            await run_blocking(element.SetFocus)

        await run_blocking(_send, raw)
        return {"status": "sent", "text": raw}
