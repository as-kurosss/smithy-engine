"""InputTextTool — type plain text into a UI element or focused window."""

from __future__ import annotations

from typing import Any

from smithcore.core.blocking import run_blocking
from smithcore.core.errors import InvalidInput, PlatformError
from smithcore.core.tool import AbstractTool
from smithcore.windows.tools._resolve import resolve_element
from smithcore.windows.tools.keyboard import send_literal_text


def _send(text: str) -> None:
    """Type *text* literally (SendInput Unicode, no SendKeys syntax)."""
    send_literal_text(text)


class InputTextTool(AbstractTool):
    """Type plain text into a UI element or the focused window.

    Can work with or without a target element:
    - With element: focuses it first, then types.
    - Without element: types into the currently focused window.

    Examples:
    - ``"Hello World"`` — type plain text
    - ``"CTRL"`` — type literal text "CTRL"
    """

    # Typed text is often a password: track the result as a secret so the
    # runner redacts it from logs/traces and keeps it out of snapshots.
    produces_secrets = True

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
            try:
                await run_blocking(element.SetFocus)
            except (InvalidInput, PlatformError):
                raise
            except Exception as exc:
                raise PlatformError(f"SetFocus failed before typing: {exc}", source=exc) from exc

        try:
            await run_blocking(_send, raw)
        except (InvalidInput, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError("SendKeys failed", source=exc) from exc
        # Do not echo the typed text: it may be a password. Length is enough
        # for automation checks; the runner tracks the value as a secret.
        return {"status": "sent", "length": len(raw)}
