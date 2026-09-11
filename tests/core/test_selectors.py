"""Tests for smithcore.core.selectors — SelectorStore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smithcore.core.selectors import SelectorStore


class TestSelectorStore:
    def test_put_get_persist(self, tmp_path: Path) -> None:
        store = SelectorStore(tmp_path / "selectors.json")
        store.put("login.submit", {"name": "OK", "control_type": "button"})
        assert store.get("login.submit") == {"name": "OK", "control_type": "button"}
        assert "login.submit" in store

        reloaded = SelectorStore(tmp_path / "selectors.json")
        assert reloaded.get("login.submit") == {"name": "OK", "control_type": "button"}

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        store = SelectorStore(tmp_path / "selectors.json")
        assert store.get("nope") is None
        assert "nope" not in store
        assert len(store) == 0

    def test_put_overwrites(self, tmp_path: Path) -> None:
        store = SelectorStore(tmp_path / "selectors.json")
        store.put("k", {"name": "old"})
        store.put("k", {"automation_id": "new"})
        assert store.get("k") == {"automation_id": "new"}

    def test_file_format(self, tmp_path: Path) -> None:
        store = SelectorStore(tmp_path / "sel.json")
        store.put("k", {"name": "x"})
        document = json.loads((tmp_path / "sel.json").read_text(encoding="utf-8"))
        assert document["k"]["selector"] == {"name": "x"}
        assert "updated_at" in document["k"]

    def test_survives_corrupt_file(self, tmp_path: Path) -> None:
        path = tmp_path / "selectors.json"
        path.write_text("{not json", encoding="utf-8")
        store = SelectorStore(path)
        assert store.get("k") is None
        store.put("k", {"name": "fresh"})
        assert json.loads(path.read_text(encoding="utf-8"))["k"]["selector"] == {"name": "fresh"}

    def test_ignores_malformed_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "selectors.json"
        path.write_text(
            json.dumps({"good": {"selector": {"name": "x"}}, "bad": 42}), encoding="utf-8"
        )
        store = SelectorStore(path)
        assert store.get("good") == {"name": "x"}
        assert store.get("bad") is None

    def test_keys_sorted(self, tmp_path: Path) -> None:
        store = SelectorStore(tmp_path / "selectors.json")
        store.put("b", {"name": "1"})
        store.put("a", {"name": "2"})
        assert store.keys() == ["a", "b"]

    @pytest.mark.parametrize(
        ("key", "selector"),
        [("", {"name": "x"}), ("k", {}), ("k", "not-a-dict")],
    )
    def test_put_validation(self, tmp_path: Path, key: str, selector: object) -> None:
        store = SelectorStore(tmp_path / "selectors.json")
        with pytest.raises(ValueError):
            store.put(key, selector)  # type: ignore[arg-type]

    def test_get_invalid_key(self, tmp_path: Path) -> None:
        store = SelectorStore(tmp_path / "selectors.json")
        with pytest.raises(ValueError):
            store.get("")

    def test_atomic_write(self, tmp_path: Path) -> None:
        path = tmp_path / "selectors.json"
        store = SelectorStore(path)
        store.put("k", {"name": "x"})
        assert not (tmp_path / "selectors.json.tmp").exists()
