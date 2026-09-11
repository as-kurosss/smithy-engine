"""Tests for smithcore.windows.tools.keyboard — normalize_keys."""

import pytest

from smithcore.windows.tools import keyboard
from smithcore.windows.tools.keyboard import normalize_keys


class TestNormalizeKeys:
    def test_plain_text_unchanged(self) -> None:
        assert normalize_keys("Hello World") == "Hello World"

    def test_literal_ctrl_not_in_brackets(self) -> None:
        assert normalize_keys("CTRL") == "CTRL"

    def test_literal_ctrl_s_not_in_brackets(self) -> None:
        assert normalize_keys("CTRL+S") == "CTRL+S"

    def test_bracket_single_key(self) -> None:
        assert normalize_keys("[CTRL]") == "{CTRL}"

    def test_bracket_hold_release(self) -> None:
        assert normalize_keys("[+CTRL]A[-CTRL]") == "{CTRL}A{CTRL}"

    def test_bracket_complex(self) -> None:
        result = normalize_keys("[+CTRL][+SHIFT]A[-SHIFT][-CTRL]")
        assert result == "{CTRL}{SHIFT}A{SHIFT}{CTRL}"

    def test_mixed_text_and_keys(self) -> None:
        assert normalize_keys("Hello [CTRL]") == "Hello {CTRL}"

    def test_literal_text_with_plus(self) -> None:
        assert normalize_keys("2+2") == "2+2"

    def test_literal_enter(self) -> None:
        assert normalize_keys("enter") == "enter"

    def test_bracket_enter(self) -> None:
        assert normalize_keys("[enter]") == "{ENTER}"

    def test_plus_minus_stripped(self) -> None:
        result = normalize_keys("[+SHIFT]Hello[-SHIFT]")
        assert result == "{SHIFT}Hello{SHIFT}"

    def test_tap_key(self) -> None:
        assert normalize_keys("[CTRL!]") == "<tap:CTRL>"

    def test_tap_with_text(self) -> None:
        assert normalize_keys("Hello[CTRL!]S") == "Hello<tap:CTRL>S"

    def test_multiple_taps(self) -> None:
        assert normalize_keys("[CTRL!][S!]") == "<tap:CTRL><tap:S>"

    def test_tap_and_hold_mixed(self) -> None:
        assert normalize_keys("[CTRL!]Hello[SHIFT]A") == "<tap:CTRL>Hello{SHIFT}A"


class TestSendLiteralText:
    def test_literal_chars_bypass_sendkeys_syntax(self, monkeypatch: pytest.MonkeyPatch) -> None:
        chars: list[str] = []
        taps: list[str] = []
        monkeypatch.setattr(keyboard, "_send_unicode", chars.append)
        monkeypatch.setattr(keyboard, "_tap_key", taps.append)

        keyboard.send_literal_text("a{+^%}\n\t\U0001f600")

        assert chars == ["a", "{", "+", "^", "%", "}", "\U0001f600"]
        assert taps == ["ENTER", "TAB"]
