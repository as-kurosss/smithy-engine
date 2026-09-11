"""KeyboardTool — send key combinations and key taps."""

from __future__ import annotations

import ctypes
import re
import time
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element

# Matches bracketed tokens: [CTRL], [CTRL!], [+CTRL], [-CTRL], [S], etc.
_BRACKET_RE = re.compile(r"\[([+-]?)(\w+)(!)?\]")

# Virtual key codes for special keys.
_VK_MAP: dict[str, int] = {
    "CTRL": 0x11,
    "CONTROL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,
    "MENU": 0x12,
    "WIN": 0x5B,
    "LWIN": 0x5B,
    "RWIN": 0x5C,
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "TAB": 0x09,
    "ESCAPE": 0x1B,
    "ESC": 0x1B,
    "BACKSPACE": 0x08,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "INSERT": 0x2D,
    "INS": 0x2D,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PRIOR": 0x21,
    "PAGEDOWN": 0x22,
    "NEXT": 0x22,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "SPACE": 0x20,
    "CAPSLOCK": 0x14,
    "CAPITAL": 0x14,
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
    "PRINTSCREEN": 0x2C,
    "SNAPSHOT": 0x2C,
    "NUMLOCK": 0x90,
    "SCROLLLOCK": 0x91,
    "SCROLL": 0x91,
}


def normalize_keys(text: str) -> str:
    """Normalize bracketed key tokens for ``uiautomation.SendKeys``.

    Bracketed tokens are key presses; everything else is literal text.
    ``+``/``-`` prefixes are stripped — ``[+CTRL]``, ``[-CTRL]``, and
    ``[CTRL]`` all become ``{CTRL}`` (hold mode in SendKeys).

    A ``!`` suffix means a quick tap (press and release):
    ``[CTRL!]`` → ``<tap:CTRL>``.

    Examples:
    - ``"Hello World"``          → ``"Hello World"``
    - ``"[CTRL]S"``              → ``"{CTRL}S"``
    - ``"[CTRL!]"``              → ``"<tap:CTRL>"``
    - ``"[CTRL!][S!]"``          → ``"<tap:CTRL><tap:S>"``
    - ``"Hello [CTRL]"``         → ``"Hello {CTRL}"``
    """
    if "[" not in text:
        return text

    def _replace(m: re.Match[str]) -> str:
        key, tap = m.group(2), m.group(3)
        key_upper = key.upper()
        if tap == "!":
            return "<tap:" + key_upper + ">"
        return "{" + key_upper + "}"

    return _BRACKET_RE.sub(_replace, text)


_TAP_RE = re.compile(r"<tap:(\w+)>")
#: Hold/special key tokens produced by ``normalize_keys`` (``{CTRL}`` …).
_HOLD_RE = re.compile(r"\{(\w+)\}")

# INPUT structures for SendInput (Win32).
_INPUT_KEYBOARD = 1
_INPUT_MOUSE = 0
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004

_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

#: Keys that must be sent with KEYEVENTF_EXTENDEDKEY (navigation cluster and
#: a few others); modifiers must NOT use it or they become the right-hand
#: variants / numpad keys.
_EXTENDED_KEYS: frozenset[int] = frozenset(
    {
        _VK_MAP["LEFT"],
        _VK_MAP["UP"],
        _VK_MAP["RIGHT"],
        _VK_MAP["DOWN"],
        _VK_MAP["INSERT"],
        _VK_MAP["DELETE"],
        _VK_MAP["HOME"],
        _VK_MAP["END"],
        _VK_MAP["PAGEUP"],
        _VK_MAP["PAGEDOWN"],
        _VK_MAP["NUMLOCK"],
        _VK_MAP["PRINTSCREEN"],
    }
)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_ushort),
        ("wParamH", ctypes.c_ushort),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _InputUnion)]


def _tap_key(name: str) -> None:
    """Quick press-and-release of a key via SendInput."""
    upper = name.upper()
    if upper in _VK_MAP:
        vk = _VK_MAP[upper]
        flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0
        _send_keybd(vk, 0, flags)
        _send_keybd(vk, 0, flags | _KEYEVENTF_KEYUP)
    elif len(name) == 1:
        _send_unicode(name)
    else:
        raise ValueError(f"Unknown key: {name!r}")
    time.sleep(0.01)


def _send_input(inp: _INPUT) -> None:
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
    if sent != 1:
        raise PlatformError("SendInput failed (the target may be elevated or blocked)")


def _send_keybd(vk: int, scan: int, flags: int) -> None:
    """Send a single keybd event via SendInput."""
    _send_input(
        _INPUT(
            _INPUT_KEYBOARD,
            _InputUnion(ki=_KEYBDINPUT(vk, scan, flags, 0, 0)),
        )
    )


