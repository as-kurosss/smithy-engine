"""Tests for the configurable ProcessTool allowlist."""

from __future__ import annotations

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.windows.tools.process import ProcessTool


@pytest.fixture(autouse=True)
def _clean_allowlist_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SMITHCORE_ALLOWED_COMMANDS", raising=False)


class TestProcessAllowlist:
    def test_default_blocks_shells(self) -> None:
        tool = ProcessTool()
        assert "cmd.exe" not in tool.allowed_commands
        assert "powershell.exe" not in tool.allowed_commands
        assert "notepad.exe" in tool.allowed_commands

    @pytest.mark.asyncio
    async def test_default_rejects_disallowed_command(self) -> None:
        tool = ProcessTool()
        with pytest.raises(InvalidInput, match="not in the allowed list"):
            await tool.execute({"action": "start", "command": "cmd.exe"})

    def test_constructor_param_overrides_defaults(self) -> None:
        tool = ProcessTool(allowed_commands=["myapp.exe"])
        assert tool.allowed_commands == {"myapp.exe"}
        assert "notepad.exe" not in tool.allowed_commands

    def test_constructor_entries_normalized(self) -> None:
        tool = ProcessTool(allowed_commands=["  MyApp.EXE ", "C:\\Tools\\other.exe", ""])
        assert tool.allowed_commands == {"myapp.exe", "other.exe"}

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ALLOWED_COMMANDS", "custom.exe, other.exe")
        tool = ProcessTool()
        assert tool.allowed_commands == {"custom.exe", "other.exe"}

    def test_empty_env_denies_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ALLOWED_COMMANDS", "")
        tool = ProcessTool()
        assert tool.allowed_commands == set()

    @pytest.mark.asyncio
    async def test_empty_env_blocks_notepad_without_spawning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMITHCORE_ALLOWED_COMMANDS", "")
        tool = ProcessTool()
        with pytest.raises(InvalidInput, match="not in the allowed list"):
            await tool.execute({"action": "start", "command": "notepad.exe"})

    def test_constructor_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ALLOWED_COMMANDS", "env-app.exe")
        tool = ProcessTool(allowed_commands=["ctor-app.exe"])
        assert tool.allowed_commands == {"ctor-app.exe"}

    @pytest.mark.asyncio
    async def test_stop_by_name_obeys_allowlist(self) -> None:
        tool = ProcessTool()
        with pytest.raises(InvalidInput, match="not in the allowed list"):
            await tool.execute({"action": "stop", "name": "cmd.exe"})
