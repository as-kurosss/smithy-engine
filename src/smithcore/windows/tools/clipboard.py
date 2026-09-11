"""ClipboardTool — read and write the system clipboard text."""

from __future__ import annotations

from typing import Any

from smithcore.core.blocking import run_blocking
from smithcore.core.errors import InvalidInput, PlatformError
from smithcore.core.tool import AbstractTool


class ClipboardTool(AbstractTool):
    """Get/set clipboard text for copy-paste flows.

    Requires ``pyperclip`` (included in ``smithcore[windows]``).
    """

    @property
    def name(self) -> str:
        return "windows.clipboard"

    @property
    def description(self) -> str:
        return "Reads or writes the system clipboard text"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set"],
                    "description": "Read ('get') or write ('set') the clipboard",
                },
                "text": {"type": "string", "description": "Text to put on the clipboard ('set')"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        action = config.get("action")
        if action not in ("get", "set"):
            raise InvalidInput(
                "Invalid 'action': expected 'get' or 'set'",
                param="action",
                input_value=action,
            )
        if action == "set":
            text = config.get("text")
            if not isinstance(text, str):
                raise InvalidInput(
                    "Invalid 'text': 'set' needs a string",
                    param="text",
                    input_value=text,
                )
        else:
            text = ""

        clipboard = _load_pyperclip()
        try:
            if action == "set":
                await run_blocking(clipboard.copy, text)
                return {"status": "set"}
            pasted = await run_blocking(clipboard.paste)
        except Exception as exc:
            raise PlatformError(f"Clipboard {action!r} failed: {exc}", source=exc) from exc
        return {"status": "read", "text": str(pasted) if pasted else ""}


def _load_pyperclip() -> Any:
    """Import pyperclip or raise a helpful error (patchable seam for tests)."""
    try:
        import pyperclip
    except ImportError as exc:
        raise PlatformError(
            "Clipboard requires 'pyperclip'. Install with: pip install smithcore[windows]",
            source=exc,
        ) from exc
    return pyperclip
