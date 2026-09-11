"""``HttpQueue`` tests against an in-process stub of the cloud contract.

The stub wraps :class:`InMemoryQueue` and speaks the frozen wire shapes,
so these tests fail if the client drifts from the contract.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import pytest

from smithcore.core.errors import InvalidInput
from smithcore.core.http_queue import HttpQueue, HttpQueueError
from smithcore.core.queue import ClaimedItem, InMemoryQueue, QueueItem
from smithcore.core.transactions import run_transactions

AGENT_ID = "agent-1"
RUN_ID = "run-1"


class _StubServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        backend: InMemoryQueue,
    ) -> None:
        self.backend = backend
        super().__init__(address, _StubHandler)


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass

    @property
    def _backend(self) -> InMemoryQueue:
        return cast("_StubServer", self.server).backend

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw)
        assert isinstance(data, dict)
        return data

    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/")]
        body = self._read_json()
        backend = self._backend
        if parts == ["", "queues"]:
            name = str(body["name"])
            info = backend.get_or_create_queue(name, max_attempts=int(body["max_attempts"]))
            self._send(
                201,
                {
                    "id": info.name,
                    "name": info.name,
                    "max_attempts": info.max_attempts,
                    "created_at": "2026-01-01T00:00:00+00:00",
                },
            )
        elif len(parts) == 4 and parts[1] == "queues" and parts[3] == "items":
            try:
                created = [
                    backend.add(
                        parts[2],
                        dict(entry["payload"]),
                        idempotency_key=entry.get("idempotency_key"),
                    )
                    for entry in body["items"]
                ]
            except KeyError:
                self._send(404, {"detail": "Queue not found"})
                return
            self._send(201, [_dump(item) for item in created])
        elif (
            len(parts) == 6
            and parts[1] == "agents"
            and parts[3] == "queues"
            and parts[5] == "claim"
        ):
            try:
                claimed = backend.claim(
                    parts[4], run_id=str(body["run_id"]), lease_seconds=int(body["lease_seconds"])
                )
            except KeyError:
                self._send(404, {"detail": "Queue not found"})
                return
            if claimed is None:
                self._send(200, {"item": None})
            else:
                self._send(
                    200,
                    {
                        "item": {
                            "id": claimed.id,
                            "payload": claimed.payload,
                            "attempts": claimed.attempts,
                            "lease_expires_at": claimed.lease_expires_at.isoformat(),
                        }
                    },
                )
        else:
            self._send(404, {"detail": "Not found"})

    def do_PATCH(self) -> None:
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(self.path).path.split("/")]
        body = self._read_json()
        if (
            len(parts) == 6
            and parts[1] == "agents"
            and parts[3] == "queue-items"
            and parts[5] == "heartbeat"
        ):
            backend = self._backend
            try:
                lease = backend.renew_lease(
                    parts[4], run_id=str(body["run_id"]), lease_seconds=int(body["lease_seconds"])
                )
            except InvalidInput:
                self._send(409, {"detail": "Not claimed by this run"})
                return
            except KeyError:
                self._send(404, {"detail": "Item not found"})
                return
            self._send(200, {"id": parts[4], "lease_expires_at": lease.isoformat()})
        elif len(parts) == 5 and parts[1] == "agents" and parts[3] == "queue-items":
            backend = self._backend
            try:
                updated = backend.set_status(
                    parts[4],
                    body["status"],
                    run_id=str(body["run_id"]),
                    error=body.get("error"),
                    result=body.get("result"),
                )
            except InvalidInput:
                self._send(409, {"detail": "Not claimed by this run"})
                return
            except KeyError:
                self._send(404, {"detail": "Item not found"})
                return
            self._send(200, _dump(updated))
        else:
            self._send(404, {"detail": "Not found"})


def _dump(item: QueueItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "queue": item.queue,
        "payload": item.payload,
        "status": item.status,
        "attempts": item.attempts,
    }


@pytest.fixture
def stub() -> Iterator[tuple[str, InMemoryQueue]]:
    """Live stub base URL plus its backing queue (for cross-checks)."""
    backend = InMemoryQueue()
    server = _StubServer(("127.0.0.1", 0), backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        yield f"http://{host}:{port}", backend
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _client(base_url: str) -> HttpQueue:
    return HttpQueue(base_url, agent_id=AGENT_ID, token="secret", allow_insecure=True)


def test_full_flow_over_http(stub: tuple[str, InMemoryQueue]) -> None:
    base_url, _ = stub
    queue = _client(base_url)
    info = queue.get_or_create_queue("jobs", max_attempts=3)
    assert (info.name, info.max_attempts) == ("jobs", 3)
    queue.add("jobs", {"n": 0})
    queue.add("jobs", {"n": 1}, idempotency_key="k")

    first = queue.claim("jobs", run_id=RUN_ID)
    assert first is not None and first.payload == {"n": 0} and first.attempts == 1
    queue.set_status(first.id, "success", run_id=RUN_ID, result={"ok": True})

    second = queue.claim("jobs", run_id=RUN_ID)
    assert second is not None and second.payload == {"n": 1}
    queue.set_status(second.id, "business_failed", run_id=RUN_ID, error="bad")

    assert queue.claim("jobs", run_id=RUN_ID) is None


def test_system_failed_requeues_over_http(stub: tuple[str, InMemoryQueue]) -> None:
    base_url, _ = stub
    queue = _client(base_url)
    queue.get_or_create_queue("jobs", max_attempts=2)
    queue.add("jobs", {"n": 0})

    first = queue.claim("jobs", run_id=RUN_ID)
    assert first is not None
    requeued = queue.set_status(first.id, "system_failed", run_id=RUN_ID, error="x")
    assert requeued.status == "new"
    second = queue.claim("jobs", run_id=RUN_ID)
    assert second is not None and second.attempts == 2
    terminal = queue.set_status(second.id, "system_failed", run_id=RUN_ID, error="x")
    assert terminal.status == "system_failed"


def test_stale_write_maps_409_to_invalid_input(stub: tuple[str, InMemoryQueue]) -> None:
    base_url, _ = stub
    queue = _client(base_url)
    queue.get_or_create_queue("jobs")
    queue.add("jobs", {"n": 0})
    claimed = queue.claim("jobs", run_id=RUN_ID)
    assert claimed is not None
    with pytest.raises(InvalidInput):
        queue.set_status(claimed.id, "success", run_id="other-run")


def test_unknown_names_map_404_to_key_error(stub: tuple[str, InMemoryQueue]) -> None:
    base_url, _ = stub
    queue = _client(base_url)
    with pytest.raises(KeyError):
        queue.add("missing", {"n": 0})
    with pytest.raises(KeyError):
        queue.claim("missing", run_id=RUN_ID)
    queue.get_or_create_queue("jobs")
    with pytest.raises(KeyError):
        queue.set_status("missing", "success", run_id=RUN_ID)


def test_connection_failure_maps_to_http_queue_error() -> None:
    queue = HttpQueue("http://127.0.0.1:1", agent_id=AGENT_ID, token="secret", max_retries=0)
    with pytest.raises(HttpQueueError):
        queue.claim("jobs", run_id=RUN_ID)


def test_renew_lease_over_http(stub: tuple[str, InMemoryQueue]) -> None:
    base_url, _ = stub
    queue = _client(base_url)
    queue.get_or_create_queue("jobs")
    queue.add("jobs", {"n": 0})
    claimed = queue.claim("jobs", run_id=RUN_ID, lease_seconds=60)
    assert claimed is not None
    renewed = queue.renew_lease(claimed.id, run_id=RUN_ID, lease_seconds=120)
    assert renewed > claimed.lease_expires_at
    with pytest.raises(InvalidInput):
        queue.renew_lease(claimed.id, run_id="other-run")
    with pytest.raises(KeyError):
        queue.renew_lease("missing", run_id=RUN_ID)


def test_transient_503_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    class _Resp:
        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *args: Any) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"name": "jobs", "max_attempts": 3}'

    def fake_urlopen(request: Any, timeout: Any = None) -> Any:
        attempts.append(1)
        if len(attempts) <= 2:
            raise urllib.error.HTTPError(
                str(request.full_url), 503, "unavailable", {}, io.BytesIO(b"busy")
            )
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    queue = _client("http://stub")
    info = queue.get_or_create_queue("jobs")
    assert (info.name, info.max_attempts) == ("jobs", 3)
    assert len(attempts) == 3
    assert sleeps == [0.5, 1.0]


def test_meaningful_errors_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def fake_urlopen(request: Any, timeout: Any = None) -> Any:
        attempts.append(1)
        raise urllib.error.HTTPError(str(request.full_url), 404, "missing", {}, io.BytesIO(b"gone"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    queue = _client("http://stub")
    with pytest.raises(KeyError):
        queue.set_status("missing", "success", run_id=RUN_ID)
    assert len(attempts) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "", "agent_id": AGENT_ID, "token": "s"},
        {"base_url": "http://x", "agent_id": "", "token": "s"},
        {"base_url": "http://x", "agent_id": AGENT_ID, "token": ""},
        {"base_url": "http://x", "agent_id": AGENT_ID, "token": "s", "timeout_seconds": 0},
        {"base_url": "http://x", "agent_id": AGENT_ID, "token": "s", "timeout_seconds": True},
        {"base_url": "http://x", "agent_id": AGENT_ID, "token": "s", "max_retries": -1},
        {"base_url": "http://x", "agent_id": AGENT_ID, "token": "s", "max_retries": True},
        {"base_url": "ftp://x", "agent_id": AGENT_ID, "token": "s"},
    ],
)
def test_constructor_validation(kwargs: dict[str, Any]) -> None:
    with pytest.raises(InvalidInput):
        HttpQueue(**kwargs)


def test_plain_http_requires_allow_insecure() -> None:
    with pytest.raises(InvalidInput, match="https"):
        HttpQueue("http://example.com/api", agent_id=AGENT_ID, token="s")


def test_plain_http_loopback_allowed() -> None:
    client = HttpQueue("http://127.0.0.1:9000/api", agent_id=AGENT_ID, token="s")
    assert client._base_url == "http://127.0.0.1:9000/api"


def test_plain_http_allowed_with_flag() -> None:
    client = HttpQueue("http://example.com/api", agent_id=AGENT_ID, token="s", allow_insecure=True)
    assert client._base_url == "http://example.com/api"


def test_runner_end_to_end_over_http(stub: tuple[str, InMemoryQueue]) -> None:
    base_url, _ = stub
    queue = _client(base_url)
    queue.get_or_create_queue("jobs", max_attempts=1)
    queue.add("jobs", {"n": 0})
    queue.add("jobs", {"n": 1})

    def process(item: ClaimedItem) -> dict[str, Any] | None:
        return {"doubled": item.payload["n"] * 2}

    report = run_transactions(queue, process, queue_name="jobs", run_id=RUN_ID)
    assert (report.succeeded, report.processed) == (2, 2)
    assert report.stop_reason == "queue_empty"
