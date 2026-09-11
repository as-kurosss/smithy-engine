"""Tests for smithcore.core.files — FileTool."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithcore.core.errors import InvalidInput, PlatformError
from smithcore.core.files import ENV_FILE_ROOT, FileTool


@pytest.fixture()
def file_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "files"
    root.mkdir()
    monkeypatch.setenv(ENV_FILE_ROOT, str(root))
    return root


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self, file_root: Path) -> None:
        tool = FileTool()
        await tool.execute({"action": "write", "path": "out/report.txt", "content": "данные"})
        result = await tool.execute({"action": "read", "path": "out/report.txt"})
        assert result["content"] == "данные"
        assert (file_root / "out" / "report.txt").read_text(encoding="utf-8") == "данные"

    @pytest.mark.asyncio
    async def test_append_accumulates(self, file_root: Path) -> None:
        tool = FileTool()
        await tool.execute({"action": "write", "path": "log.txt", "content": "one\n"})
        await tool.execute({"action": "append", "path": "log.txt", "content": "two\n"})
        result = await tool.execute({"action": "read", "path": "log.txt"})
        assert result["content"] == "one\ntwo\n"

    @pytest.mark.asyncio
    async def test_write_requires_content(self, file_root: Path) -> None:
        with pytest.raises(InvalidInput, match="content"):
            await FileTool().execute({"action": "write", "path": "x.txt"})


class TestSandbox:
    @pytest.mark.asyncio
    async def test_escape_rejected(self, file_root: Path) -> None:
        with pytest.raises(InvalidInput, match="escapes"):
            await FileTool().execute({"action": "read", "path": "../secret.txt"})

    @pytest.mark.asyncio
    async def test_absolute_outside_rejected(self, file_root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        with pytest.raises(InvalidInput, match="escapes"):
            await FileTool().execute({"action": "read", "path": str(outside)})

    @pytest.mark.asyncio
    async def test_no_root_means_free_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_FILE_ROOT, raising=False)
        target = tmp_path / "free.txt"
        target.write_text("ok", encoding="utf-8")
        result = await FileTool().execute({"action": "read", "path": str(target)})
        assert result["content"] == "ok"


class TestTransfer:
    @pytest.mark.asyncio
    async def test_copy_move(self, file_root: Path) -> None:
        tool = FileTool()
        await tool.execute({"action": "write", "path": "a.txt", "content": "data"})
        await tool.execute({"action": "copy", "path": "a.txt", "destination": "copy/a2.txt"})
        assert (file_root / "copy" / "a2.txt").read_text(encoding="utf-8") == "data"
        await tool.execute({"action": "move", "path": "a.txt", "destination": "b.txt"})
        assert not (file_root / "a.txt").exists()
        assert (file_root / "b.txt").exists()

    @pytest.mark.asyncio
    async def test_refuses_existing_destination(self, file_root: Path) -> None:
        tool = FileTool()
        await tool.execute({"action": "write", "path": "a.txt", "content": "1"})
        await tool.execute({"action": "write", "path": "b.txt", "content": "2"})
        with pytest.raises(PlatformError, match="already exists"):
            await tool.execute({"action": "copy", "path": "a.txt", "destination": "b.txt"})
        result = await tool.execute(
            {"action": "copy", "path": "a.txt", "destination": "b.txt", "overwrite": True}
        )
        assert result["status"] == "copied"


class TestMisc:
    @pytest.mark.asyncio
    async def test_exists_and_delete(self, file_root: Path) -> None:
        tool = FileTool()
        await tool.execute({"action": "write", "path": "x.txt", "content": "x"})
        assert await tool.execute({"action": "exists", "path": "x.txt"}) is True
        result = await tool.execute({"action": "delete", "path": "x.txt"})
        assert result["deleted"] is True
        assert await tool.execute({"action": "exists", "path": "x.txt"}) is False
        result = await tool.execute({"action": "delete", "path": "x.txt"})
        assert result["deleted"] is False

    @pytest.mark.asyncio
    async def test_list(self, file_root: Path) -> None:
        tool = FileTool()
        await tool.execute({"action": "write", "path": "dir/a.txt", "content": "1"})
        await tool.execute({"action": "write", "path": "dir/b.txt", "content": "22"})
        (file_root / "dir" / "sub").mkdir()
        result = await tool.execute({"action": "list", "path": "dir"})
        names = [entry["name"] for entry in result["entries"]]
        assert names == ["a.txt", "b.txt", "sub"]
        sizes = {entry["name"]: entry["size"] for entry in result["entries"]}
        assert sizes["a.txt"] == 1 and sizes["b.txt"] == 2 and sizes["sub"] is None

    @pytest.mark.asyncio
    async def test_wait_for_appears(self, file_root: Path) -> None:
        tool = FileTool()
        target = file_root / "late.txt"

        import asyncio

        async def _spawn() -> None:
            await asyncio.sleep(0.05)
            await tool.execute({"action": "write", "path": "late.txt", "content": "!"})

        task = asyncio.create_task(_spawn())
        result = await tool.execute(
            {"action": "wait_for", "path": "late.txt", "timeout_ms": 2000, "interval_ms": 50}
        )
        await task
        assert result["exists"] is True and target.exists()

    @pytest.mark.asyncio
    async def test_wait_for_times_out(self, file_root: Path) -> None:
        result = await FileTool().execute(
            {"action": "wait_for", "path": "never.txt", "timeout_ms": 120, "interval_ms": 50}
        )
        assert result["exists"] is False

    @pytest.mark.asyncio
    async def test_unknown_action(self, file_root: Path) -> None:
        with pytest.raises(InvalidInput, match="action"):
            await FileTool().execute({"action": "chmod", "path": "x.txt"})
