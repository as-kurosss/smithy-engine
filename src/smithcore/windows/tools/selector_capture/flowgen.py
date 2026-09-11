"""Convert recorded action nodes into a flow-v2 document.

Recorded nodes are flat ``{tool, args}`` pairs (see
:func:`smithcore.windows.tools.selector_capture.record.record_series`). This
module chains them into a runnable v2 flow — ``start → step N → end`` —
so a recording can be opened on the designer canvas and replayed by the
engine with no code in between.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from smithcore.flow import FLOW_VERSION
from smithcore.windows.tools.selector_capture.generate import FlowNode

_LANE_X = 80
_Y0 = 80
_STEP_DY = 130
_LABEL_LIMIT = 60


def _label_for(node: FlowNode) -> str | None:
    """A short human label for a node, taken from its selector/config."""
    for key in ("name", "automation_id", "text"):
        value = node.args.get(key)
        if isinstance(value, str) and value:
            return value[:_LABEL_LIMIT]
    return None


def nodes_to_flow(nodes: Sequence[FlowNode], *, name: str | None = None) -> dict[str, Any]:
    """Chain recorded *nodes* into a flow-v2 document.

    Raises:
        ValueError: If any recorded node carries no tool name.
    """
    doc_nodes: list[dict[str, Any]] = [
        {"id": "start", "kind": "start", "config": {}, "position": [_LANE_X, _Y0]}
    ]
    edges: list[dict[str, Any]] = []

    previous = "start"
    for index, node in enumerate(nodes):
        if not node.tool:
            raise ValueError("recorded node has no tool name")
        node_id = f"step{index + 1}"
        item: dict[str, Any] = {
            "id": node_id,
            "kind": "tool",
            "tool": node.tool,
            "config": dict(node.args),
            "position": [_LANE_X, _Y0 + (index + 1) * _STEP_DY],
        }
        label = _label_for(node)
        if label:
            item["label"] = label
        doc_nodes.append(item)
        edges.append(
            {"id": f"e{index + 1}", "source": previous, "source_handle": "out", "target": node_id}
        )
        previous = node_id

    end_y = _Y0 + (len(nodes) + 1) * _STEP_DY
    doc_nodes.append({"id": "end", "kind": "end", "config": {}, "position": [_LANE_X, end_y]})
    edges.append(
        {
            "id": f"e{len(nodes) + 1}",
            "source": previous,
            "source_handle": "out",
            "target": "end",
        }
    )

    doc: dict[str, Any] = {"version": FLOW_VERSION, "nodes": doc_nodes, "edges": edges}
    if name:
        doc["name"] = name
    return doc
