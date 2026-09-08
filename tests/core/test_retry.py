"""Tests for smithy.core.retry — RetryTool."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.core.retry import RetryTool
from smithy.core.tool import AbstractTool


class FlakyTool(AbstractTool):
    """Fails *failures* times with ElementNotFound, then succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky.tool"

    @property
    def description(self) -> str:
        return "Flaky tool."

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"x": {"type": "integer"}}}

    async def execute(self, config: dict[str, Any]) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise ElementNotFound(f"miss #{self.calls}")
        return "found"


class TestRetryTool:
    @pytest.mark.asyncio
    async def test_succeeds_after_retries(self) -> None:
        inner = FlakyTool(failures=2)
        tool = RetryTool(inner, attempts=3, delay_ms=0)
        assert await tool.execute({}) == "found"
        assert inner.calls == 3

    @pytest.mark.asyncio
    async def test_raises_last_error_when_exhausted(self) -> None:
        inner = FlakyTool(failures=10)
        tool = RetryTool(inner, attempts=3, delay_ms=0)
        with pytest.raises(ElementNotFound, match="miss #3"):
            await tool.execute({})
        assert inner.calls == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_passes_through(self) -> None:
        class StrictTool(AbstractTool):
            @property
            def name(self) -> str:
                return "strict.tool"

            @property
            def description(self) -> str:
                return "Strict."

            async def execute(self, config: dict[str, Any]) -> Any:
                raise InvalidInput("bad params")

        tool = RetryTool(StrictTool(), attempts=3, delay_ms=0)
        with pytest.raises(InvalidInput, match="bad params"):
            await tool.execute({})

    @pytest.mark.asyncio
    async def test_sleeps_between_attempts_only(self) -> None:
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        inner = FlakyTool(failures=10)
        tool = RetryTool(inner, attempts=3, delay_ms=250)
        original = asyncio.sleep
        try:
            asyncio.sleep = fake_sleep  # type: ignore[assignment]
            with pytest.raises(ElementNotFound):
                await tool.execute({})
        finally:
            asyncio.sleep = original
        assert sleeps == [0.25, 0.25]

    def test_delegates_metadata(self) -> None:
        inner = FlakyTool(failures=0)
        tool = RetryTool(inner)
        assert tool.name == "flaky.tool"
        assert tool.description == "Flaky tool."
        assert tool.schema() == inner.schema()

    def test_invalid_arguments_rejected(self) -> None:
        inner = FlakyTool(failures=0)
        with pytest.raises(InvalidInput, match="attempts"):
            RetryTool(inner, attempts=0)
        with pytest.raises(InvalidInput, match="delay_ms"):
            RetryTool(inner, delay_ms=-1)
        with pytest.raises(InvalidInput, match="jitter"):
            RetryTool(inner, jitter=1.5)
        with pytest.raises(InvalidInput, match="retry_on"):
            RetryTool(inner, retry_on=())
