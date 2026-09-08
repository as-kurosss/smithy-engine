"""Tests for the facade keyed-selector (dev capture) workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.core.selectors import SelectorStore
from smithy.core.tool import AbstractTool
from smithy.facade import Smithy
from smithy.windows.tools.selector_capture.api import CapturedSelector


class _StubTool(AbstractTool):
    """Returns scripted results; raises them when they are exceptions."""

    def __init__(self, name: str, results: list[Any]) -> None:
        self._name = name
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "stub"

    def schema(self) -> dict[str, Any]:
        return {"type": "object"}

    async def execute(self, config: dict[str, Any]) -> Any:
        self.calls.append(dict(config))
        outcome = self._results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _captured(name: str = "OK2") -> CapturedSelector:
    return CapturedSelector(selector={"name": name, "control_type": "button"})


def _patch_capture(
    monkeypatch: pytest.MonkeyPatch, captured_results: list[CapturedSelector]
) -> list[CapturedSelector]:
    """Patch capture_once_async; returns the list of capture *invocations*."""
    used: list[CapturedSelector] = []

    async def fake_capture() -> CapturedSelector:
        used.append(captured_results.pop(0))
        return used[-1]

    monkeypatch.setattr("smithy.windows.tools.selector_capture.capture_once_async", fake_capture)
    return used


class TestKeyedConfig:
    @pytest.mark.asyncio
    async def test_stored_key_runs_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used = _patch_capture(monkeypatch, [])
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("ok", {"name": "OK", "control_type": "button"})

        tool = _StubTool("windows.click", [{"status": "clicked"}])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        await bot.click(key="ok")

        assert used == []
        assert tool.calls[0]["name"] == "OK"

    @pytest.mark.asyncio
    async def test_missing_key_captures_and_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used = _patch_capture(monkeypatch, [_captured("Submit")])
        store_path = tmp_path / "selectors.json"

        tool = _StubTool("windows.click", [{"status": "clicked"}])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        await bot.click(key="submit")

        assert len(used) == 1
        assert tool.calls[0]["name"] == "Submit"
        assert SelectorStore(store_path).get("submit") == {
            "name": "Submit",
            "control_type": "button",
        }

    @pytest.mark.asyncio
    async def test_missing_key_production_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used = _patch_capture(monkeypatch, [])
        tool = _StubTool("windows.click", [{"status": "clicked"}])
        bot = Smithy(tools=[tool], selector_store=tmp_path / "s.json", dev_capture=False)
        with pytest.raises(InvalidInput, match="SMITHY_DEV_CAPTURE"):
            await bot.click(key="submit")
        assert tool.calls == []
        assert used == []

    @pytest.mark.asyncio
    async def test_explicit_fields_win_over_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_capture(monkeypatch, [])
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("ok", {"name": "StoredName"})

        tool = _StubTool("windows.click", [{"status": "clicked"}])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        await bot.click(key="ok", name="Explicit")

        assert tool.calls[0]["name"] == "Explicit"

    @pytest.mark.asyncio
    async def test_stale_selector_recaptured_and_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used = _patch_capture(monkeypatch, [_captured("Fresh")])
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("ok", {"name": "Stale"})

        tool = _StubTool("windows.click", [ElementNotFound("gone"), {"status": "clicked"}])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        result = await bot.click(key="ok")

        assert result.status == "clicked"
        assert len(used) == 1
        assert len(tool.calls) == 2
        assert tool.calls[0]["name"] == "Stale"
        assert tool.calls[1]["name"] == "Fresh"
        stored = SelectorStore(store_path).get("ok")
        assert stored is not None and stored["name"] == "Fresh"

    @pytest.mark.asyncio
    async def test_stale_selector_production_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        used = _patch_capture(monkeypatch, [])
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("ok", {"name": "Stale"})

        tool = _StubTool("windows.click", [ElementNotFound("gone")])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=False)
        with pytest.raises(ElementNotFound):
            await bot.click(key="ok")
        assert len(tool.calls) == 1
        assert used == []


class TestKeyedOtherTools:
    @pytest.mark.asyncio
    async def test_wait_uses_stored(self, tmp_path: Path) -> None:
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("dlg", {"name": "Dialog"})

        tool = _StubTool("windows.wait", [True])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        assert await bot.wait(key="dlg", timeout_ms=100) is True
        assert tool.calls[0]["name"] == "Dialog"
        assert tool.calls[0]["wait_for"] == "appear"

    @pytest.mark.asyncio
    async def test_set_text_and_input_text_keyed(self, tmp_path: Path) -> None:
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("edit", {"automation_id": "edit1"})

        tool = _StubTool("windows.set_text", [{"status": "set"}])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        await bot.set_text(key="edit", text="hello")
        assert tool.calls[0] == {"text": "hello", "automation_id": "edit1"}

        tool2 = _StubTool("windows.input_text", [{"status": "sent"}])
        bot2 = Smithy(tools=[tool2], selector_store=store_path, dev_capture=True)
        await bot2.input_text(key="edit", text="hi")
        assert tool2.calls[0] == {"text": "hi", "automation_id": "edit1"}

    @pytest.mark.asyncio
    async def test_get_text_keyed(self, tmp_path: Path) -> None:
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("lbl", {"name": "Total"})

        tool = _StubTool("windows.get_text", [{"text": "1500"}])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        assert await bot.get_text(key="lbl") == "1500"
        assert tool.calls[0]["name"] == "Total"

    @pytest.mark.asyncio
    async def test_exists_uses_store_without_retry(self, tmp_path: Path) -> None:
        store_path = tmp_path / "selectors.json"
        SelectorStore(store_path).put("x", {"name": "X"})

        tool = _StubTool("windows.exists", [False, False])
        bot = Smithy(tools=[tool], selector_store=store_path, dev_capture=True)
        assert await bot.exists(key="x") is False
        assert len(tool.calls) == 1


class TestDevCaptureEnv:
    def test_env_enables_dev_capture(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SMITHY_DEV_CAPTURE", "1")
        bot = Smithy(selector_store=tmp_path / "s.json")
        assert bot._dev_capture is True

    def test_explicit_beats_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("SMITHY_DEV_CAPTURE", "1")
        bot = Smithy(selector_store=tmp_path / "s.json", dev_capture=False)
        assert bot._dev_capture is False
