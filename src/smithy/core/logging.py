"""JSONL event sink — append-only audit log of tool executions.

Attach to a bot in one line::

    bot.add_middleware(JsonlEventLogger("bot-data/runs.jsonl"))

Every :class:`ToolEvent` becomes one JSON object per line: timestamp,
tool name, current ``transaction_id`` (when inside a transaction run),
duration, error (if any), plus the config and result. JSONL is
grep-able with plain tools and importable into log analysers —
the standard answer to "разбор полётов после ошибок".
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from smithy.core.events import ToolEvent
from smithy.core.redact import redact_value
from smithy.core.transactions import current_transaction_id

logger = logging.getLogger(__name__)

_DEFAULT_MAX_QUEUE = 10_000


class JsonlEventLogger:
    """Middleware that appends each :class:`ToolEvent` as one JSON line.

    The file is opened in append mode at construction, so a bot with an
    unwritable audit log fails fast in Init instead of silently losing
    history. Writes happen on a background thread — the event loop is
    never blocked by disk I/O. Call :meth:`close` (or use the logger as
    a context manager) to drain the queue, flush and release the handle.

    Args:
        path: JSONL file to append to. Missing parent directories are
            *not* created — a bad path raises ``OSError`` immediately.
        include_config: Record the tool input (disable for sensitive data).
        include_result: Record the tool output.
        redact: Secret values to scrub from config, result and error
            messages (defense in depth on top of the facade's own
            redaction).
        max_queue: Bound on the in-memory write queue. When the writer
            cannot keep up, new records are dropped (and counted in
            :attr:`dropped`) instead of growing memory without limit.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        include_config: bool = True,
        include_result: bool = True,
        redact: Iterable[str] = (),
        max_queue: int = _DEFAULT_MAX_QUEUE,
    ) -> None:
        self._path = Path(path)
        self._include_config = include_config
        self._include_result = include_result
        self._secrets = tuple(s for s in redact if s)
        self._file: TextIO = self._path.open("a", encoding="utf-8")
        self._lines: queue.Queue[str | None] = queue.Queue(maxsize=max(1, max_queue))
        self._dropped = 0
        self._writer = threading.Thread(
            target=self._write_loop, name="smithy-jsonl-logger", daemon=True
        )
        self._writer.start()

    @property
    def dropped(self) -> int:
        """Number of records dropped because the write queue was full."""
        return self._dropped

    def _write_loop(self) -> None:
        while True:
            line = self._lines.get()
            if line is None:
                return
            try:
                self._file.write(line)
                self._file.flush()
            except Exception:
                logger.exception("audit log write to %s failed", self._path)

    async def __call__(self, event: ToolEvent) -> ToolEvent | None:
        """Enqueue *event* for writing and pass it down the pipeline."""
        error = event.error
        record: dict[str, Any] = {
            "ts": event.timestamp.isoformat(),
            "tool": event.tool_name,
            "transaction_id": current_transaction_id.get(),
            "duration_ms": round(event.duration_ms, 3),
            "error": (
                None
                if error is None
                else {
                    "type": type(error).__name__,
                    "message": str(redact_value(str(error), self._secrets)),
                }
            ),
        }
        if self._include_config:
            record["config"] = redact_value(event.config, self._secrets)
        if self._include_result:
            record["result"] = redact_value(event.result, self._secrets)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        try:
            self._lines.put_nowait(line)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1:
                logger.warning(
                    "audit log %s is falling behind (%d records dropped)",
                    self._path,
                    self._dropped,
                )
        return event

    def close(self) -> None:
        """Drain pending writes, flush and close the underlying file."""
        self._lines.put(None)
        self._writer.join(timeout=5.0)
        self._file.close()

    def __enter__(self) -> JsonlEventLogger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
