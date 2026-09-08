"""Tool-level retries for transient UI failures.

A selector that misses on the first poll often matches a second later —
wrapping a tool in :class:`RetryTool` retries ``ElementNotFound`` (or any
chosen exception) a few times with a pause in between, instead of failing
the whole transaction at once::

    bot = Smithy(tools=[RetryTool(ClickTool(), attempts=3, delay_ms=500)])
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.core.tool import AbstractTool, Tool


class RetryTool(AbstractTool):
    """Wrap a :class:`Tool` and retry it on selected exceptions.

    Register the wrapper *instead of* the original tool — it delegates
    ``name``/``description``/``schema`` so the bot sees a single tool.

    Args:
        tool: The tool to wrap.
        attempts: Total tries including the first one (>= 1).
        delay_ms: Pause between attempts (not after the last one).
        jitter: Fraction of *delay_ms* added/subtracted randomly
            (``0.2`` → ±20%). Spread retries out when many workers
            hammer the same flaky UI; ``0`` keeps the delay exact.
        retry_on: Exception types worth retrying. Never include
            :class:`Cancelled` — cancellation is control flow, not failure.

    Raises:
        InvalidInput: If ``attempts < 1``, ``delay_ms < 0``, ``jitter``
            is outside ``[0, 1]``, or ``retry_on`` is empty.
    """

    def __init__(
        self,
        tool: Tool,
        *,
        attempts: int = 3,
        delay_ms: float = 500,
        jitter: float = 0.0,
        retry_on: tuple[type[BaseException], ...] = (ElementNotFound,),
    ) -> None:
        if attempts < 1:
            raise InvalidInput(
                f"attempts must be >= 1, got {attempts!r}", param="attempts", input_value=attempts
            )
        if delay_ms < 0:
            raise InvalidInput(
                f"delay_ms must be >= 0, got {delay_ms!r}", param="delay_ms", input_value=delay_ms
            )
        if jitter < 0 or jitter > 1:
            raise InvalidInput(
                f"jitter must be in [0, 1], got {jitter!r}", param="jitter", input_value=jitter
            )
        if not retry_on:
            raise InvalidInput("retry_on must list at least one exception type", param="retry_on")
        self._tool = tool
        self._attempts = attempts
        self._delay_ms = delay_ms
        self._jitter = jitter
        self._retry_on = retry_on

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def description(self) -> str:
        return self._tool.description

    def schema(self) -> dict[str, Any]:
        return self._tool.schema()

    async def execute(self, config: dict[str, Any]) -> Any:
        """Run the wrapped tool, retrying *retry_on* errors."""
        last_error: BaseException | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._tool.execute(config)
            except self._retry_on as exc:
                last_error = exc
                if attempt < self._attempts:
                    delay = self._delay_ms
                    if self._jitter:
                        spread = delay * self._jitter
                        delay = delay - spread + 2 * spread * random.random()
                    await asyncio.sleep(delay / 1000)
        if last_error is None:
            raise RuntimeError("unreachable: retry loop exits only via return or exception")
        raise last_error
