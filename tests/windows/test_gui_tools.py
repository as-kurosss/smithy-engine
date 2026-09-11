"""Tests for the GUI batch: extended click/wait + 10 new tools + facade/factory."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smithcore.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithcore.windows.selector import ElementSelector


def _element(**attrs: Any) -> MagicMock:
    el = MagicMock()
    for key, value in attrs.items():
        setattr(el, key, value)
    return el


class TestClickExtended:
    @pytest.mark.asyncio
    async def test_rejects_bad_button(self) -> None:
        from smithcore.windows.tools.click import ClickTool

        with pytest.raises(InvalidInput, match="button"):
            await ClickTool().execute({"button": "middle", "x": 1, "y": 2})

    @pytest.mark.asyncio
    async def test_rejects_bad_clicks(self) -> None:
        from smithcore.windows.tools.click import ClickTool

        with pytest.raises(InvalidInput, match="clicks"):
            await ClickTool().execute({"clicks": 3, "x": 1, "y": 2})

    @pytest.mark.asyncio
    async def test_rejects_bool_clicks(self) -> None:
        from smithcore.windows.tools.click import ClickTool

        with pytest.raises(InvalidInput, match="clicks"):
            await ClickTool().execute({"clicks": True, "x": 1, "y": 2})

    @pytest.mark.asyncio
    async def test_coordinate_click_calls_helper(self) -> None:
        from smithcore.windows.tools.click import ClickTool

        with patch("smithcore.windows.tools.click._click_at", return_value=None) as click_at:
            result = await ClickTool().execute({"x": 10, "y": 20, "button": "right", "clicks": 2})
        click_at.assert_called_once_with(10, 20, "right", 2)
        assert result == {"status": "clicked", "button": "right", "clicks": 2}

    @pytest.mark.asyncio
    async def test_element_click_calls_helper(self) -> None:
        from smithcore.windows.tools.click import ClickTool

        el = _element()
        with (
            patch(
                "smithcore.windows.tools.click.resolve_element",
                new=AsyncMock(return_value=el),
            ),
            patch("smithcore.windows.tools.click._click_element", return_value=None) as click_el,
        ):
            result = await ClickTool().execute({"name": "OK"})
        click_el.assert_called_once()
        assert result["status"] == "clicked"

    @pytest.mark.asyncio
    async def test_no_target_raises_not_found(self) -> None:
        from smithcore.windows.tools.click import ClickTool

        with (
            patch(
                "smithcore.windows.tools.click.resolve_element",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ElementNotFound),
        ):
            await ClickTool().execute({"name": "Missing"})

    def test_click_at_branches(self) -> None:
        import sys
        from types import ModuleType

        from smithcore.windows.tools.click import _click_at, _click_element

        fake = ModuleType("uiautomation")
        fake.Click = MagicMock()  # type: ignore[attr-defined]
        fake.RightClick = MagicMock()  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"uiautomation": fake}):
            _click_at(1, 2, "left", 1)
            fake.Click.assert_called_once_with(1, 2, waitTime=0.5)
            _click_at(3, 4, "right", 2)
            assert fake.RightClick.call_count == 2
            assert fake.RightClick.call_args_list[0] == ((3, 4), {"waitTime": 0.0})
            assert fake.RightClick.call_args_list[1] == ((3, 4), {"waitTime": 0.5})

        target = MagicMock()
        _click_element(target, "left", 2)
        target.DoubleClick.assert_called_once_with()
        target2 = MagicMock()
        _click_element(target2, "right", 2)
        assert target2.RightClick.call_count == 2
        assert target2.RightClick.call_args_list[0] == ((), {"waitTime": 0.0})
        assert target2.RightClick.call_args_list[1] == ((), {"waitTime": 0.5})


class TestWaitDisappear:
    @pytest.mark.asyncio
    async def test_disappear_true_when_missing(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        with patch.object(
            ElementSelector,
            "find_from_desktop",
            side_effect=ElementNotFound("gone"),
        ):
            result = await WaitTool().execute(
                {"wait_for": "disappear", "timeout_ms": 500, "name": "x"}
            )
        assert result is True

    @pytest.mark.asyncio
    async def test_disappear_false_when_present(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        with patch.object(ElementSelector, "find_from_desktop", return_value=MagicMock()):
            result = await WaitTool().execute(
                {"wait_for": "disappear", "timeout_ms": 60, "interval_ms": 50, "name": "x"}
            )
        assert result is False

    @pytest.mark.asyncio
    async def test_rejects_empty_selector(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        with pytest.raises(InvalidInput, match="selector"):
            await WaitTool().execute({"timeout_ms": 1000})

    @pytest.mark.asyncio
    async def test_rejects_bad_wait_for(self) -> None:
        from smithcore.windows.tools.wait import WaitTool

        with pytest.raises(InvalidInput, match="wait_for"):
            await WaitTool().execute({"wait_for": "eventually"})


class TestScroll:
    @pytest.mark.asyncio
    async def test_scroll_down_default(self) -> None:
        from smithcore.windows.tools.scroll import ScrollTool

        with (
            patch(
                "smithcore.windows.tools.scroll.resolve_point",
                new=AsyncMock(return_value=None),
            ),
            patch("smithcore.windows.tools.scroll._scroll_at", return_value=None) as at,
        ):
            result = await ScrollTool().execute({})
        at.assert_called_once_with(None, "down", 3)
        assert result["direction"] == "down"

    @pytest.mark.asyncio
    async def test_rejects_bad_direction(self) -> None:
        from smithcore.windows.tools.scroll import ScrollTool

        with pytest.raises(InvalidInput, match="direction"):
            await ScrollTool().execute({"direction": "sideways"})

    @pytest.mark.asyncio
    async def test_rejects_zero_wheel(self) -> None:
        from smithcore.windows.tools.scroll import ScrollTool

        with pytest.raises(InvalidInput, match="wheel_clicks"):
            await ScrollTool().execute({"wheel_clicks": 0})

    def test_scroll_at_branches(self) -> None:
        import sys
        from types import ModuleType

        from smithcore.windows.tools.scroll import _scroll_at

        fake = ModuleType("uiautomation")
        fake.MoveTo = MagicMock()  # type: ignore[attr-defined]
        fake.WheelUp = MagicMock()  # type: ignore[attr-defined]
        fake.WheelDown = MagicMock()  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"uiautomation": fake}):
            _scroll_at((5, 6), "up", 2)
        fake.MoveTo.assert_called_once_with(5, 6)
        fake.WheelUp.assert_called_once_with(2)
        with patch.dict(sys.modules, {"uiautomation": fake}):
            _scroll_at(None, "down", 1)
        fake.WheelDown.assert_called_once_with(1)


class TestHover:
    @pytest.mark.asyncio
    async def test_hover_moves(self) -> None:
        from smithcore.windows.tools.hover import HoverTool

        with (
            patch(
                "smithcore.windows.tools.hover.resolve_point",
                new=AsyncMock(return_value=(7, 8)),
            ),
            patch("smithcore.windows.tools.hover._move_to", return_value=None) as move,
        ):
            result = await HoverTool().execute({"name": "File"})
        move.assert_called_once_with(7, 8)
        assert result == {"status": "hovered", "x": 7, "y": 8}

    @pytest.mark.asyncio
    async def test_hover_no_target_raises(self) -> None:
        from smithcore.windows.tools.hover import HoverTool

        with (
            patch(
                "smithcore.windows.tools.hover.resolve_point",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ElementNotFound),
        ):
            await HoverTool().execute({})


class TestExists:
    @pytest.mark.asyncio
    async def test_true_when_present(self) -> None:
        from smithcore.windows.tools.exists import ExistsTool

        with patch.object(ElementSelector, "find_from_desktop", return_value=MagicMock()):
            assert await ExistsTool().execute({"name": "OK"}) is True

    @pytest.mark.asyncio
    async def test_false_when_missing(self) -> None:
        from smithcore.windows.tools.exists import ExistsTool

        with patch.object(
            ElementSelector,
            "find_from_desktop",
            side_effect=ElementNotFound("no"),
        ):
            assert await ExistsTool().execute({"name": "OK"}) is False

    @pytest.mark.asyncio
    async def test_no_selector_rejected(self) -> None:
        from smithcore.windows.tools.exists import ExistsTool

        with pytest.raises(InvalidInput):
            await ExistsTool().execute({})

    @pytest.mark.asyncio
    async def test_platform_error_propagates(self) -> None:
        from smithcore.windows.tools.exists import ExistsTool

        with (
            patch.object(
                ElementSelector,
                "find_from_desktop",
                side_effect=PlatformError("uia down"),
            ),
            pytest.raises(PlatformError),
        ):
            await ExistsTool().execute({"name": "OK"})


class TestGetText:
    @pytest.mark.asyncio
    async def test_reads_value_pattern(self) -> None:
        from smithcore.windows.tools.get_text import GetTextTool

        el = _element()
        with patch(
            "smithcore.windows.tools.get_text.resolve_element",
            new=AsyncMock(return_value=el),
        ):
            result = await GetTextTool().execute({"name": "doc"})
        assert result["status"] == "read"
        assert isinstance(result["text"], str)

    @pytest.mark.asyncio
    async def test_no_element_raises(self) -> None:
        from smithcore.windows.tools.get_text import GetTextTool

        with (
            patch(
                "smithcore.windows.tools.get_text.resolve_element",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ElementNotFound),
        ):
            await GetTextTool().execute({})

    def test_read_text_prefers_value_over_name(self) -> None:
        from smithcore.windows.tools.get_text import _read_text

        pattern = MagicMock()
        pattern.Value = "typed"
        el = MagicMock()
        el.GetValuePattern.return_value = pattern
        el.Name = "fallback"
        assert _read_text(el) == "typed"

        el2 = MagicMock()
        el2.GetValuePattern.side_effect = RuntimeError("no pattern")
        el2.Name = "label"
        assert _read_text(el2) == "label"

        el3 = MagicMock()
        el3.GetValuePattern.side_effect = RuntimeError("no pattern")
        el3.Name = ""
        assert _read_text(el3) == ""


class TestWindow:
    @pytest.mark.asyncio
    async def test_activate_calls_apply(self) -> None:
        from smithcore.windows.tools.window import WindowTool

        ctrl = _element(NativeWindowHandle=1234)
        with (
            patch.object(ElementSelector, "find_from_desktop", return_value=ctrl),
            patch("smithcore.windows.tools.window._apply_action", return_value=None) as apply,
        ):
            result = await WindowTool().execute({"action": "activate", "pid": 42})
        apply.assert_called_once_with(1234, "activate", None)
        assert result == {"status": "activated", "action": "activate", "pid": 42}

    @pytest.mark.asyncio
    async def test_rejects_bad_action(self) -> None:
        from smithcore.windows.tools.window import WindowTool

        with pytest.raises(InvalidInput, match="action"):
            await WindowTool().execute({"action": "explode", "pid": 1})

    @pytest.mark.asyncio
    async def test_rejects_bool_pid(self) -> None:
        from smithcore.windows.tools.window import WindowTool

        with pytest.raises(InvalidInput, match="pid"):
            await WindowTool().execute({"action": "close", "pid": True})

    @pytest.mark.asyncio
    async def test_move_needs_geometry(self) -> None:
        from smithcore.windows.tools.window import WindowTool

        with pytest.raises(InvalidInput):
            await WindowTool().execute({"action": "move", "pid": 1, "x": 0, "y": 0})

    @pytest.mark.asyncio
    async def test_no_hwnd_raises_platform(self) -> None:
        from smithcore.windows.tools.window import WindowTool

        ctrl = _element(NativeWindowHandle=0)
        with (
            patch.object(ElementSelector, "find_from_desktop", return_value=ctrl),
            pytest.raises(PlatformError),
        ):
            await WindowTool().execute({"action": "activate", "pid": 9})

    def test_apply_action_branches(self) -> None:
        from smithcore.windows.tools.window import _apply_action

        user32 = MagicMock()
        with patch("smithcore.windows.tools.window._user32", return_value=user32):
            _apply_action(111, "activate", None)
        user32.SetForegroundWindow.assert_called_once_with(111)

        user32 = MagicMock()
        with patch("smithcore.windows.tools.window._user32", return_value=user32):
            _apply_action(222, "minimize", None)
        user32.ShowWindow.assert_called_once()

        user32 = MagicMock()
        with patch("smithcore.windows.tools.window._user32", return_value=user32):
            _apply_action(333, "move", (1, 2, 800, 600))
        user32.SetWindowPos.assert_called_once()

        user32 = MagicMock()
        with patch("smithcore.windows.tools.window._user32", return_value=user32):
            _apply_action(444, "close", None)
        user32.PostMessageW.assert_called_once()


class TestSelect:
    @pytest.mark.asyncio
    async def test_select_calls_pattern(self) -> None:
        from smithcore.windows.tools.select import SelectTool

        pattern = MagicMock()
        el = MagicMock()
        el.GetSelectionItemPattern.return_value = pattern
        with patch(
            "smithcore.windows.tools.select.resolve_element",
            new=AsyncMock(return_value=el),
        ):
            result = await SelectTool().execute({"name": "Option A"})
        pattern.Select.assert_called_once_with()
        assert result == {"status": "selected"}

    @pytest.mark.asyncio
    async def test_no_element_raises(self) -> None:
        from smithcore.windows.tools.select import SelectTool

        with (
            patch(
                "smithcore.windows.tools.select.resolve_element",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ElementNotFound),
        ):
            await SelectTool().execute({})

    @pytest.mark.asyncio
    async def test_unsupported_selection_is_platform_error(self) -> None:
        from smithcore.windows.tools.select import SelectTool

        el = MagicMock()
        el.GetSelectionItemPattern.side_effect = RuntimeError("no pattern")
        with (
            patch(
                "smithcore.windows.tools.select.resolve_element",
                new=AsyncMock(return_value=el),
            ),
            pytest.raises(PlatformError),
        ):
            await SelectTool().execute({"name": "x"})


class TestDrag:
    @pytest.mark.asyncio
    async def test_drag_both_endpoints(self) -> None:
        from smithcore.windows.tools import drag as drag_mod
        from smithcore.windows.tools.drag import DragTool

        async def fake_endpoint(config: dict[str, Any], prefix: str) -> tuple[int, int] | None:
            return (0, 0) if prefix == "from_" else (100, 200)

        with (
            patch.object(drag_mod, "_resolve_endpoint", side_effect=fake_endpoint),
            patch("smithcore.windows.tools.drag._drag_drop", return_value=None) as dd,
        ):
            result = await DragTool().execute({"from_x": 0, "from_y": 0})
        dd.assert_called_once_with((0, 0), (100, 200))
        assert result["status"] == "dragged"

    @pytest.mark.asyncio
    async def test_drag_missing_endpoint_rejected(self) -> None:
        from smithcore.windows.tools import drag as drag_mod
        from smithcore.windows.tools.drag import DragTool

        async def fake_endpoint(config: dict[str, Any], prefix: str) -> tuple[int, int] | None:
            return None if prefix == "to_" else (1, 1)

        with (
            patch.object(drag_mod, "_resolve_endpoint", side_effect=fake_endpoint),
            pytest.raises(InvalidInput),
        ):
            await DragTool().execute({"from_x": 1, "from_y": 1})

    def test_drag_drop_calls_auto(self) -> None:
        import sys
        from types import ModuleType

        from smithcore.windows.tools.drag import _drag_drop

        fake = ModuleType("uiautomation")
        fake.DragDrop = MagicMock()  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"uiautomation": fake}):
            _drag_drop((1, 2), (3, 4))
        fake.DragDrop.assert_called_once_with(1, 2, 3, 4)


class TestClipboard:
    @pytest.mark.asyncio
    async def test_get_returns_text(self) -> None:
        from smithcore.windows.tools import clipboard as cb_mod
        from smithcore.windows.tools.clipboard import ClipboardTool

        fake = MagicMock()
        fake.paste.return_value = "hello"
        with patch.object(cb_mod, "_load_pyperclip", return_value=fake):
            result = await ClipboardTool().execute({"action": "get"})
        assert result == {"status": "read", "text": "hello"}

    @pytest.mark.asyncio
    async def test_set_copies(self) -> None:
        from smithcore.windows.tools import clipboard as cb_mod
        from smithcore.windows.tools.clipboard import ClipboardTool

        fake = MagicMock()
        with patch.object(cb_mod, "_load_pyperclip", return_value=fake):
            result = await ClipboardTool().execute({"action": "set", "text": "abc"})
        fake.copy.assert_called_once_with("abc")
        assert result == {"status": "set"}

    @pytest.mark.asyncio
    async def test_rejects_bad_action(self) -> None:
        from smithcore.windows.tools.clipboard import ClipboardTool

        with pytest.raises(InvalidInput, match="action"):
            await ClipboardTool().execute({"action": "paste"})

    @pytest.mark.asyncio
    async def test_set_needs_text(self) -> None:
        from smithcore.windows.tools.clipboard import ClipboardTool

        with pytest.raises(InvalidInput, match="text"):
            await ClipboardTool().execute({"action": "set"})

    @pytest.mark.asyncio
    async def test_missing_pyperclip_is_platform_error(self) -> None:
        from smithcore.windows.tools import clipboard as cb_mod
        from smithcore.windows.tools.clipboard import ClipboardTool

        with (
            patch.object(
                cb_mod,
                "_load_pyperclip",
                side_effect=PlatformError("needs pyperclip"),
            ),
            pytest.raises(PlatformError),
        ):
            await ClipboardTool().execute({"action": "get"})


class TestListElements:
    @pytest.mark.asyncio
    async def test_lists_children(self) -> None:
        from smithcore.windows.tools.list_elements import ListElementsTool

        kids = [
            _element(Name="OK", AutomationId="ok_btn", HasChildren=False),
            _element(Name="Cancel", AutomationId="cancel_btn", HasChildren=True),
        ]
        parent = MagicMock()
        parent.GetFirstChildControl.return_value = kids[0]
        parent.GetFirstChildControl.return_value.GetNextSiblingControl.return_value = kids[1]
        (
            parent.GetFirstChildControl.return_value.GetNextSiblingControl.return_value.GetNextSiblingControl
        ).return_value = None
        with patch(
            "smithcore.windows.tools.list_elements.resolve_element",
            new=AsyncMock(return_value=parent),
        ):
            result = await ListElementsTool().execute({"name": "dlg", "max_items": 10})
        assert result["count"] == 2
        assert result["items"][0]["name"] == "OK"
        assert result["items"][1]["has_children"] is True

    @pytest.mark.asyncio
    async def test_no_parent_raises(self) -> None:
        from smithcore.windows.tools.list_elements import ListElementsTool

        with (
            patch(
                "smithcore.windows.tools.list_elements.resolve_element",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ElementNotFound),
        ):
            await ListElementsTool().execute({"name": "dlg"})

    @pytest.mark.asyncio
    async def test_bad_max_items_rejected(self) -> None:
        from smithcore.windows.tools.list_elements import ListElementsTool

        parent = MagicMock()
        with (
            patch(
                "smithcore.windows.tools.list_elements.resolve_element",
                new=AsyncMock(return_value=parent),
            ),
            pytest.raises(InvalidInput, match="max_items"),
        ):
            await ListElementsTool().execute({"name": "dlg", "max_items": 0})


class TestHighlight:
    @pytest.mark.asyncio
    async def test_highlight_flashes(self) -> None:
        from smithcore.windows.tools.highlight import HighlightTool

        rect = MagicMock(left=1, top=2, right=30, bottom=40)
        el = MagicMock()
        el.BoundingRectangle = rect
        with (
            patch(
                "smithcore.windows.tools.highlight.resolve_element",
                new=AsyncMock(return_value=el),
            ),
            patch("smithcore.windows.tools.highlight._flash_rect", return_value=None) as flash,
        ):
            result = await HighlightTool().execute({"name": "OK", "color": "green"})
        assert flash.call_count == 1
        assert result == {"status": "highlighted", "color": "green"}

    @pytest.mark.asyncio
    async def test_rejects_bad_color(self) -> None:
        from smithcore.windows.tools.highlight import HighlightTool

        el = MagicMock()
        with (
            patch(
                "smithcore.windows.tools.highlight.resolve_element",
                new=AsyncMock(return_value=el),
            ),
            pytest.raises(InvalidInput, match="color"),
        ):
            await HighlightTool().execute({"name": "OK", "color": "purple"})

    @pytest.mark.asyncio
    async def test_no_element_raises(self) -> None:
        from smithcore.windows.tools.highlight import HighlightTool

        with (
            patch(
                "smithcore.windows.tools.highlight.resolve_element",
                new=AsyncMock(return_value=None),
            ),
            pytest.raises(ElementNotFound),
        ):
            await HighlightTool().execute({"name": "OK"})


class TestFactoryAndFacade:
    def test_factory_returns_all_tools(self) -> None:
        from smithcore.windows.tools import windows_tools

        tools = windows_tools()
        assert len(tools) == 30
        names = {t.name for t in tools}
        for expected in (
            "windows.scroll",
            "windows.hover",
            "windows.exists",
            "windows.get_text",
            "windows.window",
            "windows.select",
            "windows.drag",
            "windows.clipboard",
            "windows.list_elements",
            "windows.highlight",
            "windows.find_image",
            "windows.click_image",
            "windows.ocr",
            "excel.read",
            "excel.write",
            "excel.append",
            "asset.get",
            "asset.credential",
        ):
            assert expected in names

    @pytest.mark.asyncio
    async def test_facade_click_forwards_params(self) -> None:
        from smithcore.facade import Smithcore

        bot = Smithcore(tools=[])
        with patch.object(bot, "_execute", new=AsyncMock(return_value={"status": "clicked"})) as ex:
            await bot.click(x=5, y=6, button="right", clicks=2)
        ex.assert_called_once()
        assert ex.call_args[0][0] == "windows.click"
        assert ex.call_args[0][1]["button"] == "right"
        assert ex.call_args[0][1]["x"] == 5

    @pytest.mark.asyncio
    async def test_facade_wait_forwards_wait_for(self) -> None:
        from smithcore.facade import Smithcore

        bot = Smithcore(tools=[])
        with patch.object(bot, "_execute", new=AsyncMock(return_value=True)) as ex:
            assert await bot.wait(name="OK", wait_for="disappear") is True
        assert ex.call_args[0][1]["wait_for"] == "disappear"

    @pytest.mark.asyncio
    async def test_facade_scroll_hover_exists_get_text(self) -> None:
        from smithcore.facade import Smithcore

        bot = Smithcore(tools=[])
        with patch.object(bot, "_execute", new=AsyncMock(return_value={"status": "scrolled"})):
            out = await bot.scroll(direction="up", wheel_clicks=1)
            assert out["status"] == "scrolled"
        with patch.object(bot, "_execute", new=AsyncMock(return_value={"status": "hovered"})):
            out = await bot.hover(name="File")
            assert out["status"] == "hovered"
        with patch.object(bot, "_execute", new=AsyncMock(return_value=True)):
            assert await bot.exists(name="OK") is True
        with patch.object(
            bot,
            "_execute",
            new=AsyncMock(return_value={"status": "read", "text": "hi"}),
        ):
            assert await bot.get_text(name="doc") == "hi"

    @pytest.mark.asyncio
    async def test_facade_window_select_drag_clipboard_list_highlight(self) -> None:
        from smithcore.facade import Smithcore

        bot = Smithcore(tools=[])
        with patch.object(
            bot,
            "_execute",
            new=AsyncMock(return_value={"status": "activated"}),
        ) as ex:
            await bot.window(action="activate", pid=42)
            assert ex.call_args[0][1] == {"action": "activate", "pid": 42}
        with patch.object(bot, "_execute", new=AsyncMock(return_value={"status": "selected"})):
            out = await bot.select(name="A")
            assert out["status"] == "selected"
        with patch.object(bot, "_execute", new=AsyncMock(return_value={"status": "dragged"})) as ex:
            await bot.drag(from_x=0, from_y=0, to_x=9, to_y=9)
            assert ex.call_args[0][1]["to_x"] == 9
        with patch.object(
            bot,
            "_execute",
            new=AsyncMock(return_value={"status": "read", "text": "cb"}),
        ):
            assert await bot.clipboard(action="get") == "cb"
        with patch.object(
            bot,
            "_execute",
            new=AsyncMock(return_value={"status": "set"}),
        ):
            out = await bot.clipboard(action="set", text="x")
            assert out == {"status": "set"}
        with patch.object(bot, "_execute", new=AsyncMock(return_value={"items": [], "count": 0})):
            out = await bot.list_elements(name="dlg")
            assert out["count"] == 0
        with patch.object(
            bot,
            "_execute",
            new=AsyncMock(return_value={"status": "highlighted"}),
        ):
            out = await bot.highlight(name="OK")
            assert out["status"] == "highlighted"
