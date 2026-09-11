"""Tests for smithcore.core.tool and smithcore.core.registry."""

from __future__ import annotations

from typing import Any

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.core.registry import ToolRegistry
from smithcore.core.tool import AbstractTool, Tool

# --- Stubs ---


class StubTool:
    """Minimal Tool implementation for testing."""

    def __init__(self, tool_name: str = "stub.tool", output: Any = "ok") -> None:
        self._name = tool_name
        self._output = output

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "A stub tool for testing."

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        return self._output


class FailTool:
    """Tool that always raises."""

    @property
    def name(self) -> str:
        return "stub.fail"

    @property
    def description(self) -> str:
        return "Always fails."

    def schema(self) -> dict[str, Any]:
        return {}

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        raise InvalidInput("deliberate failure")


class FakeAbstractTool(AbstractTool):
    """Concrete implementation of AbstractTool."""

    @property
    def name(self) -> str:
        return "fake.tool"

    @property
    def description(self) -> str:
        return "Fake abstract tool."

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        return "fake executed"


class TestToolProtocol:
    def test_stub_satisfies_protocol(self) -> None:
        tool = StubTool()
        assert isinstance(tool, Tool)

    def test_fail_tool_satisfies_protocol(self) -> None:
        tool = FailTool()
        assert isinstance(tool, Tool)

    def test_abstract_tool_satisfies_protocol(self) -> None:
        tool = FakeAbstractTool()
        assert isinstance(tool, Tool)

    @pytest.mark.asyncio
    async def test_stub_execute(self) -> None:
        tool = StubTool(output="hello")
        result = await tool.execute({})
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_fail_tool_execute(self) -> None:
        tool = FailTool()
        with pytest.raises(InvalidInput):
            await tool.execute({})


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        reg = ToolRegistry()
        tool = StubTool(tool_name="test.tool")
        reg.register(tool)
        assert reg.get("test.tool") is tool

    def test_get_nonexistent(self) -> None:
        reg = ToolRegistry()
        assert reg.get("no.such.tool") is None

    def test_list_tools_sorted(self) -> None:
        reg = ToolRegistry()
        reg.register(StubTool(tool_name="z.tool"))
        reg.register(StubTool(tool_name="a.tool"))
        reg.register(StubTool(tool_name="m.tool"))
        assert reg.list_tools() == ["a.tool", "m.tool", "z.tool"]

    @pytest.mark.asyncio
    async def test_execute_existing(self) -> None:
        reg = ToolRegistry()
        reg.register(StubTool(tool_name="echo", output=42))
        result = await reg.execute("echo", {})
        assert result == 42

    @pytest.mark.asyncio
    async def test_execute_nonexistent_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(InvalidInput, match="not found"):
            await reg.execute("no.such", {})

    @pytest.mark.asyncio
    async def test_execute_fail_tool(self) -> None:
        reg = ToolRegistry()
        reg.register(FailTool())
        with pytest.raises(InvalidInput, match="deliberate failure"):
            await reg.execute("stub.fail", {})

    def test_register_overwrites(self) -> None:
        reg = ToolRegistry()
        reg.register(StubTool(tool_name="x", output="first"))
        with pytest.warns(UserWarning, match="already registered"):
            reg.register(StubTool(tool_name="x", output="second"))
        assert reg.get("x") is not None
        # The second registration should overwrite
