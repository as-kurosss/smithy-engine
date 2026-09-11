"""FlowRunner: standalone flow-v2 execution (interpolation, nodes, loops)."""

from __future__ import annotations

import pytest

from smithcore.core.registry import ToolRegistry
from smithcore.core.tool import tool
from smithcore.flow import FlowError, FlowRunner


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @tool("test.add", tool_description="add two numbers")
    def add(config: dict) -> int:
        return int(config["a"]) + int(config["b"])

    @tool("test.boom", tool_description="always fails")
    def boom(config: dict) -> None:
        raise RuntimeError("boom")

    registry.register(add)
    registry.register(boom)
    return registry


def _doc(nodes: list[dict], edges: list[dict]) -> dict:
    return {"version": 2, "nodes": nodes, "edges": edges}


async def test_set_tool_if_end() -> None:
    log: list[tuple[str, str]] = []
    runner = FlowRunner(_registry(), log=lambda lv, m: log.append((lv, m)))
    doc = _doc(
        [
            {"id": "s", "kind": "start", "config": {}},
            {"id": "n", "kind": "set", "config": {"var": "a", "type": "number", "value": "2"}},
            {
                "id": "t",
                "kind": "tool",
                "tool": "test.add",
                "config": {"a": "$a", "b": 3},
                "save_as": "sum",
            },
            {
                "id": "c",
                "kind": "if",
                "config": {},
                "condition": {"var": "sum", "op": "gt", "value": 4},
            },
            {"id": "e", "kind": "end", "config": {}},
        ],
        [
            {"id": "e1", "source": "s", "source_handle": "out", "target": "n"},
            {"id": "e2", "source": "n", "source_handle": "out", "target": "t"},
            {"id": "e3", "source": "t", "source_handle": "out", "target": "c"},
            {"id": "e4", "source": "c", "source_handle": "true", "target": "e"},
        ],
    )
    assert await runner.run(doc) == "finished"
    assert runner._variables["sum"] == 5  # true-branch taken, so the end was reached


async def test_loop_foreach_interpolates_and_finishes() -> None:
    runner = FlowRunner(_registry())
    doc = _doc(
        [
            {"id": "s", "kind": "start", "config": {}},
            {
                "id": "seed",
                "kind": "set",
                "config": {"var": "items", "type": "json", "value": "[1, 2, 3]"},
            },
            {
                "id": "lp",
                "kind": "loop",
                "config": {},
                "loop": {"mode": "foreach", "var": "items", "as": "it"},
            },
            {"id": "t", "kind": "tool", "tool": "test.add", "config": {"a": "$it", "b": 0}},
            {"id": "e", "kind": "end", "config": {}},
        ],
        [
            {"id": "e1", "source": "s", "source_handle": "out", "target": "seed"},
            {"id": "e2", "source": "seed", "source_handle": "out", "target": "lp"},
            {"id": "e3", "source": "lp", "source_handle": "body", "target": "t"},
            {"id": "e4", "source": "t", "source_handle": "out", "target": "lp"},
            {"id": "e5", "source": "lp", "source_handle": "done", "target": "e"},
        ],
    )
    assert await runner.run(doc) == "finished"


async def test_tool_failure_raises_flow_error_with_node() -> None:
    runner = FlowRunner(_registry())
    doc = _doc(
        [
            {"id": "s", "kind": "start", "config": {}},
            {"id": "bad", "kind": "tool", "tool": "test.boom", "config": {}},
        ],
        [{"id": "e1", "source": "s", "source_handle": "out", "target": "bad"}],
    )
    with pytest.raises(FlowError, match="node bad"):
        await runner.run(doc)


async def test_shared_variables_dict_is_mutated() -> None:
    variables: dict = {"seed": 10}
    runner = FlowRunner(_registry(), variables=variables)
    doc = _doc(
        [
            {"id": "s", "kind": "start", "config": {}},
            {
                "id": "t",
                "kind": "tool",
                "tool": "test.add",
                "config": {"a": "$seed", "b": 5},
                "save_as": "out",
            },
        ],
        [{"id": "e1", "source": "s", "source_handle": "out", "target": "t"}],
    )
    await runner.run(doc)
    assert variables["out"] == 15  # same dict object — debugger/REPL share it


async def test_unsupported_version_and_missing_start() -> None:
    runner = FlowRunner(_registry())
    with pytest.raises(FlowError, match="version"):
        await runner.run({"version": 1, "nodes": [], "edges": []})
    with pytest.raises(FlowError, match="no start node"):
        await runner.run({"version": 2, "nodes": [], "edges": []})
