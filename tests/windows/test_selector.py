"""Tests for smithy.windows.selector and windows tools."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.windows.selector import _CONTROL_TYPE_MAP, ElementSelector, parse_control_type


class TestElementSelector:
    def test_default_values(self) -> None:
        s = ElementSelector()
        assert s.pid is None
        assert s.name is None
        assert s.automation_id is None
        assert s.control_type is None
        assert s.class_name is None

    def test_builder_methods(self) -> None:
        s = (
            ElementSelector()
            .with_pid(1234)
            .with_name("OK")
            .with_automation_id("btn_ok")
            .with_control_type("Button")
            .with_class_name("MyClass")
        )
        assert s.pid == 1234
        assert s.name == "OK"
        assert s.automation_id == "btn_ok"
        assert s.control_type == "Button"
        assert s.class_name == "MyClass"

    def test_to_dict_partial(self) -> None:
        s = ElementSelector().with_name("Test").with_pid(42)
        d = s.to_dict()
        assert d == {"name": "Test", "pid": 42}
        assert "automation_id" not in d

    def test_to_dict_empty(self) -> None:
        s = ElementSelector()
        assert s.to_dict() == {}

    def test_find_first_no_match(self) -> None:
        s = ElementSelector().with_name("Nonexistent")
        mock_root = MagicMock()
        mock_auto = MagicMock()
        mock_auto.FindControl.return_value = None
        with pytest.raises(ElementNotFound):
            s.find_first(mock_root, mock_auto)

    def test_find_first_match(self) -> None:
        s = ElementSelector().with_name("OK")
        mock_element = MagicMock()
        mock_root = MagicMock()
        mock_auto = MagicMock()
        mock_auto.FindControl.return_value = mock_element
        result = s.find_first(mock_root, mock_auto)
        assert result is mock_element

    def test_find_first_uia_error(self) -> None:
        s = ElementSelector().with_name("OK")
        mock_root = MagicMock()
        mock_auto = MagicMock()
        mock_auto.FindControl.side_effect = Exception("COM error")
        with pytest.raises(PlatformError):
            s.find_first(mock_root, mock_auto)


class TestParseControlType:
    def test_button(self) -> None:
        assert parse_control_type("Button") == 50000

    def test_edit(self) -> None:
        assert parse_control_type("Edit") == 50004

    def test_text(self) -> None:
        assert parse_control_type("Text") == 50020

    def test_toolbar(self) -> None:
        assert parse_control_type("ToolBar") == 50021

    def test_window(self) -> None:
        assert parse_control_type("Window") == 50032

    def test_pane(self) -> None:
        assert parse_control_type("Pane") == 50033

    def test_separator(self) -> None:
        assert parse_control_type("Separator") == 50038

    def test_appbar(self) -> None:
        assert parse_control_type("AppBar") == 50040

    def test_case_insensitive(self) -> None:
        assert parse_control_type("button") == 50000
        assert parse_control_type("BUTTON") == 50000

    def test_unknown_returns_none(self) -> None:
        assert parse_control_type("NoSuchType") is None

    def test_matches_uiautomation_control_type(self) -> None:
        if sys.platform != "win32":
            pytest.skip("comtypes (uiautomation) imports COMError — Windows only")
        pytest.importorskip("uiautomation")
        import uiautomation as auto

        compound = {
            "checkbox": "CheckBox",
            "combobox": "ComboBox",
            "hyperlink": "Hyperlink",
            "listitem": "ListItem",
            "menubar": "MenuBar",
            "menuitem": "MenuItem",
            "progressbar": "ProgressBar",
            "radiobutton": "RadioButton",
            "scrollbar": "ScrollBar",
            "statusbar": "StatusBar",
            "tabitem": "TabItem",
            "toolbar": "ToolBar",
            "tooltip": "ToolTip",
            "treeitem": "TreeItem",
            "datagrid": "DataGrid",
            "dataitem": "DataItem",
            "splitbutton": "SplitButton",
            "headeritem": "HeaderItem",
            "titlebar": "TitleBar",
            "semanticzoom": "SemanticZoom",
            "appbar": "AppBar",
        }

        for type_name, expected_id in _CONTROL_TYPE_MAP.items():
            uia_name = compound.get(type_name, type_name.capitalize()) + "Control"
            uia_id = getattr(auto.ControlType, uia_name)
            assert expected_id == int(uia_id), type_name


class TestProcessTool:
    @pytest.mark.asyncio
    async def test_tool_metadata(self) -> None:
        from smithy.windows.tools.process import ProcessTool

        tool = ProcessTool()
        assert tool.name == "windows.process"
        assert "start" in tool.description.lower() or "process" in tool.description.lower()

    @pytest.mark.asyncio
    async def test_unknown_action(self) -> None:
        from smithy.windows.tools.process import ProcessTool

        tool = ProcessTool()
        with pytest.raises(InvalidInput, match="Unknown"):
            await tool.execute({"action": "reboot"})

    @pytest.mark.asyncio
    async def test_start_missing_command(self) -> None:
        from smithy.windows.tools.process import ProcessTool

        tool = ProcessTool()
        with pytest.raises(InvalidInput, match="command"):
            await tool.execute({"action": "start"})

    @pytest.mark.asyncio
    async def test_start_disallowed_command(self) -> None:
        from smithy.windows.tools.process import ProcessTool

        tool = ProcessTool()
        with pytest.raises(InvalidInput, match="not in the allowed list"):
            await tool.execute({"action": "start", "command": "cmd.exe"})

    @pytest.mark.asyncio
    async def test_stop_missing_pid_and_name(self) -> None:
        from smithy.windows.tools.process import ProcessTool

        tool = ProcessTool()
        with pytest.raises(InvalidInput, match="pid.*name"):
            await tool.execute({"action": "stop"})


class TestClickTool:
    def test_tool_metadata(self) -> None:
        from smithy.windows.tools.click import ClickTool

        tool = ClickTool()
        assert tool.name == "windows.click"
        assert isinstance(tool.schema(), dict)

    @pytest.mark.asyncio
    async def test_click_no_element_key_no_selector(self) -> None:
        from smithy.windows.tools.click import ClickTool

        tool = ClickTool()
        # Mock uiautomation to prevent real UIA calls
        mock_auto = MagicMock()
        mock_auto.GetRootControl.return_value = MagicMock()
        mock_auto.uiautomation.FindControl.return_value = None
        with (
            patch.dict("sys.modules", {"uiautomation": mock_auto}),
            pytest.raises((ElementNotFound, PlatformError)),
        ):
            await tool.execute({})
