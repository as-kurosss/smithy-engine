"""Transaction queue protocol and local backends.

A queue holds transaction payloads; workers :meth:`claim` them one at a
time and report the outcome with :meth:`set_status`. Status strings match
the smithy-cloud contract exactly:

- ``new`` — waiting for a worker.
- ``in_progress`` — claimed, protected by a lease.
- ``success`` / ``business_failed`` — terminal.
- ``system_failed`` — requeued while ``attempts < max_attempts``,
  terminal afterwards.
"""

from __future__ import annotations

import heapq
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from smithy.core.errors import InvalidInput

ItemStatus = Literal["new", "in_progress", "success", "business_failed", "system_failed"]
FinalStatus = Literal["success", "business_failed", "system_failed"]

TERMINAL_STATUSES: tuple[FinalStatus, ...] = ("success", "business_failed", "system_failed")


@dataclass(frozen=True)
class QueueInfo:
    """Named queue and its retry budget."""

    name: str
    max_attempts: int


@dataclass(frozen=True)
class QueueItem:
    """Point-in-time view of a queue row."""

    id: str
    queue: str
    payload: dict[str, Any]
    status: ItemStatus
    attempts: int


@dataclass(frozen=True)
class ClaimedItem:
    """Item leased to a worker for one processing attempt."""

    id: str
    queue: str
    payload: dict[str, Any]
    attempts: int
    lease_expires_at: datetime


