"""SetTextTool — replace the text of a Windows UI element."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element

# Win32 message to set text in a control.
_WM_SETTEXT: int = 0x000C


def _send_wm_settext(hwnd: int, text: str) -> None:
    """Send WM_SETTEXT to a window handle via ctypes."""
    import ctypes
    import ctypes.wintypes

    ctypes.windll.user32.SendMessageW(hwnd, _WM_SETTEXT, 0, text)


class SetTextTool(AbstractTool):
    """Set a UI element's text programmatically.

    Tries the UIA IValueProvider first; falls back to the Win32
    ``WM_SETTEXT`` message for legacy controls (e.g. Notepad).
    """

    @property
    def name(self) -> str:
        return "windows.set_text"

    @property
    def description(self) -> str:
        return (
            "Sets the text of a UI element programmatically. "
            "Use to replace an input field's entire value."
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to set"},
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
            "required": ["text"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        text = config.get("text")
        if not isinstance(text, str):
            raise InvalidInput(
                "Missing required parameter: text (expected a string)",
                param="text",
                input_value=text,
            )

        element = await resolve_element(config)
        if element is None:
            raise ElementNotFound(
                "No element found: provide selector fields "
                "(name, automation_id, control_type, class_name, pid)",
                selector=config,
            )

        last_error: Exception | None = None

        # 1. Try UIA IValueProvider (WPF / UWP controls).
        try:
            pattern = await run_blocking(element.GetValuePattern)
            if pattern is not None:
                await run_blocking(pattern.SetValue, text)
                return {"status": "set", "text": text, "method": "value_pattern"}
        except Exception as exc:
            last_error = exc  # element does not support ValuePattern; try fallback

        # 2. Try WM_SETTEXT via the element's HWND (Win32 controls).
        try:
            hwnd: int | None = await run_blocking(getattr,
                element,
                "NativeWindowHandle",
                None,
            )
            if hwnd:
                await run_blocking(_send_wm_settext, hwnd, text)
                return {"status": "set", "text": text, "method": "wm_settext"}
        except Exception as exc:
            last_error = exc

        raise PlatformError(
            "Element does not support programmatic text setting",
            source=last_error,
        ) from last_error
