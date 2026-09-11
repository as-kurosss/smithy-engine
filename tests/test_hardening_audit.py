"""Regression tests for the second-audit hardening pass (0.8.6)."""

from __future__ import annotations

import asyncio
import ctypes
import json
from pathlib import Path
from typing import Any

import pytest

from smithcore.core.config import load_config
from smithcore.core.errors import InvalidInput, PlatformError
from smithcore.core.files import FileTool
from smithcore.core.queue import InMemoryQueue
from smithcore.core.registry import ToolRegistry
from smithcore.core.retry import RetryTool
from smithcore.core.tool import AbstractTool, tool
from smithcore.flow import (
    FlowError,
    FlowRunner,
    parse_set_value,
    validate_document,
)
from smithcore.windows.tools.keyboard import _INPUT

# ------------------------------------------------------------------ config


class TestConfigEnvOverlay:
    def test_asset_namespace_not_overlaid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMITHCORE_ASSET_DB_PASSWORD", "SUPERSECRET")
        path = tmp_path / "bot.toml"
        path.write_text('[robot]\nname = "x"\n', encoding="utf-8")
        config = load_config(path)
        assert "asset_db_password" not in config.to_dict()
        assert "SUPERSECRET" not in repr(config)

    def test_dates_and_nonfinite_stay_strings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMITHCORE_RUN_DATE", "2023-01-01")
        monkeypatch.setenv("SMITHCORE_RATIO", "nan")
        path = tmp_path / "bot.toml"
        path.write_text("", encoding="utf-8")
        data = load_config(path).to_dict()
        assert data["run_date"] == "2023-01-01"
        assert data["ratio"] == "nan"


# ------------------------------------------------------------------ files


