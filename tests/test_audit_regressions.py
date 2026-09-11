"""Regression tests for the audit fixes (heavy + medium defects)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smithcore.core.errors import ElementNotFound, InvalidInput
from smithcore.core.tool import tool
from smithcore.windows.selector import ElementSelector
from smithcore.windows.tools._resolve import build_selector, resolve_element
from smithcore.windows.tools.screenshot import _pil_format
from smithcore.windows.tools.selector_capture.cli import main
from smithcore.windows.tools.selector_capture.generate import build_wait_config


def _ctrl(*, name: str = "OK", pid: int = 42) -> MagicMock:
    ctrl = MagicMock()
    ctrl.Name = name
    ctrl.ProcessId = pid
    ctrl.AutomationId = ""
    ctrl.ControlType = 50000
    ctrl.ClassName = ""
    return ctrl


class TestSelectorPidScope:
    def test_name_and_pid_combine_with_and(self) -> None:
        sel = ElementSelector().with_name("OK").with_pid(42)
        compare = sel._build_compare()
        assert compare(_ctrl(name="OK", pid=42), 0) is True
        assert compare(_ctrl(name="OK", pid=7), 0) is False
        assert compare(_ctrl(name="Cancel", pid=42), 0) is False

    def test_builders_do_not_mutate_original(self) -> None:
        base = ElementSelector()
        derived = base.with_name("OK").with_pid(42)
        assert base.name is None
        assert base.pid is None
        assert derived.name == "OK"
        assert derived.pid == 42


class TestElementKeyContract:
    @pytest.mark.asyncio
    async def test_resolve_element_rejects_element_key(self) -> None:
        with pytest.raises(InvalidInput, match="element_key"):
            await resolve_element({"element_key": "my_elem"})

    def test_click_schema_has_no_element_key(self) -> None:
        from smithcore.windows.tools.click import ClickTool

        assert "element_key" not in ClickTool().schema()["properties"]

    def test_unknown_control_type_rejected(self) -> None:
        with pytest.raises(InvalidInput, match="control_type"):
            build_selector({"control_type": "NoSuchType"})


class TestWaitTool:
    @pytest.mark.asyncio
    async def test_returns_true_when_found(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        with patch.object(ElementSelector, "find_from_desktop", return_value=MagicMock()):
            assert await WaitTool().execute({"timeout_ms": 1000, "name": "x"}) is True

    @pytest.mark.asyncio
    async def test_returns_false_when_missing(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        with patch.object(
            ElementSelector,
            "find_from_desktop",
            side_effect=ElementNotFound("nope"),
        ):
            result = await WaitTool().execute({"timeout_ms": 60, "interval_ms": 50, "name": "x"})
            assert result is False

    @pytest.mark.asyncio
    async def test_rejects_non_integer_timeout(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        with pytest.raises(InvalidInput):
            await WaitTool().execute({"timeout_ms": "soon"})


class TestToolValidation:
    @pytest.mark.asyncio
    async def test_process_rejects_non_string_action(self) -> None:
        from smithcore.windows.tools.process import ProcessTool

        with pytest.raises(InvalidInput, match="action"):
            await ProcessTool().execute({"action": 123})

    @pytest.mark.asyncio
    async def test_process_stop_rejects_string_pid(self) -> None:
        from smithcore.windows.tools.process import ProcessTool

        with pytest.raises(InvalidInput, match="pid"):
            await ProcessTool().execute({"action": "stop", "pid": "123"})

    @pytest.mark.asyncio
    async def test_delay_rejects_bool(self) -> None:
        from smithcore.windows.tools.delay import DelayTool

        with pytest.raises(InvalidInput):
            await DelayTool().execute({"duration_ms": True})

    @pytest.mark.asyncio
    async def test_input_text_rejects_non_string(self) -> None:
        from smithcore.windows.tools.input_text import InputTextTool

        with pytest.raises(InvalidInput, match="text"):
            await InputTextTool().execute({"text": 123})

    @pytest.mark.asyncio
    async def test_set_text_rejects_non_string(self) -> None:
        from smithcore.windows.tools.set_text import SetTextTool

        with pytest.raises(InvalidInput, match="text"):
            await SetTextTool().execute({"text": 123, "name": "x"})

    @pytest.mark.asyncio
    async def test_keyboard_maps_unknown_key_to_invalid_input(self) -> None:
        from smithcore.windows.tools.keyboard import KeyboardTool

        with (
            patch(
                "smithcore.windows.tools.keyboard._send",
                side_effect=ValueError("Unknown key: 'X'"),
            ),
            pytest.raises(InvalidInput, match="Unknown key"),
        ):
            await KeyboardTool().execute({"keys": "[X!]"})


class TestCliDispatch:
    def test_single_dispatches_with_tool_defaults(self, tmp_path: Any) -> None:
        from smithcore.windows.tools.selector_capture.generate import ToolType

        out = str(tmp_path / "sel.json")
        with patch("smithcore.windows.tools.selector_capture.cli.run_single_mode") as run:
            main(["single", "-o", out])
        run.assert_called_once_with(
            output=out,
            description=None,
            tool_type=ToolType.CLICK,
            text="",
            duration_ms=1000,
            clip=False,
        )

    def test_series_dispatches_without_tool_arg(self, tmp_path: Any) -> None:
        out = str(tmp_path / "rec.json")
        with patch("smithcore.windows.tools.selector_capture.cli.run_series_mode") as run:
            main(["series", "-o", out])
        run.assert_called_once_with(output=out)

    def test_record_dispatches_without_tool_arg(self, tmp_path: Any) -> None:
        out = str(tmp_path / "flow.json")
        with patch("smithcore.windows.tools.selector_capture.cli.run_record_mode") as run:
            main(["record", "-o", out])
        run.assert_called_once_with(output=out)


class TestWaitConfigGeneration:
    def test_build_wait_config_matches_tool_schema(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        cfg = build_wait_config(2000)
        assert cfg == {"timeout_ms": 2000}
        assert "duration_ms" not in cfg
        assert "timeout_ms" in WaitTool().schema()["properties"]


class TestScreenshotFormat:
    def test_pil_format_mapping(self) -> None:
        assert _pil_format("png") == "PNG"
        assert _pil_format("jpg") == "JPEG"
        assert _pil_format("JPG") == "JPEG"


class TestToolDecorator:
    @pytest.mark.asyncio
    async def test_sync_function_supported(self) -> None:
        @tool("sync.greet")
        def greet(config: dict[str, Any]) -> dict[str, str]:
            return {"message": "hi"}

        assert await greet.execute({}) == {"message": "hi"}

    @pytest.mark.asyncio
    async def test_async_function_still_supported(self) -> None:
        @tool("async.greet")
        async def greet(config: dict[str, Any]) -> dict[str, str]:
            return {"message": "hi"}

        assert await greet.execute({}) == {"message": "hi"}
