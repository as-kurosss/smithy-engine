"""ExcelTool — read/write xlsx workbooks (the classic RPA data source).

Requires the ``excel`` extra (``openpyxl``)::

    pip install "smithy-engine[excel]"

Paths honor the ``SMITHY_FILE_ROOT`` sandbox (see :mod:`smithy.core.files`).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import InvalidInput, PlatformError
from smithy.core.files import confine_path
from smithy.core.tool import AbstractTool

_ACTIONS = ("read", "write", "append")


class ExcelTool(AbstractTool):
    """Excel (xlsx) operations: read rows, write a sheet, append rows."""

    @property
    def name(self) -> str:
        return "excel"

    @property
    def description(self) -> str:
        return "Reads and writes xlsx workbooks: read, write, append"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "Excel operation",
                },
                "path": {"type": "string", "description": "Workbook path (.xlsx)"},
                "sheet": {
                    "type": "string",
                    "description": "Sheet name (default: active sheet)",
                },
                "rows": {
                    "type": "array",
                    "items": {"type": "array"},
                    "description": "Rows for write/append (list of lists)",
                },
                "header": {
                    "type": "boolean",
                    "default": True,
                    "description": "Treat the first row as a header (read)",
                },
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1000,
                    "description": "Max data rows for read",
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow write to replace an existing workbook",
                },
            },
            "required": ["action", "path"],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        action = config.get("action")
        if not isinstance(action, str) or action not in _ACTIONS:
            raise InvalidInput(
                f"Invalid 'action': expected one of {_ACTIONS}",
                param="action",
                input_value=action,
            )
        raw_path = config.get("path")
        if not isinstance(raw_path, (str, Path)) or not str(raw_path):
            raise InvalidInput(
                "Missing required parameter: path (expected an .xlsx path)",
                param="path",
                input_value=raw_path,
            )
        path = confine_path(Path(raw_path))
        if path.suffix.lower() != ".xlsx":
            path = path.with_suffix(".xlsx")
        sheet = config.get("sheet")
        if sheet is not None and (not isinstance(sheet, str) or not sheet):
            raise InvalidInput(
                "Invalid 'sheet': expected a non-empty string",
                param="sheet",
                input_value=sheet,
            )

        if action == "read":
            return await self._read(path, sheet, config)
        rows = _check_rows(config)
        if action == "write":
            return await self._write(path, sheet, rows, config)
        return await self._append(path, sheet, rows)

    async def _read(self, path: Path, sheet: str | None, config: dict[str, Any]) -> dict[str, Any]:
        header = config.get("header", True)
        if not isinstance(header, bool):
            raise InvalidInput(
                "Invalid 'header': expected a boolean", param="header", input_value=header
            )
        max_rows = config.get("max_rows", 1000)
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise InvalidInput(
                "Invalid 'max_rows': expected an integer >= 1",
                param="max_rows",
                input_value=max_rows,
            )
        try:
            columns, rows = await run_blocking(_read_sync, path, sheet, header, max_rows)
        except FileNotFoundError as exc:
            raise PlatformError(f"Workbook not found: {path}", source=exc) from exc
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(f"Cannot read workbook {path}: {exc}", source=exc) from exc
        result: dict[str, Any] = {"rows": rows, "count": len(rows), "path": str(path)}
        if columns is not None:
            result["columns"] = columns
        return result

    async def _write(
        self,
        path: Path,
        sheet: str | None,
        rows: list[list[Any]],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        overwrite = config.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise InvalidInput(
                "Invalid 'overwrite': expected a boolean",
                param="overwrite",
                input_value=overwrite,
            )

        def _write_sync() -> int:
            if path.exists() and not overwrite:
                raise PlatformError(
                    f"Workbook already exists: {path} (pass overwrite=true to replace)"
                )
            from openpyxl import Workbook

            workbook = Workbook()
            worksheet = workbook.active if sheet is None else workbook.create_sheet(sheet)
            assert worksheet is not None
            for row in rows:
                worksheet.append(row)
            path.parent.mkdir(parents=True, exist_ok=True)
            workbook.save(str(path))
            return len(rows)

        written = await run_blocking(_write_sync)
        return {"path": str(path), "written": written}

    async def _append(self, path: Path, sheet: str | None, rows: list[list[Any]]) -> dict[str, Any]:
        try:
            written = await run_blocking(_append_sync, path, sheet, rows)
        except FileNotFoundError as exc:
            raise PlatformError(f"Workbook not found: {path}", source=exc) from exc
        except PlatformError:
            raise
        except Exception as exc:
            raise PlatformError(f"Cannot append to workbook {path}: {exc}", source=exc) from exc
        return {"path": str(path), "written": written}


def _read_sync(
    path: Path, sheet: str | None, header: bool, max_rows: int
) -> tuple[list[str] | None, list[list[Any]]]:
    """Read rows (runs in an executor)."""
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), data_only=True, read_only=True)
    try:
        worksheet = workbook[sheet] if sheet is not None else workbook.active
        if worksheet is None:
            raise PlatformError(f"Workbook {path} has no sheets")
        columns: list[str] | None = None
        rows: list[list[Any]] = []
        for index, row in enumerate(worksheet.iter_rows(values_only=True)):
            values = [_cell(value) for value in row]
            if index == 0 and header:
                columns = [str(value) for value in values]
                continue
            rows.append(values)
            if len(rows) >= max_rows:
                break
        return columns, rows
    finally:
        workbook.close()


def _append_sync(path: Path, sheet: str | None, rows: list[list[Any]]) -> int:
    """Append rows to an existing workbook (runs in an executor)."""
    from openpyxl import load_workbook

    workbook = load_workbook(str(path))
    try:
        if sheet is not None and sheet not in workbook.sheetnames:
            raise PlatformError(f"Sheet {sheet!r} not found in {path}")
        worksheet = workbook[sheet] if sheet is not None else workbook.active
        assert worksheet is not None
        for row in rows:
            worksheet.append(row)
        workbook.save(str(path))
        return len(rows)
    finally:
        workbook.close()


def _check_rows(config: dict[str, Any]) -> list[list[Any]]:
    raw_rows = config.get("rows")
    if not isinstance(raw_rows, list) or not all(isinstance(row, list) for row in raw_rows):
        raise InvalidInput(
            "Missing or invalid 'rows': expected a list of lists",
            param="rows",
            input_value=type(raw_rows).__name__ if not isinstance(raw_rows, list) else raw_rows,
        )
    return raw_rows


def _cell(value: Any) -> Any:
    """JSON-safe cell value (runs in an executor context, never raises)."""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return value
