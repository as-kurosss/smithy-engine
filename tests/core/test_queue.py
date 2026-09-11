"""Conformance tests — every ``Queue`` backend must pass all of these."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.core.queue import InMemoryQueue, LeaseRenewable, Queue, SqliteQueue

RUN_ID = "run-1"
OTHER_RUN = "run-2"


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Queue]:
    """Fresh backend of each flavour."""
    if request.param == "memory":
        yield InMemoryQueue()
    else:
        queue = SqliteQueue(tmp_path / "queue.db")
        try:
            yield queue
        finally:
            queue.close()


def _seed(backend: Queue, name: str, count: int, **kwargs: Any) -> Queue:
    backend.get_or_create_queue(name, **kwargs)
    for index in range(count):
        backend.add(name, {"n": index})
    return backend


def test_get_or_create_is_idempotent(backend: Queue) -> None:
    first = backend.get_or_create_queue("jobs", max_attempts=2)
    second = backend.get_or_create_queue("jobs", max_attempts=5)
    assert (first.name, first.max_attempts) == ("jobs", 2)
    assert (second.name, second.max_attempts) == ("jobs", 2)


@pytest.mark.parametrize("bad", [0, -1, True, "3", 2.5])
def test_get_or_create_rejects_bad_budget(backend: Queue, bad: Any) -> None:
    with pytest.raises(InvalidInput):
        backend.get_or_create_queue("jobs", max_attempts=bad)


def test_claim_follows_fifo_and_counts_attempts(backend: Queue) -> None:
    _seed(backend, "jobs", 3)
    first = backend.claim("jobs", run_id=RUN_ID)
    second = backend.claim("jobs", run_id=RUN_ID)
    assert first is not None and second is not None
    assert (first.payload["n"], second.payload["n"]) == (0, 1)
    assert (first.attempts, second.attempts) == (1, 1)
    assert first.lease_expires_at.tzinfo is not None


def test_claim_empty_returns_none(backend: Queue) -> None:
    backend.get_or_create_queue("jobs")
    assert backend.claim("jobs", run_id=RUN_ID) is None


def test_unknown_queue_raises_key_error(backend: Queue) -> None:
    with pytest.raises(KeyError):
        backend.add("nope", {"n": 1})
    with pytest.raises(KeyError):
        backend.claim("nope", run_id=RUN_ID)


@pytest.mark.parametrize("bad", [0, -2, True, "60"])
def test_claim_rejects_bad_lease(backend: Queue, bad: Any) -> None:
    backend.get_or_create_queue("jobs")
    with pytest.raises(InvalidInput):
        backend.claim("jobs", run_id=RUN_ID, lease_seconds=bad)


def test_add_idempotency_key_returns_existing_row(backend: Queue) -> None:
    backend.get_or_create_queue("jobs")
    first = backend.add("jobs", {"n": 1}, idempotency_key="order-7")
    second = backend.add("jobs", {"n": 999}, idempotency_key="order-7")
    assert first.id == second.id
    claimed = backend.claim("jobs", run_id=RUN_ID)
    assert claimed is not None and claimed.payload == {"n": 1}
    assert backend.claim("jobs", run_id=RUN_ID) is None


def test_idempotency_keys_are_scoped_per_queue(backend: Queue) -> None:
    backend.get_or_create_queue("a")
    backend.get_or_create_queue("b")
    first = backend.add("a", {"n": 1}, idempotency_key="k")
    second = backend.add("b", {"n": 1}, idempotency_key="k")
    assert first.id != second.id


def test_success_is_terminal(backend: Queue) -> None:
    _seed(backend, "jobs", 1)
    claimed = backend.claim("jobs", run_id=RUN_ID)
    assert claimed is not None
    view = backend.set_status(claimed.id, "success", run_id=RUN_ID, result={"ok": True})
    assert (view.status, view.attempts) == ("success", 1)
    assert backend.claim("jobs", run_id=RUN_ID) is None


def test_business_failed_is_terminal(backend: Queue) -> None:
    _seed(backend, "jobs", 1, max_attempts=3)
    claimed = backend.claim("jobs", run_id=RUN_ID)
    assert claimed is not None
    view = backend.set_status(claimed.id, "business_failed", run_id=RUN_ID, error="bad row")
    assert view.status == "business_failed"
    assert backend.claim("jobs", run_id=RUN_ID) is None


def test_system_failed_requeues_within_budget_then_terminal(backend: Queue) -> None:
    _seed(backend, "jobs", 1, max_attempts=2)
    first = backend.claim("jobs", run_id=RUN_ID)
    assert first is not None and first.attempts == 1
    requeued = backend.set_status(first.id, "system_failed", run_id=RUN_ID, error="boom")
    assert requeued.status == "new"
    second = backend.claim("jobs", run_id=RUN_ID)
    assert second is not None and second.id == first.id and second.attempts == 2
    terminal = backend.set_status(second.id, "system_failed", run_id=RUN_ID, error="boom")
    assert terminal.status == "system_failed"
    assert backend.claim("jobs", run_id=RUN_ID) is None


def test_stale_writer_is_rejected(backend: Queue) -> None:
    _seed(backend, "jobs", 1)
    claimed = backend.claim("jobs", run_id=RUN_ID)
    assert claimed is not None
    with pytest.raises(InvalidInput):
        backend.set_status(claimed.id, "success", run_id=OTHER_RUN)
    view = backend.set_status(claimed.id, "success", run_id=RUN_ID)
    assert view.status == "success"


def test_set_status_unknown_item_raises_key_error(backend: Queue) -> None:
    backend.get_or_create_queue("jobs")
    with pytest.raises(KeyError):
        backend.set_status("missing", "success", run_id=RUN_ID)


def test_renew_lease_extends_expiry(backend: Queue) -> None:
    assert isinstance(backend, LeaseRenewable)
    _seed(backend, "jobs", 1)
    claimed = backend.claim("jobs", run_id=RUN_ID, lease_seconds=60)
    assert claimed is not None
    renewed = backend.renew_lease(claimed.id, run_id=RUN_ID, lease_seconds=120)
    assert renewed > claimed.lease_expires_at


def test_renew_lease_rejects_foreign_run(backend: Queue) -> None:
    assert isinstance(backend, LeaseRenewable)
    _seed(backend, "jobs", 1)
    claimed = backend.claim("jobs", run_id=RUN_ID)
    assert claimed is not None
    with pytest.raises(InvalidInput):
        backend.renew_lease(claimed.id, run_id=OTHER_RUN)


def test_renew_lease_unknown_item_raises_key_error(backend: Queue) -> None:
    assert isinstance(backend, LeaseRenewable)
    backend.get_or_create_queue("jobs")
    with pytest.raises(KeyError):
        backend.renew_lease("missing", run_id=RUN_ID)


@pytest.mark.parametrize("bad", [0, -2, True, "60"])
def test_renew_lease_rejects_bad_lease(backend: Queue, bad: Any) -> None:
    assert isinstance(backend, LeaseRenewable)
    _seed(backend, "jobs", 1)
    claimed = backend.claim("jobs", run_id=RUN_ID)
    assert claimed is not None
    with pytest.raises(InvalidInput):
        backend.renew_lease(claimed.id, run_id=RUN_ID, lease_seconds=bad)


def test_renew_expired_but_unclaimed_succeeds(backend: Queue) -> None:
    assert isinstance(backend, LeaseRenewable)
    _seed(backend, "jobs", 1)
    claimed = backend.claim("jobs", run_id=RUN_ID, lease_seconds=1)
    assert claimed is not None
    time.sleep(1.2)
    renewed = backend.renew_lease(claimed.id, run_id=RUN_ID, lease_seconds=60)
    assert renewed.tzinfo is not None


def test_expired_lease_returns_item_to_new(backend: Queue) -> None:
    _seed(backend, "jobs", 1)
    claimed = backend.claim("jobs", run_id=RUN_ID, lease_seconds=1)
    assert claimed is not None
    deadline = time.monotonic() + 5.0
    recovered = None
    while time.monotonic() < deadline:
        recovered = backend.claim("jobs", run_id=OTHER_RUN)
        if recovered is not None:
            break
        time.sleep(0.05)
    assert recovered is not None and recovered.id == claimed.id