def _send_unicode(char: str) -> None:
    """Send a single Unicode character via SendInput KEYEVENTF_UNICODE.

    Characters outside the BMP are encoded as a UTF-16 surrogate pair and
    sent as two events, so emoji/astral text survives.
    """
    encoded = char.encode("utf-16-le")
    for offset in range(0, len(encoded), 2):
        scan = encoded[offset] | (encoded[offset + 1] << 8)
        _send_input(
            _INPUT(
                _INPUT_KEYBOARD,
                _InputUnion(ki=_KEYBDINPUT(0, scan, _KEYEVENTF_UNICODE, 0, 0)),
            )
        )
        _send_input(
            _INPUT(
                _INPUT_KEYBOARD,
                _InputUnion(ki=_KEYBDINPUT(0, scan, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, 0)),
            )
        )


def send_literal_text(text: str) -> None:
    """Type *text* literally via SendInput, bypassing SendKeys syntax.

    Unlike :meth:`KeyboardTool` (which uses ``{...}`` tokens for named
    keys), this treats every character as data: ``{``, ``+``, ``^``,
    ``%``, ``~`` are typed as-is. Newlines become ENTER, tabs become TAB.
    """
    for char in text:
        if char == "\n":
            _tap_key("ENTER")
        elif char == "\t":
            _tap_key("TAB")
        elif char == "\r":
            continue
        else:
            _send_unicode(char)


def _send(text: str) -> None:
    """Send keystrokes — tap keys and literals via SendInput, holds via SendKeys.

    Plain text is typed literally (so ``%``, ``+``, ``^`` are data), while
    ``{CTRL}``-style hold tokens keep SendKeys' modifier semantics.
    """
    import uiautomation as auto

    parts = _TAP_RE.split(text)
    for i, part in enumerate(parts):
        if not part:
            continue
        if i % 2 == 1:
            _tap_key(part)
        else:
            _send_segment(part, auto)


def _send_segment(part: str, auto: Any) -> None:
    """Type a non-tap segment, preserving only known ``{KEY}`` hold tokens."""
    position = 0
    for match in _HOLD_RE.finditer(part):
        literal = part[position : match.start()]
        if literal:
            send_literal_text(literal)
        token = match.group(1)
        if token.upper() in _VK_MAP or len(token) == 1:
            auto.SendKeys(match.group(0))
        else:
            send_literal_text(match.group(0))
        position = match.end()
    tail = part[position:]
    if tail:
        send_literal_text(tail)


class KeyboardTool(AbstractTool):
    """Send key combinations and key presses.

    Bracketed tokens are key events; everything else is plain text.

    Hold mode: ``[CTRL]`` enters hold, subsequent chars are typed with
    CTRL held, hold exits on the next non-modifier character.

    Tap mode: ``[CTRL!]`` quickly presses and releases CTRL.

    Examples:
    - ``"[CTRL]S"``     — hold Ctrl, type S (Ctrl+S)
    - ``"[CTRL!]S"``    — tap Ctrl, then type S separately
    - ``"[CTRL!][S!]"`` — tap Ctrl, tap S (two quick taps)
    - ``"[ENTER]"``     — press Enter (hold mode)
    - ``"Hello"``       — type literal text
    """

    @property
    def name(self) -> str:
        return "windows.keyboard"

    @property
    def description(self) -> str:
        return "Send key combinations and key presses"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": (
                        "Key presses. Bracketed tokens are keys: "
                        "'[CTRL]', '[CTRL]S', '[CTRL!]'. "
                        "Add '!' suffix for quick tap instead of hold. "
                        "Everything else is literal text."
                    ),
                },
                "name": {"type": "string", "description": "Element name to find"},
                "automation_id": {
                    "type": "string",
                    "description": "UI Automation identifier",
                },
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
            },
            "required": ["keys"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        raw = config.get("keys")
        if not isinstance(raw, str) or not raw:
            raise InvalidInput(
                "Missing required parameter: keys (expected a non-empty string)",
                param="keys",
                input_value=raw,
            )

        keys = normalize_keys(raw)

        element = await resolve_element(config)
        if element is not None:
            try:
                await run_blocking(element.SetFocus)
            except Exception as exc:
                raise PlatformError(
                    f"Cannot focus the target element before sending keys: {exc}",
                    source=exc,
                    input_value=raw,
                ) from exc

        try:
            await run_blocking(_send, keys)
        except ValueError as exc:
            raise InvalidInput(
                f"Unknown key: {exc}",
                param="keys",
                input_value="***",
            ) from exc
        except Exception as exc:
            raise PlatformError(
                "Failed to send keys",
                source=exc,
                input_value="***",
            ) from exc
        # Do not echo raw keys (may contain typed secrets); normalized tokens
        # describe structure without literal text.
        return {"status": "sent", "length": len(raw)}
