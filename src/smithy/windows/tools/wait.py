"""WaitTool — wait for a Windows UI element to appear or disappear."""

from __future__ import annotations

import asyncio
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.selector import ElementSelector, parse_control_type


class WaitTool(AbstractTool):
    """Wait for a UI element to appear (or disappear) by polling."""

    @property
    def name(self) -> str:
        return "windows.wait"

    @property
    def description(self) -> str:
        return (
            "Waits for a Windows UI element to appear by polling "
            "at a fixed interval until found or timeout exceeded"
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Element name to match (supports wildcards: * and ?)",
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
                "timeout_ms": {
                    "type": "integer",
                    "description": "Maximum wait time in milliseconds",
                    "minimum": 1,
                    "default": 10000,
                },
                "interval_ms": {
                    "type": "integer",
                    "description": "Polling interval in milliseconds",
                    "minimum": 50,
                    "default": 500,
                },
                "wait_for": {
                    "type": "string",
                    "enum": ["appear", "disappear"],
                    "default": "appear",
                    "description": "Wait until the element appears or disappears",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        timeout_ms = config.get("timeout_ms", 10000)
        interval_ms = config.get("interval_ms", 500)

        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or isinstance(interval_ms, bool)
            or not isinstance(interval_ms, int)
        ):
            raise InvalidInput(
                "Invalid timeout_ms/interval_ms: expected integers",
                param="timeout_ms",
                input_value={"timeout_ms": timeout_ms, "interval_ms": interval_ms},
            )

        if interval_ms < 50:
            raise InvalidInput(
                "interval_ms must be >= 50",
                param="interval_ms",
                input_value=interval_ms,
            )

        if timeout_ms < 1:
            raise InvalidInput(
                "timeout_ms must be >= 1",
                param="timeout_ms",
                input_value=timeout_ms,
            )

        wait_for = config.get("wait_for", "appear")
        if wait_for not in ("appear", "disappear"):
            raise InvalidInput(
                "Invalid 'wait_for': expected 'appear' or 'disappear'",
                param="wait_for",
                input_value=wait_for,
            )

        if "control_type" in config:
            raw_ct = config["control_type"]
            if not isinstance(raw_ct, str) or parse_control_type(raw_ct) is None:
                raise InvalidInput(
                    f"Unknown control_type: {raw_ct!r}",
                    param="control_type",
                    input_value=raw_ct,
                )
        selector = ElementSelector()
        if "name" in config:
            selector = selector.with_name(config["name"])
        if "automation_id" in config:
            selector = selector.with_automation_id(config["automation_id"])
        if "control_type" in config:
            selector = selector.with_control_type(config["control_type"])
        if "class_name" in config:
            selector = selector.with_class_name(config["class_name"])
        if "pid" in config:
            selector = selector.with_pid(config["pid"])

        deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)
        interval = interval_ms / 1000
        while True:
            try:
                await run_blocking(selector.find_from_desktop)
                missing = False
            except ElementNotFound:
                missing = True
            except PlatformError:
                # Transient UIA hiccup — keep polling as if still present.
                missing = False

            if (wait_for == "disappear") == missing:
                return True

            if asyncio.get_running_loop().time() >= deadline:
                return False

            await asyncio.sleep(interval)
