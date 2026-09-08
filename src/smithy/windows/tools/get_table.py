"""GetTableTool — extract tabular data from a Windows UI element."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import ElementNotFound, InvalidInput, PlatformError
from smithy.core.tool import AbstractTool
from smithy.windows.tools._resolve import resolve_element

_ROW_CONTROL_TYPES = frozenset({"listitem", "dataitem", "treeitem", "row"})
_HEADER_CONTROL_TYPES = frozenset({"header", "headeritem"})
_CONTAINER_CONTROL_TYPES = frozenset(
    {"list", "datagrid", "table", "tree", "data", "pane", "group", "custom", "document"}
)
_MAX_SCAN_DEPTH = 4
_MAX_COLUMNS = 64


class GetTableTool(AbstractTool):
    """Extract rows of a table/list/grid element as JSON arrays.

    Works with any UIA container whose rows are list/data/tree items:
    DataGrid, ListView, GridView, TreeView. Rows are the container's
    children; cells are each row's direct children (``Name`` values).
    A ``header`` control is used for column names when present.
    """

    @property
    def name(self) -> str:
        return "windows.get_table"

    @property
    def description(self) -> str:
        return "Extracts rows of a table/list/grid element as JSON (columns + rows)"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Table container name"},
                "automation_id": {"type": "string", "description": "UI Automation identifier"},
                "control_type": {"type": "string", "description": "Control type"},
                "class_name": {"type": "string", "description": "Window class name"},
                "pid": {"type": "integer", "description": "Process ID filter"},
                "max_rows": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 100,
                    "description": "Max data rows to extract",
                },
            },
            "required": [],
        }

    async def execute(self, config: dict[str, Any]) -> Any:
        max_rows = config.get("max_rows", 100)
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise InvalidInput(
                "Invalid 'max_rows': expected an integer >= 1",
                param="max_rows",
                input_value=max_rows,
            )
        element = await resolve_element(config)
        if element is None:
            raise ElementNotFound(
                "No element found: provide selector fields "
                "(name, automation_id, control_type, class_name, pid)",
                selector=config,
            )
        try:
            header, rows = await run_blocking(_extract_table, element, max_rows)
        except (InvalidInput, ElementNotFound, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError(f"Table extraction failed: {exc}", source=exc) from exc
        if not rows and header is None:
            raise PlatformError(
                "No rows found under the element — point the selector at the "
                "list/grid container (or raise max_rows)"
            )
        result: dict[str, Any] = {"rows": rows, "count": len(rows)}
        if header is not None:
            result["columns"] = header
        return result


def _safe(call: Any) -> Any:
    """Best-effort property/method read on a live UIA node (never raises)."""
    try:
        return call()
    except Exception:
        return None


def _control_type(node: Any) -> str | None:
    value = _safe(lambda: str(node.ControlTypeName).lower())
    return value if isinstance(value, str) else None


def _find_row_container(node: Any, depth: int) -> Any:
    """Descend through wrapper containers until children are rows/headers."""
    if depth > _MAX_SCAN_DEPTH:
        return node
    child = _safe(lambda: node.GetFirstChildControl())
    if child is None:
        return node
    first_type = _control_type(child)
    if first_type is None:
        return node
    if first_type in _ROW_CONTROL_TYPES or first_type in _HEADER_CONTROL_TYPES:
        return node
    if first_type in _CONTAINER_CONTROL_TYPES:
        return _find_row_container(child, depth + 1)
    return node


def _read_cells(row: Any) -> list[str]:
    """Read direct-cell names of one row (skips nested row-like children)."""
    cells: list[str] = []
    cell = _safe(lambda: row.GetFirstChildControl())
    while cell is not None and len(cells) < _MAX_COLUMNS:
        if _control_type(cell) not in _ROW_CONTROL_TYPES:
            name = _safe(lambda c=cell: str(c.Name))
            if isinstance(name, str):
                cells.append(name)
        cell = _safe(lambda c=cell: c.GetNextSiblingControl())
    return cells


def _extract_table(element: Any, max_rows: int) -> tuple[list[str] | None, list[list[str]]]:
    """Walk the table tree (runs in an executor)."""
    container = _find_row_container(element, 0)
    header: list[str] | None = None
    rows: list[list[str]] = []
    node = _safe(lambda: container.GetFirstChildControl())
    while node is not None:
        ctype = _control_type(node)
        if ctype in _HEADER_CONTROL_TYPES and header is None:
            header = _read_cells(node)
        elif ctype in _ROW_CONTROL_TYPES and len(rows) < max_rows:
            rows.append(_read_cells(node))
        node = _safe(lambda n=node: n.GetNextSiblingControl())
    return header, rows
