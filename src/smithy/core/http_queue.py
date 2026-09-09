"""HTTP queue backend speaking the smithy-cloud transaction contract.

Transport is stdlib ``urllib`` only — no third-party dependencies. All HTTP
mechanics live in the private :func:`_post` / :func:`_patch` helpers so a
future transport (keep-alive, async) can replace them without touching the
:class:`Queue` implementation.

Reliability: every request has a timeout and is retried (exponential
backoff) on connection-level failures and HTTP 502/503/504. Meaningful
answers — 400/404/409/422 — are never retried.

Note on auth: ``claim``/``set_status`` use the agent Bearer token, while
``get_or_create_queue``/``add`` require operator rights. Seed queues with an
operator token (or pre-create them server-side); workers only claim.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, cast

from smithy.core.errors import InfrastructureError, InvalidInput
from smithy.core.queue import (
    TERMINAL_STATUSES,
    ClaimedItem,
    FinalStatus,
    ItemStatus,
    QueueInfo,
    QueueItem,
)


class HttpQueueError(InfrastructureError):
    """Transport or contract failure talking to the queue server."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_RETRYABLE_STATUS: frozenset[int] = frozenset({502, 503, 504})
_RETRY_BASE_SECONDS = 0.5


def _engine_version() -> str | None:
    """Installed smithy-engine version (None when not resolvable)."""
    try:
        return _pkg_version("smithy-engine")
    except PackageNotFoundError:
        return None


def _post(
    url: str, payload: dict[str, Any], *, token: str, timeout: float, max_retries: int
) -> Any:
    """POST *payload* as JSON, return the decoded JSON body."""
    return _request("POST", url, payload, token=token, timeout=timeout, max_retries=max_retries)


def _patch(
    url: str, payload: dict[str, Any], *, token: str, timeout: float, max_retries: int
) -> Any:
    """PATCH *payload* as JSON, return the decoded JSON body."""
    return _request("PATCH", url, payload, token=token, timeout=timeout, max_retries=max_retries)


