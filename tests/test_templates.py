"""Template packs: descriptor loading, validation and manifest embedding."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.pack import build_pack, load_template, validate_template


def _flow() -> dict[str, object]:
    return {
        "version": 2,
        "nodes": [
            {"id": "s", "kind": "start", "config": {}},
            {"id": "e", "kind": "end", "config": {}},
        ],
        "edges": [{"id": "x", "source": "s", "source_handle": "out", "target": "e"}],
    }


def _pack(
    tmp_path: Path,
    *,
    flow_name: str = "flow.json",
    template: dict[str, object] | None = None,
) -> Path:
    root = tmp_path / "pack"
    root.mkdir()
    (root / flow_name).write_text(json.dumps(_flow()), encoding="utf-8")
    if template is not None:
        (root / "template.json").write_text(json.dumps(template), encoding="utf-8")
    return root


def test_flow_json_is_used_as_process_stage(tmp_path: Path) -> None:
    root = _pack(tmp_path)
    build_pack(root, name="demo", version="1.0.0")
    manifest = json.loads((root / "pack.json").read_text(encoding="utf-8"))
    assert manifest["entry"] == {"process": "flow.json"}


def test_template_summary_is_embedded_in_manifest(tmp_path: Path) -> None:
    template = {
        "schema": "smithcore-template-v1",
        "title": "Notepad demo",
        "category": "Demo",
        "params": [{"name": "text", "type": "string", "default": "hi"}],
    }
    root = _pack(tmp_path, template=template)
    build_pack(root, name="demo", version="1.0.0")
    manifest = json.loads((root / "pack.json").read_text(encoding="utf-8"))
    assert manifest["template"]["title"] == "Notepad demo"
    assert manifest["template"]["params"] == [{"name": "text", "type": "string", "default": "hi"}]


def test_malformed_template_fails_build(tmp_path: Path) -> None:
    root = _pack(tmp_path, template={"params": "not-a-list"})
    with pytest.raises(InvalidInput):
        build_pack(root, name="demo", version="1.0.0")


def test_validate_template_reports_problems() -> None:
    problems = validate_template(
        {"params": [{"name": "a"}, {"name": "a", "type": "nope"}, "x"]}
    )
    assert any("title" in item for item in problems)
    assert any("duplicate" in item for item in problems)
    assert any("unknown type" in item for item in problems)
    assert any("object" in item for item in problems)


def test_load_template_absent_is_none(tmp_path: Path) -> None:
    assert load_template(_pack(tmp_path)) is None


def test_load_template_reads_valid_file(tmp_path: Path) -> None:
    root = _pack(tmp_path, template={"title": "T", "params": []})
    loaded = load_template(root)
    assert loaded is not None and loaded["title"] == "T"
