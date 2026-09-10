"""ControlActionTool — drive UI elements via native UIA patterns.

Coordinate clicks require the window to be visible and focused; pattern
calls (``Invoke``, ``Toggle``, …) act on the element directly and keep
working when the window is covered or on another virtual desktop.
"""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element

_ACTIONS = ("invoke", "toggle", "expand", "collapse", "select", "focus")


class ControlActionTool(AbstractTool):
    """Perform a native UIA pattern action on an element.

    More reliable than a coordinate click when a control exposes the
    matching pattern (buttons → ``invoke``, checkboxes → ``toggle``,
    tree/expanders → ``expand``/``collapse``).
    """

    @property
    def name(self) -> str:
        return "windows.control_action"

    @property
    def description(self) -> str:
        return (
            "Performs a native UIA pattern action (invoke, toggle, expand, "
            "collapse, select, focus) on a UI element without mouse clicks"
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "Pattern action to perform",
                },
                "name": {"type": "string", "description": "Element name"},
                "automation_id": {"type": "string", "description": "UI Automation identifier"},
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
            },
            "required": ["action"],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        action = config.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise InvalidInput(
                f"Invalid 'action': expected one of {_ACTIONS}",
                param="action",
                input_value=action,
            )
        element = await resolve_element(config)
        if element is None:
            raise ElementNotFound(
                "No element found: provide selector fields "
                "(name, automation_id, control_type, class_name, pid)",
                selector=config,
            )
        try:
            extra = await run_blocking(_apply_action, element, action)
        except (InvalidInput, ElementNotFound, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError(
                f"Control action '{action}' failed — the element may not "
                f"support the pattern: {exc}",
                source=exc,
            ) from exc
        result: dict[str, Any] = {"status": "performed", "action": action, **extra}
        return result


def _pattern(element: Any, getter: str, action: str) -> Any:
    pattern = getattr(element, getter, None)
    if pattern is None:
        raise PlatformError(f"The element does not expose the pattern required for '{action}'")
    return pattern() if callable(pattern) else pattern


def _apply_action(element: Any, action: str) -> dict[str, Any]:
    """Run the pattern call (runs in an executor)."""
    if action == "invoke":
        _pattern(element, "GetInvokePattern", action).Invoke()
        return {}
    if action == "toggle":
        pattern = _pattern(element, "GetTogglePattern", action)
        pattern.Toggle()
        state = getattr(pattern, "ToggleState", None)
        return {"toggle_state": str(state) if state is not None else None}
    if action == "expand":
        _pattern(element, "GetExpandCollapsePattern", action).Expand()
        return {}
    if action == "collapse":
        _pattern(element, "GetExpandCollapsePattern", action).Collapse()
        return {}
    if action == "select":
        _pattern(element, "GetSelectionItemPattern", action).Select()
        return {}
    element.SetFocus()
    return {}
