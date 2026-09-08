"""Tests for smithy.windows.tools.get_table and control_action."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.windows.tools.control_action import ControlActionTool
from smithy.windows.tools.get_table import GetTableTool


def _ctrl(**attrs: Any) -> MagicMock:
    node = MagicMock()
    for key, value in attrs.items():
        setattr(node, key, value)
    return node


def _table_element() -> MagicMock:
    """DataGrid → header + rows (DataItems) → cells."""
    header = _ctrl(ControlTypeName="Header")
    header_cells = [
        _ctrl(ControlTypeName="HeaderItem", Name="user"),
        _ctrl(ControlTypeName="HeaderItem", Name="count"),
    ]
    header_cells[0].GetNextSiblingControl.return_value = header_cells[1]
    header_cells[1].GetNextSiblingControl.return_value = None
    header.GetFirstChildControl.return_value = header_cells[0]

    rows: list[MagicMock] = []
    for values in (("alice", "10"), ("bob", "20")):
        row = _ctrl(ControlTypeName="DataItem")
        cells = [_ctrl(ControlTypeName="Text", Name=name) for name in values]
        row.GetFirstChildControl.return_value = cells[0]
        cells[0].GetNextSiblingControl.return_value = cells[1]
        cells[1].GetNextSiblingControl.return_value = None
        rows.append(row)
    header.GetNextSiblingControl.return_value = rows[0]
    rows[0].GetNextSiblingControl.return_value = rows[1]
    rows[1].GetNextSiblingControl.return_value = None

    grid = _ctrl(ControlTypeName="DataGrid")
    grid.GetFirstChildControl.return_value = header
    return grid


class TestGetTable:
    @pytest.mark.asyncio
    async def test_extracts_header_and_rows(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.get_table.resolve_element",
                _fake_resolve(_table_element()),
            )
            result = await GetTableTool().execute({"automation_id": "grid"})
        assert result["columns"] == ["user", "count"]
        assert result["rows"] == [["alice", "10"], ["bob", "20"]]
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_descends_into_wrapper_container(self) -> None:
        inner = _table_element()
        wrapper = _ctrl(ControlTypeName="Pane")
        wrapper.GetFirstChildControl.return_value = inner
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.get_table.resolve_element",
                _fake_resolve(wrapper),
            )
            result = await GetTableTool().execute({"name": "dlg"})
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_no_rows_raises(self) -> None:
        empty = _ctrl(ControlTypeName="DataGrid")
        empty.GetFirstChildControl.return_value = None
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.get_table.resolve_element",
                _fake_resolve(empty),
            )
            with pytest.raises(PlatformError, match="No rows found"):
                await GetTableTool().execute({"name": "empty"})

    @pytest.mark.asyncio
    async def test_max_rows_validation(self) -> None:
        with pytest.raises(InvalidInput, match="max_rows"):
            await GetTableTool().execute({"name": "x", "max_rows": 0})

    @pytest.mark.asyncio
    async def test_missing_selector(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.get_table.resolve_element",
                _fake_resolve(None),
            )
            with pytest.raises(ElementNotFound):
                await GetTableTool().execute({"max_rows": 5})


class TestControlAction:
    @pytest.mark.asyncio
    async def test_invoke(self) -> None:
        element = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.control_action.resolve_element",
                _fake_resolve(element),
            )
            result = await ControlActionTool().execute({"action": "invoke", "name": "OK"})
        element.Invoke.assert_called_once_with()
        assert result["status"] == "performed"
        assert result["action"] == "invoke"

    @pytest.mark.asyncio
    async def test_toggle_reports_state(self) -> None:
        element = MagicMock()
        element.ToggleState = "On"
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.control_action.resolve_element",
                _fake_resolve(element),
            )
            result = await ControlActionTool().execute({"action": "toggle", "name": "cb"})
        element.Toggle.assert_called_once_with()
        assert result["toggle_state"] == "On"

    @pytest.mark.asyncio
    async def test_focus(self) -> None:
        element = MagicMock()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.control_action.resolve_element",
                _fake_resolve(element),
            )
            result = await ControlActionTool().execute({"action": "focus", "name": "edit"})
        element.SetFocus.assert_called_once_with()
        assert result["action"] == "focus"

    @pytest.mark.asyncio
    async def test_pattern_failure_wrapped(self) -> None:
        element = MagicMock()
        element.Invoke.side_effect = Exception("COMError: pattern unsupported")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "smithy.windows.tools.control_action.resolve_element",
                _fake_resolve(element),
            )
            with pytest.raises(PlatformError, match="pattern"):
                await ControlActionTool().execute({"action": "invoke", "name": "x"})

    @pytest.mark.asyncio
    async def test_invalid_action(self) -> None:
        with pytest.raises(InvalidInput, match="action"):
            await ControlActionTool().execute({"action": "explode", "name": "x"})


def _fake_resolve(element: Any) -> Any:
    async def _resolve(config: dict[str, Any], **kwargs: Any) -> Any:
        return element

    return _resolve
