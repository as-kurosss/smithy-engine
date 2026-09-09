"""FlowTracer — record facade tool executions as a linear v2 flow document.

The "converter" in the dev→delivery pipeline: run a bot under development
with ``Smithy(trace="bot.flow.json")`` and every successful tool call
becomes a ``tool`` node. Selectors resolved through the keyed store are
traced as ``key`` (portable), not as resolved fields. The document is
re-written after every call, so a crashed dev run still leaves a valid
partial trace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from smithy.core.events import ToolEvent
from smithy.flow import FLOW_VERSION, jsonable

_SELECTOR_FIELDS = ("name", "automation_id", "control_type", "class_name", "pid")


class FlowTracer:
    """Middleware turning the tool-call log into ``flow.json``."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._nodes: list[dict[str, Any]] = []
        self._counter = 0

    async def __call__(self, event: ToolEvent) -> ToolEvent | None:
        """Append *event* as a node; failed calls are not steps."""
        if event.error is not None:
            return event
        config: dict[str, Any] = jsonable(event.config) if isinstance(event.config, dict) else {}
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
        self._write()
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

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.document(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
