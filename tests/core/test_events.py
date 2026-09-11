"""Tests for smithcore.core.events — ToolEvent, EventBus, Middleware."""

from __future__ import annotations

from typing import Any

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.core.events import EventBus, ToolEvent
from smithcore.core.tool import AbstractTool
from smithcore.facade import SmithCore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StubTool(AbstractTool):
    def __init__(self, tool_name: str = "stub.tool", output: Any = "ok") -> None:
        self._name = tool_name
        self._output = output

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Stub."

    async def execute(self, config: dict[str, Any]) -> Any:
        return self._output


class FailTool(AbstractTool):
    @property
    def name(self) -> str:
        return "stub.fail"

    @property
    def description(self) -> str:
        return "Always fails."

    async def execute(self, config: dict[str, Any]) -> Any:
        raise InvalidInput("deliberate failure")


# ---------------------------------------------------------------------------
# ToolEvent
# ---------------------------------------------------------------------------


class TestToolEvent:
    def test_defaults(self) -> None:
        event = ToolEvent(tool_name="t", config={})
        assert event.tool_name == "t"
        assert event.config == {}
        assert event.result is None
        assert event.error is None
        assert event.duration_ms == 0.0
        assert event.metadata == {}

    def test_timestamp_is_utc(self) -> None:
        event = ToolEvent(tool_name="t", config={})
        assert event.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class TestEventBus:
    @pytest.mark.asyncio
    async def test_no_middlewares(self) -> None:
        bus = EventBus()
        event = ToolEvent(tool_name="t", config={})
        result = await bus.emit(event)
        assert result is event

    @pytest.mark.asyncio
    async def test_single_middleware_passthrough(self) -> None:
        bus = EventBus()

        async def echo(event: ToolEvent) -> ToolEvent:
            return event

        bus.add_middleware(echo)
        event = ToolEvent(tool_name="t", config={})
        result = await bus.emit(event)
        assert result is event

    @pytest.mark.asyncio
    async def test_middleware_can_transform(self) -> None:
        bus = EventBus()

        async def add_meta(event: ToolEvent) -> ToolEvent:
            event.metadata["injected"] = True
            return event

        bus.add_middleware(add_meta)
        event = ToolEvent(tool_name="t", config={})
        result = await bus.emit(event)
        assert result is not None
        assert result.metadata["injected"] is True

    @pytest.mark.asyncio
    async def test_none_stops_propagation(self) -> None:
        bus = EventBus()
        called = []

        async def stopper(event: ToolEvent) -> ToolEvent | None:
            called.append("stopper")
            return None

        async def second(event: ToolEvent) -> ToolEvent:
            called.append("second")
            return event

        bus.add_middleware(stopper)
        bus.add_middleware(second)
        result = await bus.emit(ToolEvent(tool_name="t", config={}))
        assert result is None
        assert called == ["stopper"]

    @pytest.mark.asyncio
    async def test_ordering(self) -> None:
        bus = EventBus()
        order = []

        async def first(event: ToolEvent) -> ToolEvent:
            order.append(1)
            return event

        async def second(event: ToolEvent) -> ToolEvent:
            order.append(2)
            return event

        bus.add_middleware(first)
        bus.add_middleware(second)
        await bus.emit(ToolEvent(tool_name="t", config={}))
        assert order == [1, 2]


# ---------------------------------------------------------------------------
# SmithCore middleware integration
# ---------------------------------------------------------------------------


class TestSmithCoreMiddleware:
    def test_add_middleware(self) -> None:
        bus = EventBus()

        async def noop(event: ToolEvent) -> ToolEvent:
            return event

        bus.add_middleware(noop)
        assert len(bus._middlewares) == 1

    @pytest.mark.asyncio
    async def test_middleware_receives_event(self) -> None:
        received = []

        async def collector(event: ToolEvent) -> ToolEvent:
            received.append(event)
            return event

        bot = SmithCore(tools=[StubTool()])
        bot.add_middleware(collector)
        await bot.call("stub.tool", x=1)

        assert len(received) == 1
        ev = received[0]
        assert ev.tool_name == "stub.tool"
        assert ev.config == {"x": 1}
        assert ev.result == "ok"
        assert ev.error is None
        assert ev.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_middleware_receives_error(self) -> None:
        received = []

        async def collector(event: ToolEvent) -> ToolEvent:
            received.append(event)
            return event

        bot = SmithCore(tools=[FailTool()])
        bot.add_middleware(collector)
        with pytest.raises(InvalidInput):
            await bot.call("stub.fail")

        assert len(received) == 1
        ev = received[0]
        assert ev.tool_name == "stub.fail"
        assert ev.error is not None
        assert "deliberate failure" in str(ev.error)

    @pytest.mark.asyncio
    async def test_multiple_middlewares(self) -> None:
        events = []

        async def a(event: ToolEvent) -> ToolEvent:
            events.append("a")
            return event

        async def b(event: ToolEvent) -> ToolEvent:
            events.append("b")
            return event

        bot = SmithCore(tools=[StubTool()])
        bot.add_middleware(a)
        bot.add_middleware(b)
        await bot.call("stub.tool")

        assert events == ["a", "b"]
