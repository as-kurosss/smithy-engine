"""FileTool — file operations for config-driven flows.

Flows (``flow.json``) cannot call Python, but almost every robot moves
files: a download must be picked up, a report must be written, a CSV
must be archived. This tool exposes the basic filesystem surface with
the same validation style as the rest of the engine.

Sandbox: when ``SMITHY_FILE_ROOT`` is set, every path must resolve
inside that directory — flow configs then cannot read or overwrite
anything outside it. Without the env var paths are used as-is (flows
are trusted code, see SECURITY.md).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import InvalidInput, PlatformError
from smithy.core.tool import AbstractTool

ENV_FILE_ROOT = "SMITHY_FILE_ROOT"

_ACTIONS = ("read", "write", "append", "copy", "move", "delete", "exists", "wait_for", "list")


def confine_path(path: Path) -> Path:
    """Resolve *path* against ``SMITHY_FILE_ROOT`` when set.

    Absolute paths outside the sandbox and relative escapes (``..``)
    are rejected. Without the env var the path is used as-is.
    """
    root_raw = os.environ.get(ENV_FILE_ROOT)
    if not root_raw:
        return path
    root = Path(root_raw).resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise InvalidInput(
            f"Path {str(path)!r} escapes the file root {str(root)!r} (set via {ENV_FILE_ROOT})",
            param="path",
            input_value=str(path),
        )
    return resolved


class FileTool(AbstractTool):
    """Filesystem operations: read, write, append, copy, move, delete, list."""

    @property
    def name(self) -> str:
        return "file"

    @property
    def description(self) -> str:
        return (
            "File operations for automation flows: read, write, append, "
            "copy, move, delete, exists, wait_for, list"
        )

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "File operation",
                },
                "path": {"type": "string", "description": "Target file or directory"},
                "destination": {
                    "type": "string",
                    "description": "Target path for copy/move",
                },
                "content": {"type": "string", "description": "Text for write/append"},
                "encoding": {"type": "string", "default": "utf-8"},
                "pattern": {"type": "string", "description": "Glob for the list action"},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 30000,
                    "description": "Max wait for the wait_for action",
                },
                "interval_ms": {
                    "type": "integer",
                    "minimum": 50,
                    "default": 500,
                    "description": "Polling interval for wait_for",
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow copy/move to replace an existing destination",
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
        if not isinstance(raw_path, (str, os.PathLike)) or not str(raw_path):
            raise InvalidInput(
                "Missing required parameter: path (expected a non-empty path)",
                param="path",
                input_value=raw_path,
            )
        path = confine_path(Path(raw_path))

        if action == "read":
            return await self._read(path, config)
        if action == "write":
            return await self._write(path, config, append=False)
        if action == "append":
            return await self._write(path, config, append=True)
        if action == "copy" or action == "move":
            return await self._transfer(path, config, move=action == "move")
        if action == "delete":
            return await self._delete(path)
        if action == "exists":
            return await self._exists(path)
        if action == "wait_for":
            return await self._wait_for(path, config)
        return await self._list(path, config)

    async def _read(self, path: Path, config: dict[str, Any]) -> dict[str, Any]:
        encoding = _check_encoding(config)
        try:
            text: str = await run_blocking(path.read_text, encoding=encoding)
        except FileNotFoundError as exc:
            raise PlatformError(f"File not found: {path}", source=exc) from exc
        except OSError as exc:
            raise PlatformError(f"Cannot read {path}: {exc}", source=exc) from exc
        return {"path": str(path), "content": text, "size": len(text)}

    async def _write(self, path: Path, config: dict[str, Any], *, append: bool) -> dict[str, Any]:
        content = config.get("content")
        if not isinstance(content, str):
            raise InvalidInput(
                "Missing required parameter: content (expected a string)",
                param="content",
                input_value=content,
            )
        encoding = _check_encoding(config)
        mode = "a" if append else "w"

        def _write_file() -> int:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open(mode, encoding=encoding, newline="") as fh:
                return fh.write(content)

        size: int = await run_blocking(_write_file)
        return {"path": str(path), "written": size}

    async def _transfer(self, path: Path, config: dict[str, Any], *, move: bool) -> dict[str, Any]:
        raw_dest = config.get("destination")
        if not isinstance(raw_dest, (str, os.PathLike)) or not str(raw_dest):
            raise InvalidInput(
                "Missing required parameter: destination for copy/move",
                param="destination",
                input_value=raw_dest,
            )
        overwrite = config.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise InvalidInput(
                "Invalid 'overwrite': expected a boolean",
                param="overwrite",
                input_value=overwrite,
            )
        destination = confine_path(Path(raw_dest))

        def _transfer_sync() -> dict[str, Any]:
            if not path.exists():
                raise PlatformError(f"Source not found: {path}")
            if destination.exists() and not overwrite:
                raise PlatformError(
                    f"Destination already exists: {destination} (pass overwrite=true to replace)"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            if move:
                moved: str = shutil.move(str(path), str(destination))
                return {"source": str(path), "path": moved}
            shutil.copy2(str(path), str(destination))
            return {"source": str(path), "path": str(destination)}

        result: dict[str, Any] = await run_blocking(_transfer_sync)
        result["status"] = "moved" if move else "copied"
        return result

    async def _delete(self, path: Path) -> dict[str, Any]:
        existed = await run_blocking(_delete_path, path)
        return {"path": str(path), "deleted": existed}

    async def _exists(self, path: Path) -> bool:
        return bool(await run_blocking(path.exists))

    async def _wait_for(self, path: Path, config: dict[str, Any]) -> dict[str, Any]:
        timeout_ms = config.get("timeout_ms", 30000)
        interval_ms = config.get("interval_ms", 500)
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or timeout_ms < 1
            or isinstance(interval_ms, bool)
            or not isinstance(interval_ms, int)
            or interval_ms < 50
        ):
            raise InvalidInput(
                "Invalid timeout_ms/interval_ms: expected integers (interval_ms >= 50)",
                param="timeout_ms",
                input_value={"timeout_ms": timeout_ms, "interval_ms": interval_ms},
            )
        deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while True:
            if await run_blocking(path.exists):
                return {"exists": True, "path": str(path)}
            if asyncio.get_running_loop().time() >= deadline:
                return {"exists": False, "path": str(path)}
            await asyncio.sleep(interval_ms / 1000)

    async def _list(self, path: Path, config: dict[str, Any]) -> dict[str, Any]:
        raw_pattern = config.get("pattern", "*")
        if not isinstance(raw_pattern, str) or not raw_pattern:
            raise InvalidInput(
                "Invalid 'pattern': expected a non-empty glob string",
                param="pattern",
                input_value=raw_pattern,
            )
        entries: list[dict[str, Any]] = await run_blocking(_list_entries, path, raw_pattern)
        return {"path": str(path), "entries": entries, "count": len(entries)}


def _delete_path(path: Path) -> bool:
    """Delete a file (runs in an executor). Returns True if it existed."""
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def _list_entries(path: Path, pattern: str) -> list[dict[str, Any]]:
    """Directory listing (runs in an executor)."""
    if not path.is_dir():
        raise PlatformError(f"Not a directory: {path}")
    entries: list[dict[str, Any]] = []
    for item in sorted(path.glob(pattern)):
        entries.append(
            {
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else None,
            }
        )
    return entries


def _check_encoding(config: dict[str, Any]) -> str:
    encoding = config.get("encoding", "utf-8")
    if not isinstance(encoding, str) or not encoding:
        raise InvalidInput(
            "Invalid 'encoding': expected a non-empty string",
            param="encoding",
            input_value=encoding,
        )
    return encoding
