"""Tests for smithy.run_flow CLI: --validate mode and exit codes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from smithy import run_flow


def _write(tmp_path: Path, doc: dict[str, Any], name: str = "flow.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _doc(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 2, "nodes": nodes, "edges": edges}


def _chain(*ids: str) -> list[dict[str, Any]]:
    return [
        {"id": f"e{i}", "source": ids[i], "source_handle": "out", "target": ids[i + 1]}
        for i in range(len(ids) - 1)
    ]


def _clean_doc() -> dict[str, Any]:
    return _doc(
        [
            {"id": "s", "kind": "start", "config": {}},
            {"id": "x", "kind": "set", "config": {"var": "a", "value": "1"}},
            {"id": "e", "kind": "end", "config": {}},
        ],
        _chain("s", "x", "e"),
    )


class TestValidateMode:
    def test_valid_document_passes(self, tmp_path: Path, capsys: Any) -> None:
        path = _write(tmp_path, _clean_doc())
        assert run_flow.main([str(path), "--validate"]) == 0
        assert "validation passed" in capsys.readouterr().out

    def test_broken_document_fails(self, tmp_path: Path) -> None:
        doc = _doc(
            [
                {"id": "t", "kind": "tool", "tool": "windows.nonexistent", "config": {}},
            ],
            [],
        )
        path = _write(tmp_path, doc)
        assert run_flow.main([str(path), "--validate"]) == 1

    def test_unreadable_file_fails(self, tmp_path: Path) -> None:
        assert run_flow.main([str(tmp_path / "nope.json"), "--validate"]) == 1


class TestDocumentVariables:
    def test_variables_seed_flow_defaults(self, tmp_path: Path, capsys: Any) -> None:
        doc = {
            "version": 2,
            "variables": {"who": "world"},
            "nodes": [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "bad",
                    "kind": "fail",
                    "config": {"mode": "business", "message": "hi $who"},
                },
            ],
            "edges": _chain("s", "bad"),
        }
        path = _write(tmp_path, doc)
        assert run_flow.main([str(path)]) == 1
        assert "hi world" in capsys.readouterr().err

    def test_variables_must_be_an_object(self, tmp_path: Path) -> None:
        doc = _doc([{"id": "s", "kind": "start", "config": {}}], [])
        doc["variables"] = "nope"
        path = _write(tmp_path, doc)
        assert run_flow.main([str(path), "--validate"]) == 1

    def test_variables_list_seeds_typed_defaults(self, tmp_path: Path, capsys: Any) -> None:
        doc = {
            "version": 2,
            "variables": [
                {"name": "who", "type": "string", "value": "world"},
                {"name": "n", "type": "number", "value": "3"},
            ],
            "nodes": [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "bad",
                    "kind": "fail",
                    "config": {"mode": "business", "message": "$who $n"},
                },
            ],
            "edges": _chain("s", "bad"),
        }
        path = _write(tmp_path, doc)
        assert run_flow.main([str(path)]) == 1
        assert "world 3" in capsys.readouterr().err


class TestExitCodes:
    def test_finished_returns_zero(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _write(tmp_path, _clean_doc())
        monkeypatch.setenv("SMITHY_SELECTOR_STORE", str(tmp_path / "sel.json"))
        assert run_flow.main([str(path)]) == 0

    def test_flow_failure_returns_one(self, tmp_path: Path) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "bad", "kind": "tool", "tool": "windows.nonexistent", "config": {}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "e"),
        )
        path = _write(tmp_path, doc)
        assert run_flow.main([str(path)]) == 1

    def test_stopped_returns_two(self, tmp_path: Path, monkeypatch: Any) -> None:
        path = _write(tmp_path, _clean_doc())

        class FakeRunner:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def run(self, doc: dict[str, Any]) -> str:
                raise asyncio.CancelledError()

        monkeypatch.setattr(run_flow, "FlowRunner", FakeRunner)
        assert run_flow.main([str(path)]) == 2
