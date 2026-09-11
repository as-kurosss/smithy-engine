"""DelayTool — simple pause for bot-level delays."""

from __future__ import annotations

import asyncio
from typing import Any

from smithcore.core.errors import InvalidInput
from smithcore.core.tool import AbstractTool


class DelayTool(AbstractTool):
    """Pause bot execution for a specified duration."""

    @property
    def name(self) -> str:
        return "windows.delay"

    @property
    def description(self) -> str:
        return "Pauses bot execution for a specified duration"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "duration_ms": {
                    "type": "integer",
                    "description": "Delay duration in milliseconds",
                    "minimum": 1,
                },
            },
            "required": ["duration_ms"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        duration_ms = config.get("duration_ms")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
            raise InvalidInput(
                "Missing or invalid 'duration_ms': expected a positive integer",
                param="duration_ms",
                input_value=duration_ms,
            )
        if duration_ms < 1:
            raise InvalidInput(
                "Missing or invalid 'duration_ms'",
                param="duration_ms",
                input_value=duration_ms,
            )

        await asyncio.sleep(duration_ms / 1000)
        return {"status": "delayed", "duration_ms": duration_ms}
