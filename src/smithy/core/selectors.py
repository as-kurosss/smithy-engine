"""Selector registry — stable keys for UI selectors, persisted as JSON.

Bots under development reference elements by *keys* instead of inline
selector dicts::

    await bot.click(key="login.submit")

The store resolves keys to selector configs and is what makes the
dev-capture workflow possible: a key that is already stored runs
silently; a missing or stale key triggers an interactive capture in
dev mode and persists the result for every next run.

The default file name (``selectors.json``) is machine-specific and is
typically git-ignored; teams can commit it if they prefer shared
selectors.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SelectorStore:
    """Key → selector registry backed by a JSON file.

    Thread-safe; reads are served from an in-memory snapshot, writes
    update the snapshot and persist immediately (a crash mid-debugging
    must not lose recorded selectors).
    """

    def __init__(self, path: str | Path = "selectors.json") -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def path(self) -> Path:
        """Backing JSON file."""
        return self._path

    def get(self, key: str) -> dict[str, Any] | None:
        """Selector config stored for *key*, or ``None``."""
        if not isinstance(key, str) or not key:
            raise ValueError("selector key must be a non-empty string")
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            return dict(entry["selector"])

    def put(self, key: str, selector: dict[str, Any]) -> None:
        """Store (or overwrite) the selector for *key* and persist."""
        if not isinstance(key, str) or not key:
            raise ValueError("selector key must be a non-empty string")
        if not isinstance(selector, dict) or not selector:
            raise ValueError("selector must be a non-empty dict")
        record: dict[str, Any] = {
            "selector": dict(selector),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with self._lock:
            self._entries[key] = record
            self._save_locked()

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def keys(self) -> list[str]:
        """All stored keys, sorted."""
        with self._lock:
            return sorted(self._entries)

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Cannot read selector store %s: %s", self._path, exc)
            return
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON in selector store %s: %s", self._path, exc)
            return
        if not isinstance(document, dict):
            logger.warning("Selector store %s must hold an object", self._path)
            return
        for key, value in document.items():
            if isinstance(value, dict) and isinstance(value.get("selector"), dict):
                self._entries[str(key)] = value

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)
