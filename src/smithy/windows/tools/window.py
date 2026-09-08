"""WindowTool — activate, minimize, maximize, restore, move, or close a window."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.selector import ElementSelector

_ACTIONS = ("activate", "minimize", "maximize", "restore", "move", "close")
_STATUSES = {
    "activate": "activated",
    "minimize": "minimized",
    "maximize": "maximized",
    "restore": "restored",
    "move": "moved",
    "close": "closed",
}

# Win32 ShowWindow commands.
_SW_RESTORE = 9
_SW_MINIMIZE = 6
_SW_MAXIMIZE = 3
# WM_CLOSE message.
_WM_CLOSE = 0x0010
# SetWindowPos flags: keep Z order, keep size when only moving is not needed —
# here position and size always come together, so just show the window.
_SWP_SHOWWINDOW = 0x0040


class WindowTool(AbstractTool):
    """Manage a top-level window by process ID.

    The classic flake source — clicks missing because the window is not
    foreground — is fixed by the ``activate`` + click pair. ``move``
    needs ``x``, ``y``, ``width``, and ``height`` together.
    """

    @property
    def name(self) -> str:
        return "windows.window"

    @property
    def description(self) -> str:
        return "Activates, minimizes, maximizes, restores, moves, or closes a window by PID"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["activate", "minimize", "maximize", "restore", "move", "close"],
                    "description": "Window action",
                },
                "pid": {"type": "integer", "description": "Process ID owning the window"},
                "x": {"type": "integer", "description": "Left edge (move only)"},
                "y": {"type": "integer", "description": "Top edge (move only)"},
                "width": {"type": "integer", "minimum": 1, "description": "Width (move only)"},
                "height": {"type": "integer", "minimum": 1, "description": "Height (move only)"},
            },
            "required": ["action", "pid"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        action = config.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise InvalidInput(
                f"Invalid 'action': expected one of {_ACTIONS}",
                param="action",
                input_value=action,
            )
        pid = config.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise InvalidInput(
                "Invalid 'pid': expected an integer",
                param="pid",
                input_value=pid,
            )
        geometry: tuple[int, int, int, int] | None = None
        if action == "move":
            geometry = _read_geometry(config)

        try:
            selector = ElementSelector().with_pid(pid)
            control = await run_blocking(selector.find_from_desktop)
            hwnd = getattr(control, "NativeWindowHandle", None)
            if not hwnd:
                raise PlatformError(f"No window handle for PID {pid}")
            await run_blocking(_apply_action, hwnd, action, geometry)
        except (InvalidInput, ElementNotFound, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError(f"Window action {action!r} failed: {exc}", source=exc) from exc
        return {"status": _STATUSES[action], "action": action, "pid": pid}


def _read_geometry(config: dict[str, Any]) -> tuple[int, int, int, int]:
    """Read move geometry; all four fields are required together."""
    values: list[int] = []
    for key in ("x", "y", "width", "height"):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidInput(
                f"Invalid {key!r}: 'move' needs integer x, y, width, height",
                param=key,
                input_value=value,
            )
        values.append(value)
    if values[2] < 1 or values[3] < 1:
        raise InvalidInput(
            "'move' needs width and height >= 1",
            param="width",
            input_value={"width": values[2], "height": values[3]},
        )
    return (values[0], values[1], values[2], values[3])


def _user32() -> Any:
    """Win32 user32 handle (patchable seam for tests)."""
    import ctypes

    return ctypes.windll.user32


def _apply_action(hwnd: int, action: str, geometry: tuple[int, int, int, int] | None) -> None:
    """Apply the Win32 call (runs in an executor)."""
    user32 = _user32()
    if action == "activate":
        user32.SetForegroundWindow(hwnd)
    elif action == "minimize":
        user32.ShowWindow(hwnd, _SW_MINIMIZE)
    elif action == "maximize":
        user32.ShowWindow(hwnd, _SW_MAXIMIZE)
    elif action == "restore":
        user32.ShowWindow(hwnd, _SW_RESTORE)
    elif action == "move":
        assert geometry is not None
        x, y, width, height = geometry
        user32.SetWindowPos(hwnd, None, x, y, width, height, _SWP_SHOWWINDOW)
    else:  # close
        user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
