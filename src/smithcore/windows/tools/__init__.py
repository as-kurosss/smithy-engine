"""Default Windows toolset factory."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smithcore.core.tool import Tool

__all__ = ["windows_tools"]


def windows_tools(
    *,
    allowed_commands: Iterable[str] | None = None,
) -> list[Tool]:
    """Build the default Windows tool set for :class:`SmithCore`.

    Imports are function-local so that importing this package stays cheap
    and never pulls UIA dependencies at module import time::

        from smithcore.windows.tools import windows_tools

        bot = SmithCore(tools=windows_tools())

    Args:
        allowed_commands: Forwarded to :class:`ProcessTool` — executables
            the bot may start. ``None`` means the built-in demo list (or
            the ``SMITHCORE_ALLOWED_COMMANDS`` env override when set).
    """
    from smithcore.core.asset_tools import AssetCredentialTool, AssetGetTool
    from smithcore.core.excel import ExcelAppendTool, ExcelReadTool, ExcelWriteTool
    from smithcore.core.files import FileTool
    from smithcore.windows.tools.click import ClickTool
    from smithcore.windows.tools.clipboard import ClipboardTool
    from smithcore.windows.tools.control_action import ControlActionTool
    from smithcore.windows.tools.delay import DelayTool
    from smithcore.windows.tools.drag import DragTool
    from smithcore.windows.tools.exists import ExistsTool
    from smithcore.windows.tools.get_element import GetElementTool
    from smithcore.windows.tools.get_table import GetTableTool
    from smithcore.windows.tools.get_text import GetTextTool
    from smithcore.windows.tools.highlight import HighlightTool
    from smithcore.windows.tools.hover import HoverTool
    from smithcore.windows.tools.image import ClickImageTool, FindImageTool
    from smithcore.windows.tools.input_text import InputTextTool
    from smithcore.windows.tools.keyboard import KeyboardTool
    from smithcore.windows.tools.list_elements import ListElementsTool
    from smithcore.windows.tools.ocr import OcrTool
    from smithcore.windows.tools.process import ProcessTool
    from smithcore.windows.tools.screenshot import ScreenshotTool
    from smithcore.windows.tools.scroll import ScrollTool
    from smithcore.windows.tools.select import SelectTool
    from smithcore.windows.tools.set_text import SetTextTool
    from smithcore.windows.tools.wait import WaitTool
    from smithcore.windows.tools.window import WindowTool

    return [
        ProcessTool(allowed_commands=allowed_commands),
        ClickTool(),
        WaitTool(),
        DelayTool(),
        ScreenshotTool(),
        InputTextTool(),
        KeyboardTool(),
        SetTextTool(),
        GetElementTool(),
        ScrollTool(),
        HoverTool(),
        ExistsTool(),
        GetTextTool(),
        WindowTool(),
        SelectTool(),
        DragTool(),
        ClipboardTool(),
        ListElementsTool(),
        HighlightTool(),
        GetTableTool(),
        ControlActionTool(),
        FindImageTool(),
        ClickImageTool(),
        OcrTool(),
        FileTool(),
        ExcelReadTool(),
        ExcelWriteTool(),
        ExcelAppendTool(),
        AssetGetTool(),
        AssetCredentialTool(),
    ]
