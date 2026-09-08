"""Tests for the programmatic capture API (capture_once)."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock

import pytest

from smithy.core.errors import ToolError
from smithy.windows.tools.selector_capture import api as capture_api
from smithy.windows.tools.selector_capture.api import CaptureCancelled, capture_once
from smithy.windows.tools.selector_capture.capture import BestSelector, PathNode
from smithy.windows.tools.selector_capture.recorder import SharedEvent


class _FakeGroup:
    def __init__(self, listeners: list[Any]) -> None:
        pass

    def __enter__(self) -> _FakeGroup:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _patch_capture_backend(
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_event: str = "trigger",
    selector: BestSelector | None = None,
    ranked_config: dict[str, Any] | None = None,
) -> None:
    def fake_shared_listener(out: Any) -> Any:
        out.put(SharedEvent(first_event))  # type: ignore[arg-type]
        return object()

    def fake_capture_at_point(x: float, y: float) -> tuple[Any, BestSelector]:
        path = [PathNode(control_type="Window", name="App")]
        return path, selector or BestSelector(
            control_type="Button", name="Submit", automation_id="btnSubmit"
        )

    ranked = MagicMock()
    ranked.config = ranked_config or {"name": "Submit", "automation_id": "btnSubmit"}
    ranked.confidence = "high"
    ranked.warnings = ()

    monkeypatch.setattr(capture_api, "_ListenerGroup", _FakeGroup)
    monkeypatch.setattr(capture_api, "_shared_listener", fake_shared_listener)
    monkeypatch.setattr(capture_api, "_require_pynput", lambda: None)
    monkeypatch.setattr(capture_api, "capture_at_point", fake_capture_at_point)
    monkeypatch.setattr(capture_api, "_rank_captured", lambda sel: ranked)
    monkeypatch.setattr(capture_api, "_log_ranked", lambda ranked, sel: None)
    _patch_pynput_mouse(monkeypatch)


def _patch_pynput_mouse(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = MagicMock()
    controller.position = (10, 20)
    fake_mouse = ModuleType("pynput.mouse")
    fake_mouse.Controller = lambda: controller  # type: ignore[attr-defined]
    fake_root = ModuleType("pynput")
    monkeypatch.setitem(sys.modules, "pynput", fake_root)
    monkeypatch.setitem(sys.modules, "pynput.mouse", fake_mouse)


class TestCaptureOnce:
    def test_returns_ranked_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_capture_backend(monkeypatch)
        captured = capture_once()
        assert captured.selector == {"name": "Submit", "automation_id": "btnSubmit"}
        assert captured.confidence == "high"
        assert captured.full_path == [{"control_type": "Window", "name": "App"}]

    def test_escape_raises_cancelled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_capture_backend(monkeypatch, first_event="escape")
        with pytest.raises(CaptureCancelled):
            capture_once()

    def test_capture_cancelled_is_tool_error(self) -> None:
        assert issubclass(CaptureCancelled, ToolError)

    def test_falls_back_to_inline_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_capture_backend(
            monkeypatch,
            ranked_config=None,
            selector=BestSelector(control_type="Button", name="Submit", automation_id="btnSubmit"),
        )

        class _NoneRanked:
            pass

        monkeypatch.setattr(capture_api, "_rank_captured", lambda sel: None)
        monkeypatch.setattr(
            capture_api,
            "build_inline_selector",
            lambda sel: {"name": "Submit"},
        )
        captured = capture_once()
        assert captured.selector == {"name": "Submit"}
        assert captured.confidence is None

    @pytest.mark.asyncio
    async def test_async_twin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from smithy.windows.tools.selector_capture import capture_once_async

        _patch_capture_backend(monkeypatch)
        captured = await capture_once_async()
        assert captured.selector == {"name": "Submit", "automation_id": "btnSubmit"}
