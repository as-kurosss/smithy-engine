"""ScreenshotTool — capture screen or window and save to a file."""

from __future__ import annotations

import ctypes
import ctypes.wintypes  # noqa: PLC0415
import os
from pathlib import Path
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import InvalidInput, PlatformError
from smithy.core.tool import AbstractTool

# https://learn.microsoft.com/en-us/windows/win32/dwm/window-attributes
_DWMWA_EXTENDED_FRAME_BOUNDS = 9


class ScreenshotTool(AbstractTool):
    """Capture a screenshot of the screen or a specific window.

    Requires ``mss`` and ``Pillow`` (included in ``smithy[windows]``).
    """

    @property
    def name(self) -> str:
        return "windows.screenshot"

    @property
    def description(self) -> str:
        return "Capture a screenshot of the full screen or a specific window and save it to a file"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to save the screenshot",
                },
                "pid": {
                    "type": "integer",
                    "description": "Capture only the window belonging to this process ID",
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "jpg"],
                    "default": "png",
                    "description": "Image format (png or jpg)",
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        raw_path = config.get("path")
        if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path):
            raise InvalidInput(
                "Missing required parameter: path (expected a non-empty path)",
                param="path",
                input_value=raw_path,
            )

        save_path = Path(raw_path)
        raw_fmt = config.get("format", "png")
        if not isinstance(raw_fmt, str):
            raise InvalidInput(
                "Invalid format: expected 'png' or 'jpg'",
                param="format",
                input_value=raw_fmt,
            )
        fmt = raw_fmt.lower()
        if fmt not in ("png", "jpg"):
            raise InvalidInput(
                f"Unsupported format: {fmt!r}. Use 'png' or 'jpg'.",
                param="format",
                input_value=fmt,
            )

        # Ensure the file extension matches the requested format
        if save_path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            save_path = save_path.with_suffix(f".{fmt}")

        pid = config.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
            raise InvalidInput(
                "Invalid 'pid': expected an integer",
                param="pid",
                input_value=pid,
            )

        try:
            if pid is not None:
                saved = await run_blocking(_capture_window, pid, save_path, fmt)
            else:
                saved = await run_blocking(_capture_full_screen, save_path, fmt)
        except ImportError as exc:
            raise PlatformError(
                "Screenshot requires 'mss' and 'Pillow'. Install with: pip install mss Pillow",
                source=exc,
            ) from exc
        except Exception as exc:
            raise PlatformError(
                f"Screenshot capture failed: {exc}",
                source=exc,
            ) from exc

        return {"status": "saved", "path": str(saved), "format": fmt}


def _capture_full_screen(save_path: Path, fmt: str) -> Path:
    """Capture the full screen using mss and save to *save_path*."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    save_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(save_path), format=_pil_format(fmt))
    return save_path


def _pil_format(fmt: str) -> str:
    """Map a user format to a Pillow format name."""
    if fmt.lower() in ("jpg", "jpeg"):
        return "JPEG"
    return "PNG"


def _capture_window(pid: int, save_path: Path, fmt: str) -> Path:
    """Capture a single window's bounding box using mss."""
    import mss
    from PIL import Image

    user32 = ctypes.windll.user32
    hwnd = _find_window_by_pid(user32, pid)
    if hwnd is None:
        raise ValueError(f"No visible window found for PID {pid}")

    rect = ctypes.wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ValueError(f"GetWindowRect failed for PID {pid}")
    left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom

    # DWM extended frame bounds (accounts for shadows/borders)
    extended_rect = ctypes.c_long * 4
    er = extended_rect()
    if (
        ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(er), ctypes.sizeof(er)
        )
        == 0
    ):
        left, top, right, bottom = er[0], er[1], er[2], er[3]

    monitor = {
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }

    with mss.mss() as sct:
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    save_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(save_path), format=_pil_format(fmt))
    return save_path


def _find_window_by_pid(user32: Any, target_pid: int) -> int | None:
    """Find the first visible top-level window for *target_pid*."""
    result: dict[str, int] = {}

    @ctypes.WINFUNCTYPE(  # type: ignore[untyped-decorator]
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )
    def _enum_callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        current_pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(
            hwnd,
            ctypes.byref(current_pid),
        )
        if current_pid.value == target_pid and user32.GetWindowTextLengthW(hwnd) > 0:
            result["hwnd"] = hwnd
            return False  # stop enumeration
        return True

    user32.EnumWindows(_enum_callback, 0)
    return result.get("hwnd")
