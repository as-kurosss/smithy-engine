"""Tests for the dev→delivery pipeline: tracer, tools loader, transactional mode."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from smithcore import run_flow
from smithcore.core.queue import SqliteQueue
from smithcore.core.selectors import SelectorStore
from smithcore.core.tool import AbstractTool
from smithcore.facade import SmithCore


class EchoTool(AbstractTool):
    @property
    def name(self) -> str:
        return "test.echo"

    @property
    def description(self) -> str:
        return "echoes config"

    def schema(self) -> dict[str, Any]:
        return {"type": "object"}

    async def execute(self, config: dict[str, Any]) -> Any:
        return {"echo": config}


class ClickStub(AbstractTool):
    @property
    def name(self) -> str:
        return "windows.click"

    @property
    def description(self) -> str:
        return "stub click"

    def schema(self) -> dict[str, Any]:
        return {"type": "object"}

    async def execute(self, config: dict[str, Any]) -> Any:
        return {"status": "clicked"}


class FailingTool(AbstractTool):
    @property
    def name(self) -> str:
        return "test.fail"

    @property
    def description(self) -> str:
        return "always fails"

    def schema(self) -> dict[str, Any]:
        return {"type": "object"}

    async def execute(self, config: dict[str, Any]) -> Any:
        raise RuntimeError("boom")


# ------------------------------------------------------------------ tracer


class TestFlowTracer:
    @pytest.mark.asyncio
    async def test_keyed_calls_traced_as_keys(self, tmp_path: Path) -> None:
        SelectorStore(tmp_path / "sel.json").put(
            "editor", {"automation_id": "15", "class_name": "Edit"}
        )
        trace_path = tmp_path / "bot.flow.json"
        bot = SmithCore(
            tools=[ClickStub()],
            selector_store=tmp_path / "sel.json",
            dev_capture=True,
            trace=trace_path,
        )
        await bot.click(key="editor")
        doc = json.loads(trace_path.read_text(encoding="utf-8"))
        kinds = [n["kind"] for n in doc["nodes"]]
        assert kinds == ["start", "tool", "end"]
        tool_node = doc["nodes"][1]
        assert tool_node["tool"] == "windows.click"
        assert tool_node["config"]["key"] == "editor"
        assert "automation_id" not in tool_node["config"]
        assert doc["edges"][0]["source"] == "start"
        assert doc["edges"][-1]["target"] == "end"

    @pytest.mark.asyncio
    async def test_failed_calls_are_not_steps(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "t.flow.json"
        bot = SmithCore(tools=[FailingTool(), EchoTool()], trace=trace_path)
        with pytest.raises(RuntimeError, match="boom"):
            await bot.call("test.fail")
        await bot.call("test.echo", a=1)
        doc = json.loads(trace_path.read_text(encoding="utf-8"))
        assert [n["kind"] for n in doc["nodes"]] == ["start", "tool", "end"]
        assert doc["nodes"][1]["tool"] == "test.echo"

    @pytest.mark.asyncio
    async def test_two_calls_chain_linearly(self, tmp_path: Path) -> None:
        trace_path = tmp_path / "t.flow.json"
        bot = SmithCore(tools=[EchoTool()], trace=trace_path)
        await bot.call("test.echo", step=1)
        await bot.call("test.echo", step=2)
        doc = json.loads(trace_path.read_text(encoding="utf-8"))
        tool_nodes = [n for n in doc["nodes"] if n["kind"] == "tool"]
        assert [n["config"]["step"] for n in tool_nodes] == [1, 2]
        assert len(doc["edges"]) == 3


# ------------------------------------------------------------------ tools loader


class TestToolsLoader:
    def test_loads_tools_list_convention(self, tmp_path: Path) -> None:
        module = tmp_path / "my_tools.py"
        module.write_text(
            "from smithcore.core.tool import AbstractTool\n"
            "from typing import Any\n"
            "class T(AbstractTool):\n"
            "    @property\n"
            "    def name(self) -> str:\n"
            "        return 'my.tool'\n"
            "    @property\n"
            "    def description(self) -> str:\n"
            "        return 'd'\n"
            "    def schema(self) -> dict:\n"
            "        return {'type': 'object'}\n"
            "    async def execute(self, config: dict) -> Any:\n"
            "        return {'ok': True}\n"
            "TOOLS = [T()]\n",
            encoding="utf-8",
        )
        from smithcore.run_flow import _load_tools

        tools = _load_tools(str(module))
        assert [t.name for t in tools] == ["my.tool"]

    def test_scans_module_instances_without_tools_list(self, tmp_path: Path) -> None:
        module = tmp_path / "bare_tools.py"
        module.write_text(
            "from smithcore.core.tool import AbstractTool\n"
            "from typing import Any\n"
            "class T(AbstractTool):\n"
            "    @property\n"
            "    def name(self) -> str:\n"
            "        return 'bare.tool'\n"
            "    @property\n"
            "    def description(self) -> str:\n"
            "        return 'd'\n"
            "    def schema(self) -> dict:\n"
            "        return {'type': 'object'}\n"
            "    async def execute(self, config: dict) -> Any:\n"
            "        return {}\n"
            "t = T()\n",
            encoding="utf-8",
        )
        from smithcore.run_flow import _load_tools

        tools = _load_tools(str(module))
        assert [t.name for t in tools] == ["bare.tool"]


# ------------------------------------------------------------------ vars/payload


class TestVarsMerge:
    def test_payload_then_vars_then_set_wins(self, tmp_path: Path) -> None:
        flow = tmp_path / "f.json"
        payload = tmp_path / "payload.json"
        vars_file = tmp_path / "vars.json"
        flow.write_text(json.dumps({"version": 2, "nodes": [], "edges": []}), encoding="utf-8")
        payload.write_text(json.dumps({"a": 1, "b": 2, "c": 3}), encoding="utf-8")
        vars_file.write_text(json.dumps({"b": 20, "c": 30}), encoding="utf-8")

        from smithcore import run_flow as run_flow_module

        captured: dict[str, Any] = {}

        class FakeRunner:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured["variables"] = kwargs["variables"]

            async def run(self, doc: dict[str, Any]) -> str:
                return "finished"

        original = run_flow_module.FlowRunner
        run_flow_module.FlowRunner = FakeRunner  # type: ignore[misc]
        try:
            rc = run_flow.main(
                [str(flow), "--payload", str(payload), "--vars", str(vars_file), "--set", "c=99"]
            )
        finally:
            run_flow_module.FlowRunner = original  # type: ignore[misc]
        assert rc == 0
        assert captured["variables"] == {"a": 1, "b": 20, "c": 99}

    def test_bad_vars_file_fails(self, tmp_path: Path) -> None:
        flow = tmp_path / "f.json"
        flow.write_text(json.dumps({"version": 2, "nodes": [], "edges": []}), encoding="utf-8")
        bad = tmp_path / "v.json"
        bad.write_text("{nope", encoding="utf-8")
        assert run_flow.main([str(flow), "--vars", str(bad)]) == 1


# ------------------------------------------------------------------ transactional


class TestTransactional:
    def test_local_sqlite_queue_processes_items(self, tmp_path: Path) -> None:
        flow = tmp_path / "f.json"
        doc = {
            "version": 2,
            "nodes": [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "set",
                    "config": {"var": "doubled", "type": "number", "value": "$n"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            "edges": [
                {"id": "e1", "source": "s", "source_handle": "out", "target": "t"},
                {"id": "e2", "source": "t", "source_handle": "out", "target": "e"},
            ],
        }
        flow.write_text(json.dumps(doc), encoding="utf-8")
        db = tmp_path / "q.db"
        queue = SqliteQueue(db)
        queue.get_or_create_queue("jobs", max_attempts=1)
        queue.add("jobs", {"n": 2})
        queue.add("jobs", {"n": 5})
        queue.close()

        rc = run_flow.main([str(flow), "--transactional", "--queue", "jobs", "--db", str(db)])
        assert rc == 0

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT payload, result, status FROM items WHERE queue = 'jobs'"
        ).fetchall()
        conn.close()
        statuses = {str(status) for _, _, status in rows}
        assert statuses == {"success"}
        results = {
            json.loads(str(payload))["n"]: json.loads(str(result)) for payload, result, _ in rows
        }
        assert results == {
            2: {"n": 2, "doubled": 2},
            5: {"n": 5, "doubled": 5},
        }

    def test_transactional_requires_queue_backend(self, tmp_path: Path) -> None:
        flow = tmp_path / "f.json"
        flow.write_text(json.dumps({"version": 2, "nodes": [], "edges": []}), encoding="utf-8")
        assert run_flow.main([str(flow), "--transactional", "--queue", "jobs"]) == 1

    def test_transactional_requires_queue_name(self, tmp_path: Path) -> None:
        flow = tmp_path / "f.json"
        flow.write_text(json.dumps({"version": 2, "nodes": [], "edges": []}), encoding="utf-8")
        assert run_flow.main([str(flow), "--transactional"]) == 1
