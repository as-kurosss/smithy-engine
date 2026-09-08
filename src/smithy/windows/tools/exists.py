"""ExistsTool — fast boolean check whether a UI element is present."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import build_selector


class ExistsTool(AbstractTool):
    """Check element presence with a single lookup (no waiting, no raising).

    Unlike ``windows.wait``, this never polls and never raises
    ``ElementNotFound`` — it returns ``True``/``False`` immediately.
    ``PlatformError`` (e.g. UIA init failure) still propagates, since
    "not found" cannot be distinguished from "cannot look" then.
    """

    @property
    def name(self) -> str:
        return "windows.exists"

    @property
    def description(self) -> str:
        return "Checks whether a UI element exists right now (True/False)"

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
        selector = build_selector(config)
        if selector is None:
            raise InvalidInput(
                "No selector: provide at least one of "
                "(name, automation_id, control_type, class_name, pid)",
                param=None,
                input_value=config,
            )
        try:
            await run_blocking(selector.find_from_desktop)
        except ElementNotFound:
            return False
        return True
