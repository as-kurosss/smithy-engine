"""FlowTracer — record facade tool executions as a linear v2 flow document.

The "converter" in the dev→delivery pipeline: run a bot under development
with ``SmithCore(trace="bot.flow.json")`` and every successful tool call
becomes a ``tool`` node. Selectors resolved through the keyed store are
traced as ``key`` (portable), not as resolved fields. The document is
re-written periodically, so a crashed dev run still leaves a valid
partial trace without paying an O(N²) rewrite per call.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from smithcore.core.events import ToolEvent
from smithcore.flow import FLOW_VERSION, jsonable

logger = logging.getLogger(__name__)

_SELECTOR_FIELDS = ("name", "automation_id", "control_type", "class_name")
#: Dev-run PIDs are never portable, so they are dropped from the trace
#: (a traced ``pid`` would point at a process that no longer exists).
_VOLATILE_FIELDS = ("pid", "from_pid", "to_pid")

_DEFAULT_MAX_NODES = 20_000
_DEFAULT_WRITE_INTERVAL = 0.25
#: Always rewrite for the first few calls (small traces stay exact), then
#: throttle whole-document rewrites to bound I/O on long dev runs.
_ALWAYS_WRITE_LIMIT = 50


class FlowTracer:
    """Middleware turning the tool-call log into ``flow.json``.

    Args:
        path: Destination flow document.
        max_nodes: Stop recording after this many tool calls (bounds
            memory for a very long dev run).
        write_interval: Minimum seconds between whole-document rewrites
            (bounds disk I/O; the trace stays valid but may lag the last
            few calls on a crash).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_nodes: int = _DEFAULT_MAX_NODES,
        write_interval: float = _DEFAULT_WRITE_INTERVAL,
    ) -> None:
        self._path = Path(path)
        self._nodes: list[dict[str, Any]] = []
        self._counter = 0
        self._max_nodes = max_nodes
        self._write_interval = write_interval
        self._last_write = 0.0
        self._truncated = False

    async def __call__(self, event: ToolEvent) -> ToolEvent | None:
        """Append *event* as a node; failed calls are not steps."""
        if event.error is not None:
            return event
        if len(self._nodes) >= self._max_nodes:
            if not self._truncated:
                self._truncated = True
                logger.warning(
                    "flow trace %s reached max_nodes=%d; further calls are not recorded",
                    self._path,
                    self._max_nodes,
                )
            return event
        config: dict[str, Any] = jsonable(event.config) if isinstance(event.config, dict) else {}
        for field_name in _VOLATILE_FIELDS:
            config.pop(field_name, None)
        key = event.metadata.get("selector_key")
        if key is not None:
            for field_name in _SELECTOR_FIELDS:
                config.pop(field_name, None)
            config["key"] = key
        self._counter += 1
        self._nodes.append(
            {
                "id": f"n{self._counter}",
                "kind": "tool",
                "tool": event.tool_name,
                "config": config,
            }
        )
        now = time.monotonic()
        if self._counter <= _ALWAYS_WRITE_LIMIT or now - self._last_write >= self._write_interval:
            # Off the event loop: whole-document rewrites are O(N) JSON+IO.
            import asyncio

            await asyncio.to_thread(self._write)
        return event

    def nodes(self) -> list[dict[str, Any]]:
        """Traced tool nodes so far (without start/end)."""
        return list(self._nodes)

    def document(self) -> dict[str, Any]:
        """The current flow document (start → nodes → end, chained)."""
        ids = ["start", *(node["id"] for node in self._nodes), "end"]
        return {
            "version": FLOW_VERSION,
            "nodes": [
                {"id": "start", "kind": "start", "config": {}},
                *self._nodes,
                {"id": "end", "kind": "end", "config": {}},
            ],
            "edges": [
                {
                    "id": f"e{i + 1}",
                    "source": ids[i],
                    "source_handle": "out",
                    "target": ids[i + 1],
                }
                for i in range(len(ids) - 1)
            ],
        }

    def flush(self) -> None:
        """Force a rewrite of the trace file (e.g. at a safe checkpoint)."""
        self._write()

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(self.document(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("failed to write flow trace %s", self._path)
            return
        self._last_write = time.monotonic()
