"""Tests for smithcore.facade — Smithcore facade class and ProcessHandle."""

from __future__ import annotations

from typing import Any

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.core.tool import AbstractTool
from smithcore.facade import ProcessHandle, Smithcore

# --- Stubs ---


class StubTool(AbstractTool):
    """Minimal tool for testing facade dispatch."""

    def __init__(
        self,
        tool_name: str = "stub.tool",
        output: Any = "ok",
    ) -> None:
        self._name = tool_name
        self._output = output

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Stub tool for testing."

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        return self._output


class ProcessStub(AbstractTool):
    """Stub for windows.process tool."""

    @property
    def name(self) -> str:
        return "windows.process"

    @property
    def description(self) -> str:
        return "Process stub."

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        return {"status": "started", "pid": 12345}


class ClickStub(AbstractTool):
    """Stub for windows.click tool."""

    @property
    def name(self) -> str:
        return "windows.click"

    @property
    def description(self) -> str:
        return "Click stub."

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        return {"status": "clicked"}


# --- Tests ---


class TestProcessHandle:
    def test_creation(self) -> None:
        h = ProcessHandle(pid=123, name="notepad.exe")
        assert h.pid == 123
        assert h.name == "notepad.exe"

    def test_equality(self) -> None:
        a = ProcessHandle(pid=1, name="a")
        b = ProcessHandle(pid=1, name="a")
        assert a == b


class TestSmithcoreInit:
    def test_empty(self) -> None:
        bot = Smithcore()
        assert bot._registry.list_tools() == []

    def test_with_tools(self) -> None:
        bot = Smithcore(tools=[StubTool(tool_name="a"), StubTool(tool_name="b")])
        assert sorted(bot._registry.list_tools()) == ["a", "b"]

    def test_register(self) -> None:
        bot = Smithcore()
        bot.register(StubTool(tool_name="x"))
        assert bot._registry.get("x") is not None


class TestSmithcoreProcess:
    @pytest.mark.asyncio
    async def test_process_returns_handle(self) -> None:
        bot = Smithcore(tools=[ProcessStub()])
        handle = await bot.process_run("notepad.exe")
        assert isinstance(handle, ProcessHandle)
        assert handle.pid == 12345
        assert handle.name == "notepad.exe"

    @pytest.mark.asyncio
    async def test_process_no_tool_raises(self) -> None:
        bot = Smithcore()
        with pytest.raises(InvalidInput, match="not found"):
            await bot.process_run("notepad.exe")


class TestSmithcoreClick:
    @pytest.mark.asyncio
    async def test_click_with_handle(self) -> None:
        bot = Smithcore(tools=[ClickStub()])
        handle = ProcessHandle(pid=42, name="app")
        result = await bot.click(handle, name="OK")
        assert result.status == "clicked"

    @pytest.mark.asyncio
    async def test_click_without_handle(self) -> None:
        bot = Smithcore(tools=[ClickStub()])
        result = await bot.click(name="OK")
        assert result.status == "clicked"

    @pytest.mark.asyncio
    async def test_click_with_element_key(self) -> None:
        bot = Smithcore(tools=[ClickStub()])
        result = await bot.click(element_key="my_elem")
        assert result.status == "clicked"

    @pytest.mark.asyncio
    async def test_click_pid_forwarded(self) -> None:
        """PID from handle should be in kwargs passed to tool."""
        received: dict[str, Any] = {}

        class CaptureClick(AbstractTool):
            @property
            def name(self) -> str:
                return "windows.click"

            @property
            def description(self) -> str:
                return "Capture."

            async def execute(
                self,
                config: dict[str, Any],
            ) -> Any:
                received.update(config)
                return {"status": "clicked"}

        bot = Smithcore(tools=[CaptureClick()])
        handle = ProcessHandle(pid=99, name="app")
        await bot.click(handle, name="Button")
        assert received["pid"] == 99
        assert received["name"] == "Button"

    @pytest.mark.asyncio
    async def test_click_explicit_pid_not_overridden(self) -> None:
        """If user passes pid explicitly, handle should not override it."""
        received: dict[str, Any] = {}

        class CaptureClick(AbstractTool):
            @property
            def name(self) -> str:
                return "windows.click"

            @property
            def description(self) -> str:
                return "Capture."

            async def execute(
                self,
                config: dict[str, Any],
            ) -> Any:
                received.update(config)
                return {"status": "clicked"}

        bot = Smithcore(tools=[CaptureClick()])
        handle = ProcessHandle(pid=99, name="app")
        await bot.click(handle, name="Button", pid=55)
        assert received["pid"] == 55  # explicit pid wins


