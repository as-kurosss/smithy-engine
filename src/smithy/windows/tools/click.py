"""ClickTool — click a Windows UI element or screen coordinates."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.element import SafeUIElement
from smithy.windows.tools._resolve import resolve_element

_BUTTONS = ("left", "right")
_CLICKS = (1, 2)


class ClickTool(AbstractTool):
    """Perform a click on a UI element by inline selector or coordinates."""

    @property
    def name(self) -> str:
        return "windows.click"

    @property
    def description(self) -> str:
        return "Performs a click on a UI element by inline selector or coordinates"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Element name to find",
                },
                "automation_id": {
                    "type": "string",
                    "description": "UI Automation identifier",
                },
                "control_type": {
                    "type": "string",
                    "description": "Control type",
                },
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {
                    "type": "integer",
                    "description": "Process ID filter",
                },
                "button": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "default": "left",
                    "description": "Mouse button",
                },
                "clicks": {
                    "type": "integer",
                    "enum": [1, 2],
                    "default": 1,
                    "description": "Single (1) or double (2) click",
                },
                "x": {
                    "type": "integer",
                    "description": "Screen X (coordinate click; needs 'y')",
                },
                "y": {
                    "type": "integer",
                    "description": "Screen Y (coordinate click; needs 'x')",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        button = config.get("button", "left")
        if not isinstance(button, str) or button not in _BUTTONS:
            raise InvalidInput(
                f"Invalid 'button': expected one of {_BUTTONS}",
                param="button",
                input_value=button,
            )
        clicks = config.get("clicks", 1)
        if isinstance(clicks, bool) or clicks not in _CLICKS:
            raise InvalidInput(
                f"Invalid 'clicks': expected one of {_CLICKS}",
                param="clicks",
                input_value=clicks,
            )

        try:
            if "x" in config or "y" in config:
                # Explicit coordinates win over selector fields.
                x = config.get("x")
                y = config.get("y")
                if (
                    isinstance(x, bool)
                    or not isinstance(x, int)
                    or isinstance(y, bool)
                    or not isinstance(y, int)
                ):
                    raise InvalidInput(
                        "Invalid 'x'/'y': expected integers",
                        param="x",
                        input_value={"x": x, "y": y},
                    )
                await run_blocking(_click_at, x, y, button, clicks)
            else:
                element = await resolve_element(config)
                if element is None:
                    raise ElementNotFound(
                        "No element found: provide selector fields "
                        "(name, automation_id, control_type, class_name, pid) "
                        "or coordinates ('x', 'y')",
                        selector=config,
                    )
                await run_blocking(_click_element, element, button, clicks)
        except (InvalidInput, ElementNotFound):
            raise
        except Exception as exc:
            raise PlatformError(f"Click failed: {exc}", source=exc) from exc

        return {"status": "clicked", "button": button, "clicks": clicks}


def _click_at(x: int, y: int, button: str, clicks: int) -> None:
    """Click screen coordinates (runs in an executor).

    Multi-clicks drop the post-click wait on every click but the last so
    the OS recognizes the sequence as a double-click (default 0.5 s
    pacing would exceed the system double-click time).
    """
    import uiautomation as auto

    click = auto.RightClick if button == "right" else auto.Click
    for index in range(clicks):
        wait = 0.5 if index == clicks - 1 else 0.0
        click(x, y, waitTime=wait)


def _click_element(element: Any, button: str, clicks: int) -> None:
    """Click a resolved element (runs in an executor)."""
    target = element.element if isinstance(element, SafeUIElement) else element
    if button == "left" and clicks == 2:
        target.DoubleClick()
    elif button == "right":
        for index in range(clicks):
            wait = 0.5 if index == clicks - 1 else 0.0
            target.RightClick(waitTime=wait)
    else:
        target.Click()
