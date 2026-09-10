"""Default Windows toolset factory."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smithy.core.tool import Tool

__all__ = ["windows_tools"]


def windows_tools(
    *,
    allowed_commands: Iterable[str] | None = None,
) -> list[Tool]:
    """Build the default Windows tool set for :class:`Smithy`.

    Imports are function-local so that importing this package stays cheap
    and never pulls UIA dependencies at module import time::

        from smithy.windows.tools import windows_tools

        bot = Smithy(tools=windows_tools())

    Args:
        allowed_commands: Forwarded to :class:`ProcessTool` — executables
            the bot may start. ``None`` means the built-in demo list (or
            the ``SMITHY_ALLOWED_COMMANDS`` env override when set).
    """
    from smithy.core.asset_tools import AssetCredentialTool, AssetGetTool
    from smithy.core.excel import ExcelAppendTool, ExcelReadTool, ExcelWriteTool
    from smithy.core.files import FileTool
    from smithy.windows.tools.click import ClickTool
    from smithy.windows.tools.clipboard import ClipboardTool
    from smithy.windows.tools.control_action import ControlActionTool
    from smithy.windows.tools.delay import DelayTool
    from smithy.windows.tools.drag import DragTool
    from smithy.windows.tools.exists import ExistsTool
    from smithy.windows.tools.get_element import GetElementTool
    from smithy.windows.tools.get_table import GetTableTool
    from smithy.windows.tools.get_text import GetTextTool
    from smithy.windows.tools.highlight import HighlightTool
    from smithy.windows.tools.hover import HoverTool
    from smithy.windows.tools.image import ClickImageTool, FindImageTool
    from smithy.windows.tools.input_text import InputTextTool
    from smithy.windows.tools.keyboard import KeyboardTool
    from smithy.windows.tools.list_elements import ListElementsTool
    from smithy.windows.tools.ocr import OcrTool
    from smithy.windows.tools.process import ProcessTool
    from smithy.windows.tools.screenshot import ScreenshotTool
    from smithy.windows.tools.scroll import ScrollTool
    from smithy.windows.tools.select import SelectTool
    from smithy.windows.tools.set_text import SetTextTool
    from smithy.windows.tools.wait import WaitTool
    from smithy.windows.tools.window import WindowTool

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