class TestSmithcoreInputText:
    @pytest.mark.asyncio
    async def test_input_text_with_handle(self) -> None:
        stub = StubTool(tool_name="windows.input_text", output={"status": "typed"})
        bot = Smithcore(tools=[stub])
        handle = ProcessHandle(pid=42, name="app")
        result = await bot.input_text(handle, text="hello")
        assert result.status == "typed"

    @pytest.mark.asyncio
    async def test_input_text_without_handle(self) -> None:
        stub = StubTool(tool_name="windows.input_text", output={"status": "typed"})
        bot = Smithcore(tools=[stub])
        result = await bot.input_text(text="hello")
        assert result.status == "typed"

    @pytest.mark.asyncio
    async def test_input_text_pid_forwarded(self) -> None:
        received: dict[str, Any] = {}

        class Capture(AbstractTool):
            @property
            def name(self) -> str:
                return "windows.input_text"

            @property
            def description(self) -> str:
                return "Capture."

            async def execute(
                self,
                config: dict[str, Any],
            ) -> Any:
                received.update(config)
                return {"status": "typed"}

        bot = Smithcore(tools=[Capture()])
        handle = ProcessHandle(pid=99, name="app")
        await bot.input_text(handle, text="hi")
        assert received["pid"] == 99
        assert received["text"] == "hi"

    @pytest.mark.asyncio
    async def test_input_text_handle_first_positional(self) -> None:
        """handle must be the first positional arg, matching click/find."""
        received: dict[str, Any] = {}

        class Capture(AbstractTool):
            @property
            def name(self) -> str:
                return "windows.input_text"

            @property
            def description(self) -> str:
                return "Capture."

            async def execute(
                self,
                config: dict[str, Any],
            ) -> Any:
                received.update(config)
                return {"status": "typed"}

        bot = Smithcore(tools=[Capture()])
        handle = ProcessHandle(pid=99, name="app")
        await bot.input_text(handle, text="hello")
        # text must be a string, not a ProcessHandle
        assert isinstance(received["text"], str)
        assert received["pid"] == 99


class TestSmithcoreSetText:
    @pytest.mark.asyncio
    async def test_set_text_with_handle(self) -> None:
        stub = StubTool(tool_name="windows.set_text", output={"status": "set"})
        bot = Smithcore(tools=[stub])
        handle = ProcessHandle(pid=42, name="app")
        result = await bot.set_text(handle, text="hello")
        assert result.status == "set"

    @pytest.mark.asyncio
    async def test_set_text_without_handle(self) -> None:
        stub = StubTool(tool_name="windows.set_text", output={"status": "set"})
        bot = Smithcore(tools=[stub])
        result = await bot.set_text(text="hello")
        assert result.status == "set"

    @pytest.mark.asyncio
    async def test_set_text_handle_first_positional(self) -> None:
        received: dict[str, Any] = {}

        class Capture(AbstractTool):
            @property
            def name(self) -> str:
                return "windows.set_text"

            @property
            def description(self) -> str:
                return "Capture."

            async def execute(
                self,
                config: dict[str, Any],
            ) -> Any:
                received.update(config)
                return {"status": "set"}

        bot = Smithcore(tools=[Capture()])
        handle = ProcessHandle(pid=99, name="app")
        await bot.set_text(handle, text="hello")
        assert isinstance(received["text"], str)
        assert received["pid"] == 99


class TestSmithcoreGetElement:
    @pytest.mark.asyncio
    async def test_get_element_with_handle(self) -> None:
        output = {"element": {"name": "OK"}}
        bot = Smithcore(tools=[StubTool(tool_name="windows.get_element", output=output)])
        handle = ProcessHandle(pid=42, name="app")
        result = await bot.get_element(handle)
        assert result == output

    @pytest.mark.asyncio
    async def test_get_element_without_handle(self) -> None:
        output = {"element": {"name": "OK"}}
        bot = Smithcore(tools=[StubTool(tool_name="windows.get_element", output=output)])
        result = await bot.get_element(name="OK")
        assert result == output


class TestSmithcoreCall:
    @pytest.mark.asyncio
    async def test_call_custom_tool(self) -> None:
        bot = Smithcore(tools=[StubTool(tool_name="custom", output="done")])
        result = await bot.call("custom")
        assert result == "done"

    @pytest.mark.asyncio
    async def test_call_nonexistent_raises(self) -> None:
        bot = Smithcore()
        with pytest.raises(InvalidInput, match="not found"):
            await bot.call("no.such.tool")