class TestFileSandbox:
    @pytest.mark.asyncio
    async def test_glob_traversal_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMITHCORE_FILE_ROOT", str(tmp_path))
        with pytest.raises(InvalidInput, match="pattern"):
            await FileTool().execute({"action": "list", "path": ".", "pattern": "../*.txt"})

    @pytest.mark.asyncio
    async def test_delete_directory_raises_platform_error(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        with pytest.raises(PlatformError, match="directory"):
            await FileTool().execute({"action": "delete", "path": str(sub)})

    @pytest.mark.asyncio
    async def test_unknown_encoding_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(InvalidInput, match="encoding"):
            await FileTool().execute({"action": "read", "path": str(f), "encoding": "nope-42"})


# ------------------------------------------------------------------ queue


class TestQueueHardening:
    def test_payload_is_deep_copied(self) -> None:
        queue = InMemoryQueue()
        queue.get_or_create_queue("q")
        payload = {"nested": {"values": [1, 2]}}
        item = queue.add("q", payload)
        item.payload["nested"]["values"].append(3)
        claimed = queue.claim("q", run_id="r")
        assert claimed is not None
        assert claimed.payload["nested"]["values"] == [1, 2]

    def test_purge_terminal_removes_items(self) -> None:
        queue = InMemoryQueue()
        queue.get_or_create_queue("q")
        item = queue.add("q", {"x": 1})
        queue.claim("q", run_id="r")
        queue.set_status(item.id, "success", run_id="r")
        assert queue.purge_terminal() == 1
        assert queue.purge_terminal() == 0


class TestRetryValidation:
    def test_bool_attempts_rejected(self) -> None:
        class Dummy(AbstractTool):
            @property
            def name(self) -> str:
                return "dummy"

            @property
            def description(self) -> str:
                return "dummy"

            def schema(self) -> dict[str, Any]:
                return {}

            async def execute(self, config: dict[str, Any]) -> Any:
                return None

        with pytest.raises(InvalidInput, match="attempts"):
            RetryTool(Dummy(), attempts=True)  # type: ignore[arg-type]
        with pytest.raises(InvalidInput, match="delay_ms"):
            RetryTool(Dummy(), delay_ms="fast")  # type: ignore[arg-type]


# ------------------------------------------------------------------ flow


def _doc(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 2, "nodes": nodes, "edges": edges}


def _edge(source: str, target: str, handle: str = "out") -> dict[str, Any]:
    return {
        "id": f"{source}-{target}-{handle}",
        "source": source,
        "source_handle": handle,
        "target": target,
    }


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    @tool("boom")
    async def boom(config: dict[str, Any]) -> dict[str, Any]:
        raise ZeroDivisionError("kaboom")

    reg.register(boom)
    return reg


class TestFlowHardening:
    @pytest.mark.asyncio
    async def test_cycle_raises_instead_of_hanging(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "a", "kind": "set", "config": {"var": "x", "value": 1}},
                {"id": "b", "kind": "set", "config": {"var": "y", "value": 2}},
            ],
            [_edge("s", "a"), _edge("a", "b"), _edge("b", "a")],
        )
        runner = FlowRunner(ToolRegistry(), max_steps=50)
        with pytest.raises(FlowError, match="cycle"):
            await asyncio.wait_for(runner.run(doc), timeout=5)

    @pytest.mark.asyncio
    async def test_dangling_edge_raises(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            [_edge("s", "ghost")],
        )
        with pytest.raises(FlowError, match="not a node"):
            await FlowRunner(ToolRegistry()).run(doc)

    @pytest.mark.asyncio
    async def test_error_edge_routes_and_saves(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "tool", "tool": "boom", "config": {}},
                {"id": "r", "kind": "set", "config": {"var": "recovered", "value": True}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            [_edge("s", "t"), _edge("t", "r", "error"), _edge("r", "e")],
        )
        variables: dict[str, Any] = {}
        result = await FlowRunner(_registry(), variables=variables).run(doc)
        assert result == "finished"
        assert variables["recovered"] is True
        assert "ZeroDivisionError" in variables["_error"]

    @pytest.mark.asyncio
    async def test_on_error_continue_on_set_node(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "n",
                    "kind": "set",
                    "config": {"var": "x", "value": "not-a-number", "type": "number"},
                    "on_error": {"policy": "continue", "save_error_as": "err"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            [_edge("s", "n"), _edge("n", "e")],
        )
        variables: dict[str, Any] = {}
        await FlowRunner(ToolRegistry(), variables=variables).run(doc)
        assert "err" in variables

    @pytest.mark.asyncio
    async def test_empty_edges_do_not_reuse_previous(self) -> None:
        tool_ran = False

        @tool("probe")
        async def probe(config: dict[str, Any]) -> dict[str, Any]:
            nonlocal tool_ran
            tool_ran = True
            return {}

        reg = ToolRegistry()
        reg.register(probe)
        runner = FlowRunner(reg, edges=[_edge("s", "t")])
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "tool", "tool": "probe", "config": {}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            [],
        )
        await runner.run(doc)
        assert tool_ran is False

    def test_parse_set_value_preserves_dict(self) -> None:
        assert parse_set_value({"a": 1}, "auto", {}) == {"a": 1}

    def test_validate_requires_node_shapes(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "i", "kind": "if", "config": {}},
                {"id": "l", "kind": "loop", "loop": {}},
                {"id": "x", "kind": "set", "config": {}},
            ],
            [],
        )
        problems = validate_document(doc)
        joined = "\n".join(problems)
        assert "condition" in joined
        assert "loop.mode" in joined
        assert "config.var" in joined

    def test_validate_skips_interpolated_config(self) -> None:
        class IntTool(AbstractTool):
            @property
            def name(self) -> str:
                return "needs_int"

            @property
            def description(self) -> str:
                return "needs an int"

            def schema(self) -> dict[str, Any]:
                return {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                    "required": ["n"],
                }

            async def execute(self, config: dict[str, Any]) -> Any:
                return None

        reg = ToolRegistry()
        reg.register(IntTool())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "tool", "tool": "needs_int", "config": {"n": "$delay"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            [_edge("s", "t"), _edge("t", "e")],
        )
        assert validate_document(doc, registry=reg) == []


# ------------------------------------------------------------------ keyboard


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="x64-specific INPUT layout check")
def test_input_struct_matches_native_size() -> None:
    assert ctypes.sizeof(_INPUT) == 40


# ------------------------------------------------------------------ process