class Queue(Protocol):
    """Worker-facing queue contract (local and HTTP backends)."""

    def get_or_create_queue(self, name: str, *, max_attempts: int = 3) -> QueueInfo:
        """Return the queue, creating it with *max_attempts* if missing."""
        ...

    def add(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> QueueItem:
        """Append *payload*; a duplicate *idempotency_key* returns the existing row."""
        ...

    def claim(self, queue: str, *, run_id: str, lease_seconds: int = 300) -> ClaimedItem | None:
        """Lease the oldest ``new`` item (resetting expired leases first)."""
        ...

    def set_status(
        self,
        item_id: str,
        status: FinalStatus,
        *,
        run_id: str,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> QueueItem:
        """Record the outcome; ``system_failed`` requeues within budget.

        *run_id* must own the claim — a stale writer gets ``InvalidInput``
        (HTTP ``409``), never silently overwrites another run.
        """
        ...


@runtime_checkable
class LeaseRenewable(Protocol):
    """Optional queue capability: extend a claim's lease while work continues.

    Backends without this method simply run without a heartbeat — the
    runner checks support with ``isinstance(queue, LeaseRenewable)``.
    """

    def renew_lease(self, item_id: str, *, run_id: str, lease_seconds: int = 300) -> datetime:
        """Push *lease_expires_at* forward; the claim must belong to *run_id*.

        Raises ``KeyError`` for an unknown item and ``InvalidInput`` when
        another run owns the claim. Renewing an expired-but-unclaimed lease
        succeeds — the owner hasn't changed.
        """
        ...


def _check_max_attempts(max_attempts: int) -> None:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise InvalidInput("max_attempts must be an int", param="max_attempts")
    if max_attempts < 1:
        raise InvalidInput("max_attempts must be >= 1", param="max_attempts")


def _check_lease(lease_seconds: int) -> None:
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
        raise InvalidInput("lease_seconds must be an int", param="lease_seconds")
    if lease_seconds < 1:
        raise InvalidInput("lease_seconds must be >= 1", param="lease_seconds")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ----------------------------------------------------------------------
# In-memory backend (tests, ephemeral runs)
# ----------------------------------------------------------------------


@dataclass
class _Record:
    id: str
    queue: str
    payload: dict[str, Any]
    status: ItemStatus = "new"
    attempts: int = 0
    run_id: str | None = None
    lease_expires_at: datetime | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    seq: int = 0


class InMemoryQueue:
    """Thread-safe in-process queue with full retry/lease semantics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, int] = {}
        self._items: dict[str, _Record] = {}
        self._keys: dict[tuple[str, str], str] = {}
        self._seq = 0
        # Min-heap of (seq, item_id) per queue — makes claim O(log n)
        # instead of a full scan. Entries for items that are no longer
        # "new" are skipped lazily on pop.
        self._new: dict[str, list[tuple[int, str]]] = {}

    def get_or_create_queue(self, name: str, *, max_attempts: int = 3) -> QueueInfo:
        _check_max_attempts(max_attempts)
        with self._lock:
            self._queues.setdefault(name, max_attempts)
            return QueueInfo(name=name, max_attempts=self._queues[name])

    def add(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> QueueItem:
        with self._lock:
            self._require_queue(queue)
            if idempotency_key is not None and (queue, idempotency_key) in self._keys:
                return self._view(self._items[self._keys[(queue, idempotency_key)]])
            self._seq += 1
            record = _Record(
                id=uuid.uuid4().hex,
                queue=queue,
                payload=dict(payload),
                seq=self._seq,
            )
            self._items[record.id] = record
            if idempotency_key is not None:
                self._keys[(queue, idempotency_key)] = record.id
            heapq.heappush(self._new.setdefault(queue, []), (record.seq, record.id))
            return self._view(record)

    def claim(self, queue: str, *, run_id: str, lease_seconds: int = 300) -> ClaimedItem | None:
        _check_lease(lease_seconds)
        now = _utcnow()
        with self._lock:
            self._require_queue(queue)
            self._reset_expired_locked(queue, now)
            heap = self._new.get(queue, [])
            record: _Record | None = None
            while heap:
                _seq, item_id = heapq.heappop(heap)
                candidate = self._items.get(item_id)
                if candidate is not None and candidate.queue == queue and candidate.status == "new":
                    record = candidate
                    break
            if record is None:
                return None
            record.status = "in_progress"
            record.run_id = run_id
            record.attempts += 1
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.error = None
            record.result = None
            lease = record.lease_expires_at
            return ClaimedItem(
                id=record.id,
                queue=queue,
                payload=dict(record.payload),
                attempts=record.attempts,
                lease_expires_at=lease,
            )

    def set_status(
        self,
        item_id: str,
        status: FinalStatus,
        *,
        run_id: str,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> QueueItem:
        with self._lock:
            record = self._items.get(item_id)
            if record is None:
                raise KeyError(f"Unknown queue item: {item_id!r}")
            self._check_owner(record, run_id)
            if status == "system_failed" and record.attempts < self._queues[record.queue]:
                record.status = "new"
                record.run_id = None
                record.lease_expires_at = None
                heapq.heappush(self._new.setdefault(record.queue, []), (record.seq, record.id))
            else:
                record.status = status
            record.error = error
            record.result = None if result is None else dict(result)
            return self._view(record)

    def renew_lease(self, item_id: str, *, run_id: str, lease_seconds: int = 300) -> datetime:
        _check_lease(lease_seconds)
        now = _utcnow()
        with self._lock:
            record = self._items.get(item_id)
            if record is None:
                raise KeyError(f"Unknown queue item: {item_id!r}")
            self._check_owner(record, run_id)
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return record.lease_expires_at

    # -- internals -----------------------------------------------------

    def _require_queue(self, queue: str) -> None:
        if queue not in self._queues:
            raise KeyError(f"Unknown queue: {queue!r}")

    @staticmethod
    def _check_owner(record: _Record, run_id: str) -> None:
        if record.status != "in_progress" or record.run_id != run_id:
            raise InvalidInput(
                f"Queue item {record.id!r} is not claimed by this run",
                param="run_id",
                input_value=run_id,
            )

    def _reset_expired_locked(self, queue: str, now: datetime) -> None:
        heap = self._new.setdefault(queue, [])
        for record in self._items.values():
            if (
                record.queue == queue
                and record.status == "in_progress"
                and record.lease_expires_at is not None
                and record.lease_expires_at < now
            ):
                record.status = "new"
                record.run_id = None
                record.lease_expires_at = None
                heapq.heappush(heap, (record.seq, record.id))

    @staticmethod
    def _view(record: _Record) -> QueueItem:
        return QueueItem(
            id=record.id,
            queue=record.queue,
            payload=dict(record.payload),
            status=record.status,
            attempts=record.attempts,
        )


# ----------------------------------------------------------------------
# SQLite backend (local runs with durability)
# ----------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queues (
    name TEXT PRIMARY KEY,
    max_attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    queue TEXT NOT NULL REFERENCES queues(name),
    payload TEXT NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    lease_expires_at TEXT,
    error TEXT,
    result TEXT,
    seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (queue, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_items_queue_status ON items (queue, status);
CREATE INDEX IF NOT EXISTS ix_items_seq ON items (seq);
"""


class SqliteQueue:
    """File-backed queue with the same semantics as :class:`InMemoryQueue`."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()

    def get_or_create_queue(self, name: str, *, max_attempts: int = 3) -> QueueInfo:
        _check_max_attempts(max_attempts)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT max_attempts FROM queues WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO queues (name, max_attempts) VALUES (?, ?)",
                    (name, max_attempts),
                )
                return QueueInfo(name=name, max_attempts=max_attempts)
            return QueueInfo(name=name, max_attempts=int(row["max_attempts"]))

    def add(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> QueueItem:
        with self._lock, self._conn:
            self._require_queue(queue)
            if idempotency_key is not None:
                row = self._conn.execute(
                    "SELECT id FROM items WHERE queue = ? AND idempotency_key = ?",
                    (queue, idempotency_key),
                ).fetchone()
                if row is not None:
                    return self._view(self._get(row["id"]))
            item_id = uuid.uuid4().hex
            seq = self._next_seq_locked()
            self._conn.execute(
                "INSERT INTO items (id, queue, payload, idempotency_key, status,"
                " attempts, seq, updated_at) VALUES (?, ?, ?, ?, 'new', 0, ?, ?)",
                (item_id, queue, json.dumps(payload), idempotency_key, seq, _utcnow().isoformat()),
            )
            return self._view(self._get(item_id))

    def claim(self, queue: str, *, run_id: str, lease_seconds: int = 300) -> ClaimedItem | None:
        _check_lease(lease_seconds)
        now = _utcnow()
        with self._lock, self._conn:
            self._require_queue(queue)
            self._conn.execute(
                "UPDATE items SET status = 'new', run_id = NULL, lease_expires_at = NULL,"
                " updated_at = ? WHERE queue = ? AND status = 'in_progress'"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (now.isoformat(), queue, now.isoformat()),
            )
            row = self._conn.execute(
                "SELECT id FROM items WHERE queue = ? AND status = 'new' ORDER BY seq LIMIT 1",
                (queue,),
            ).fetchone()
            if row is None:
                return None
            lease = now + timedelta(seconds=lease_seconds)
            self._conn.execute(
                "UPDATE items SET status = 'in_progress', run_id = ?, attempts = attempts + 1,"
                " lease_expires_at = ?, error = NULL, result = NULL, updated_at = ?"
                " WHERE id = ?",
                (run_id, lease.isoformat(), now.isoformat(), row["id"]),
            )
            current = self._get(row["id"])
            if current["lease_expires_at"] is None:
                raise ValueError(f"Claimed item {row['id']!r} has no lease timestamp")
            return ClaimedItem(
                id=current["id"],
                queue=queue,
                payload=json.loads(str(current["payload"])),
                attempts=int(current["attempts"]),
                lease_expires_at=datetime.fromisoformat(str(current["lease_expires_at"])),
            )

    def set_status(
        self,
        item_id: str,
        status: FinalStatus,
        *,
        run_id: str,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> QueueItem:
        with self._lock, self._conn:
            row = self._get(item_id)
            if str(row["status"]) != "in_progress" or row["run_id"] != run_id:
                raise InvalidInput(
                    f"Queue item {item_id!r} is not claimed by this run",
                    param="run_id",
                    input_value=run_id,
                )
            budget = self._max_attempts_locked(str(row["queue"]))
            if status == "system_failed" and int(row["attempts"]) < budget:
                self._conn.execute(
                    "UPDATE items SET status = 'new', run_id = NULL, lease_expires_at = NULL,"
                    " error = ?, result = ?, updated_at = ? WHERE id = ?",
                    (error, _dump_result(result), _utcnow().isoformat(), item_id),
                )
            else:
                self._conn.execute(
                    "UPDATE items SET status = ?, error = ?, result = ?, updated_at = ?"
                    " WHERE id = ?",
                    (status, error, _dump_result(result), _utcnow().isoformat(), item_id),
                )
            return self._view(self._get(item_id))

    def renew_lease(self, item_id: str, *, run_id: str, lease_seconds: int = 300) -> datetime:
        _check_lease(lease_seconds)
        now = _utcnow()
        with self._lock, self._conn:
            row = self._get(item_id)
            if str(row["status"]) != "in_progress" or row["run_id"] != run_id:
                raise InvalidInput(
                    f"Queue item {item_id!r} is not claimed by this run",
                    param="run_id",
                    input_value=run_id,
                )
            lease = now + timedelta(seconds=lease_seconds)
            self._conn.execute(
                "UPDATE items SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (lease.isoformat(), now.isoformat(), item_id),
            )
            return lease

    # -- internals -----------------------------------------------------

    def _require_queue(self, queue: str) -> None:
        row = self._conn.execute("SELECT 1 FROM queues WHERE name = ?", (queue,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown queue: {queue!r}")

    def _max_attempts_locked(self, queue: str) -> int:
        row = self._conn.execute(
            "SELECT max_attempts FROM queues WHERE name = ?", (queue,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown queue: {queue!r}")
        return int(row["max_attempts"])

    def _next_seq_locked(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM items").fetchone()
        return int(row["m"]) + 1

    def _get(self, item_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown queue item: {item_id!r}")
        return row

    @staticmethod
    def _view(row: sqlite3.Row) -> QueueItem:
        status = str(row["status"])
        if status not in TERMINAL_STATUSES and status not in ("new", "in_progress"):
            raise ValueError(f"Unknown item status in database: {status!r}")
        return QueueItem(
            id=str(row["id"]),
            queue=str(row["queue"]),
            payload=json.loads(str(row["payload"])),
            status=cast("ItemStatus", status),
            attempts=int(row["attempts"]),
        )


def _dump_result(result: dict[str, Any] | None) -> str | None:
    return None if result is None else json.dumps(result)
