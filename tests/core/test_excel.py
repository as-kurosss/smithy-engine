"""Tests for smithcore.core.excel — excel.read / excel.write / excel.append."""

from __future__ import annotations

from pathlib import Path

import pytest

from smithcore.core.errors import InvalidInput, PlatformError
from smithcore.core.excel import ExcelAppendTool, ExcelReadTool, ExcelWriteTool

pytest.importorskip("openpyxl")


class TestWriteRead:
    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "book"
        await ExcelWriteTool().execute(
            {
                "path": str(target),
                "sheet": "Data",
                "rows": [["name", "qty"], ["wid", 1], ["gear", 2]],
            }
        )
        xlsx = target.with_suffix(".xlsx")
        assert xlsx.exists()
        result = await ExcelReadTool().execute({"path": str(xlsx), "sheet": "Data"})
        assert result["columns"] == ["name", "qty"]
        assert result["rows"] == [["wid", 1], ["gear", 2]]
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_read_without_header(self, tmp_path: Path) -> None:
        target = tmp_path / "plain.xlsx"
        await ExcelWriteTool().execute(
            {"path": str(target), "rows": [[1, 2], [3, 4]], "overwrite": True}
        )
        result = await ExcelReadTool().execute({"path": str(target), "header": False})
        assert result["rows"] == [[1, 2], [3, 4]]
        assert "columns" not in result

    @pytest.mark.asyncio
    async def test_max_rows(self, tmp_path: Path) -> None:
        target = tmp_path / "big.xlsx"
        await ExcelWriteTool().execute(
            {"path": str(target), "rows": [[n] for n in range(10)], "overwrite": True}
        )
        result = await ExcelReadTool().execute(
            {"path": str(target), "header": False, "max_rows": 3}
        )
        assert result["count"] == 3


class TestGuards:
    def test_distinct_tool_names(self) -> None:
        assert ExcelReadTool().name == "excel.read"
        assert ExcelWriteTool().name == "excel.write"
        assert ExcelAppendTool().name == "excel.append"

    @pytest.mark.asyncio
    async def test_write_refuses_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "book.xlsx"
        await ExcelWriteTool().execute({"path": str(target), "rows": [[1]]})
        with pytest.raises(PlatformError, match="already exists"):
            await ExcelWriteTool().execute({"path": str(target), "rows": [[2]]})

    @pytest.mark.asyncio
    async def test_append_missing_workbook(self, tmp_path: Path) -> None:
        with pytest.raises(PlatformError, match="not found"):
            await ExcelAppendTool().execute({"path": str(tmp_path / "none.xlsx"), "rows": [[1]]})

    @pytest.mark.asyncio
    async def test_append_to_sheet(self, tmp_path: Path) -> None:
        target = tmp_path / "app.xlsx"
        await ExcelWriteTool().execute({"path": str(target), "rows": [["a"]], "overwrite": True})
        await ExcelAppendTool().execute({"path": str(target), "rows": [["b"]]})
        result = await ExcelReadTool().execute({"path": str(target), "header": False})
        assert result["rows"] == [["a"], ["b"]]

    @pytest.mark.asyncio
    async def test_rows_validation(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidInput, match="rows"):
            await ExcelWriteTool().execute({"path": str(tmp_path / "x.xlsx"), "rows": "nope"})