class TestProcessHardening:
    def test_path_qualified_command_must_be_on_path(self, tmp_path: Path) -> None:
        from smithcore.windows.tools.process import _resolve_command_path

        planted = tmp_path / "notepad.exe"
        planted.write_bytes(b"MZ")
        with pytest.raises(InvalidInput):
            _resolve_command_path(str(planted))

    @pytest.mark.asyncio
    async def test_stop_by_pid_requires_allowlisted_image(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from smithcore.windows.tools.process import ProcessTool

        monkeypatch.setattr(
            "smithcore.windows.tools.process._query_image_name", lambda pid: "lsass.exe"
        )
        with pytest.raises(InvalidInput, match="allowed list"):
            await ProcessTool().execute({"action": "stop", "pid": 4})


# ------------------------------------------------------------------ pack


class TestPackHardening:
    def _make_pack(self, tmp_path: Path) -> Path:
        from smithcore.pack import build_pack

        root = tmp_path / "pack"
        root.mkdir()
        (root / "process.flow.json").write_text(
            json.dumps({"version": 2, "nodes": [], "edges": []}), encoding="utf-8"
        )
        build_pack(root, name="p", version="1.0.0")
        return root

    def test_verify_rejects_extra_file(self, tmp_path: Path) -> None:
        from smithcore.pack import verify_pack

        root = self._make_pack(tmp_path)
        (root / "tools.py").write_text("# planted\n", encoding="utf-8")
        problems = verify_pack(root)
        assert any("not in the manifest" in problem for problem in problems)

    def test_verify_rejects_manifest_traversal(self, tmp_path: Path) -> None:
        from smithcore.pack import PACK_MANIFEST, verify_pack

        root = self._make_pack(tmp_path)
        manifest = json.loads((root / PACK_MANIFEST).read_text(encoding="utf-8"))
        manifest["files"].append({"path": "../evil", "sha256": "0" * 64})
        (root / PACK_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        problems = verify_pack(root)
        assert any("unsafe path" in problem for problem in problems)

    def test_fetch_replaces_stale_destination(self, tmp_path: Path) -> None:
        from smithcore.pack import fetch_pack, zip_pack

        root = self._make_pack(tmp_path)
        archive = zip_pack(root, out=tmp_path / "p.zip")
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "tools.py").write_text("# stale\n", encoding="utf-8")
        fetch_pack(str(archive), dest)
        assert not (dest / "tools.py").exists()
        assert (dest / "pack.json").is_file()

    def test_manifest_lists_file(self) -> None:
        from smithcore.pack import manifest_lists_file

        manifest = {"files": [{"path": "tools.py", "sha256": "0" * 64}]}
        assert manifest_lists_file(manifest, "tools.py") is True
        assert manifest_lists_file(manifest, "main.py") is False


# ------------------------------------------------------------------ run_flow


class TestRunFlowTransactional:
    def test_cli_vars_and_payload_reach_the_flow(self, tmp_path: Path) -> None:
        import sqlite3

        from smithcore.core.queue import SqliteQueue
        from smithcore.run_flow import main

        flow = tmp_path / "process.flow.json"
        flow.write_text(
            json.dumps(
                {
                    "version": 2,
                    "nodes": [
                        {"id": "s", "kind": "start", "config": {}},
                        {"id": "e", "kind": "end", "config": {}},
                    ],
                    "edges": [_edge("s", "e")],
                }
            ),
            encoding="utf-8",
        )
        db = tmp_path / "q.db"
        queue = SqliteQueue(db)
        queue.get_or_create_queue("q")
        queue.add("q", {"item_var": "payload"})
        queue.close()

        rc = main(
            [
                str(flow),
                "--transactional",
                "--queue",
                "q",
                "--db",
                str(db),
                "--set",
                "cli_var=cli",
            ]
        )
        assert rc == 0
        with sqlite3.connect(db) as conn:
            row = conn.execute("SELECT result FROM items").fetchone()
        assert row is not None and row[0] is not None
        result = json.loads(row[0])
        assert result["item_var"] == "payload"
        assert result["cli_var"] == "cli"