def _request(
    method: str,
    url: str,
    payload: dict[str, Any],
    *,
    token: str,
    timeout: float,
    max_retries: int,
) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:
                if exc.code in _RETRYABLE_STATUS and attempt < max_retries:
                    time.sleep(_RETRY_BASE_SECONDS * 2**attempt)
                    attempt += 1
                    continue
                detail = exc.read().decode("utf-8", errors="replace")
                raise HttpQueueError(
                    f"{method} {url} failed with HTTP {exc.code}: {detail}",
                    status_code=exc.code,
                ) from exc
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < max_retries:
                time.sleep(_RETRY_BASE_SECONDS * 2**attempt)
                attempt += 1
                continue
            raise HttpQueueError(f"{method} {url} failed: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HttpQueueError(f"{method} {url} returned invalid JSON: {exc}") from exc


class HttpQueue:
    """Queue backend over the smithy-cloud HTTP contract.

    *base_url* is the API root including the version/prefix path,
    e.g. ``"https://host/api"`` for a default smithy-cloud deployment.

    Non-HTTPS *base_url* is rejected unless ``allow_insecure=True`` — the
    Bearer token would otherwise travel in cleartext. Plain-HTTP loopback
    addresses (``localhost``, ``127.0.0.1``, ``::1``) are always accepted
    so local development and test servers work out of the box.
    """

    def __init__(
        self,
        base_url: str,
        *,
        agent_id: str,
        token: str,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        allow_insecure: bool = False,
    ) -> None:
        if not base_url or not isinstance(base_url, str):
            raise InvalidInput("base_url must be a non-empty string", param="base_url")
        if not agent_id or not isinstance(agent_id, str):
            raise InvalidInput("agent_id must be a non-empty string", param="agent_id")
        if not token or not isinstance(token, str):
            raise InvalidInput("token must be a non-empty string", param="token")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise InvalidInput("timeout_seconds must be a positive number", param="timeout_seconds")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise InvalidInput(
                "max_retries must be an int >= 0", param="max_retries", input_value=max_retries
            )
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            raise InvalidInput(
                "base_url must be an http(s) URL",
                param="base_url",
                input_value=base_url,
            )
        if parsed.scheme != "https" and not allow_insecure:
            host = (parsed.hostname or "").lower()
            loopback = host in ("localhost", "127.0.0.1", "::1")
            if not loopback:
                raise InvalidInput(
                    "base_url must use https:// (pass allow_insecure=True to override)",
                    param="base_url",
                    input_value=base_url,
                )
        self._base_url = base_url.rstrip("/")
        self._agent_id = agent_id
        self._token = token
        self._timeout = float(timeout_seconds)
        self._max_retries = max_retries

    def get_or_create_queue(self, name: str, *, max_attempts: int = 3) -> QueueInfo:
        data = _post(
            f"{self._base_url}/queues",
            {"name": name, "max_attempts": max_attempts},
            token=self._token,
            timeout=self._timeout,
            max_retries=self._max_retries,
        )
        if not isinstance(data, dict):
            raise HttpQueueError(f"Expected a queue object, got {data!r}")
        return QueueInfo(name=str(data["name"]), max_attempts=int(data["max_attempts"]))

    def add(
        self,
        queue: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> QueueItem:
        quoted = urllib.parse.quote(queue, safe="")
        try:
            data = _post(
                f"{self._base_url}/queues/{quoted}/items",
                {"items": [{"payload": payload, "idempotency_key": idempotency_key}]},
                token=self._token,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        except HttpQueueError as exc:
            raise _maybe_missing(exc, f"Unknown queue: {queue!r}") from exc
        if not isinstance(data, list) or not data:
            raise HttpQueueError(f"Expected a non-empty item list, got {data!r}")
        return _parse_item(queue, data[0])

    def claim(self, queue: str, *, run_id: str, lease_seconds: int = 300) -> ClaimedItem | None:
        quoted = urllib.parse.quote(queue, safe="")
        body: dict[str, Any] = {"run_id": run_id, "lease_seconds": lease_seconds}
        # Version stamping: the cloud records which agent/engine actually
        # claims work (monitoring now, version routing later). Both fields
        # are optional and ignored by servers without the feature.
        engine = _engine_version()
        if engine is not None:
            body["engine_version"] = engine
        agent_version = os.environ.get("SMITHY_AGENT_VERSION")
        if agent_version:
            body["agent_version"] = agent_version
        try:
            data = _post(
                f"{self._base_url}/agents/{self._agent_id}/queues/{quoted}/claim",
                body,
                token=self._token,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        except HttpQueueError as exc:
            raise _maybe_missing(exc, f"Unknown queue: {queue!r}") from exc
        if not isinstance(data, dict):
            raise HttpQueueError(f"Expected a claim object, got {data!r}")
        item = data.get("item")
        if item is None:
            return None
        return ClaimedItem(
            id=str(item["id"]),
            queue=queue,
            payload=dict(item["payload"]),
            attempts=int(item["attempts"]),
            lease_expires_at=_parse_time(item["lease_expires_at"]),
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
        try:
            data = _patch(
                f"{self._base_url}/agents/{self._agent_id}/queue-items/{item_id}",
                {"status": status, "run_id": run_id, "error": error, "result": result},
                token=self._token,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        except HttpQueueError as exc:
            if exc.status_code == 409:
                raise InvalidInput(
                    f"Queue item {item_id!r} is not claimed by this run",
                    param="run_id",
                    input_value=run_id,
                ) from exc
            raise _maybe_missing(exc, f"Unknown queue item: {item_id!r}") from exc
        if not isinstance(data, dict):
            raise HttpQueueError(f"Expected a queue-item object, got {data!r}")
        return _parse_item(str(data.get("queue", "")), data)

    def renew_lease(self, item_id: str, *, run_id: str, lease_seconds: int = 300) -> datetime:
        """Extend the claim lease; the claim must belong to *run_id*.

        Requires the server heartbeat endpoint
        (``PATCH /agents/{agent}/queue-items/{id}/heartbeat``).
        """
        try:
            data = _patch(
                f"{self._base_url}/agents/{self._agent_id}/queue-items/{item_id}/heartbeat",
                {"run_id": run_id, "lease_seconds": lease_seconds},
                token=self._token,
                timeout=self._timeout,
                max_retries=self._max_retries,
            )
        except HttpQueueError as exc:
            if exc.status_code == 409:
                raise InvalidInput(
                    f"Queue item {item_id!r} is not claimed by this run",
                    param="run_id",
                    input_value=run_id,
                ) from exc
            raise _maybe_missing(exc, f"Unknown queue item: {item_id!r}") from exc
        if not isinstance(data, dict) or "lease_expires_at" not in data:
            raise HttpQueueError(f"Expected a heartbeat object, got {data!r}")
        return _parse_time(data["lease_expires_at"])


def _maybe_missing(exc: HttpQueueError, message: str) -> HttpQueueError | KeyError:
    """Map HTTP 404 to ``KeyError`` so backends stay substitutable."""
    if exc.status_code == 404:
        return KeyError(message)
    return exc


def _parse_item(queue: str, data: Any) -> QueueItem:
    if not isinstance(data, dict):
        raise HttpQueueError(f"Expected a queue-item object, got {data!r}")
    status = str(data.get("status", ""))
    valid = ("new", "in_progress", *TERMINAL_STATUSES)
    if status not in valid:
        raise HttpQueueError(f"Unknown queue-item status: {status!r}")
    payload = data.get("payload", {})
    if not isinstance(payload, dict):
        raise HttpQueueError(f"Queue-item payload must be an object, got {payload!r}")
    return QueueItem(
        id=str(data["id"]),
        queue=queue,
        payload=payload,
        status=cast("ItemStatus", status),
        attempts=int(data.get("attempts", 0)),
    )


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise HttpQueueError(f"Expected an ISO timestamp string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HttpQueueError(f"Invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
