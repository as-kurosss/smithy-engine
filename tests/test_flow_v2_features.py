"""FlowRunner v2 extensions: on_error, key selectors, assets, subflows, validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from smithcore.core.assets import EnvAssetProvider
from smithcore.core.errors import BusinessError, ElementNotFound, InfrastructureError
from smithcore.core.registry import ToolRegistry
from smithcore.core.selectors import SelectorStore
from smithcore.core.tool import AbstractTool, tool
from smithcore.flow import FlowError, FlowRunner, validate_document
from smithcore.windows.tools.selector_capture.api import CapturedSelector


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    @tool("test.add", tool_description="add two numbers")
    def add(config: dict) -> int:
        return int(config["a"]) + int(config["b"])

    class TypedAdd(AbstractTool):
        """Addition with a real schema (for validation checks)."""

        @property
        def name(self) -> str:
            return "test.typed_add"

        @property
        def description(self) -> str:
            return "adds two integers"

        def schema(self) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            }

        async def execute(self, config: dict[str, Any]) -> Any:
            return int(config["a"]) + int(config["b"])

    @tool("test.boom", tool_description="always fails")
    def boom(config: dict) -> None:
        raise RuntimeError("boom")

    @tool("test.flaky", tool_description="fails twice, then works")
    def flaky(config: dict) -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("not yet")
        return "ok"

    @tool("test.need_selector", tool_description="fails without a selector")
    def need_selector(config: dict) -> str:
        automation_id = config.get("automation_id")
        if not automation_id or automation_id == "stale":
            raise ElementNotFound("selector does not match the UI")
        return "done"

    for item in (add, TypedAdd(), boom, flaky, need_selector):
        registry.register(item)
    return registry


calls: list[int] = []


def _doc(nodes: list[dict], edges: list[dict]) -> dict:
    return {"version": 2, "nodes": nodes, "edges": edges}


def _chain(*ids: str) -> list[dict]:
    return [
        {"id": f"e{i}", "source": ids[i], "source_handle": "out", "target": ids[i + 1]}
        for i in range(len(ids) - 1)
    ]


# ------------------------------------------------------------------ on_error


class TestOnError:
    async def test_continue_saves_error_and_proceeds(self) -> None:
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "bad",
                    "kind": "tool",
                    "tool": "test.boom",
                    "config": {},
                    "on_error": {"policy": "continue", "save_error_as": "err"},
                },
                {
                    "id": "after",
                    "kind": "set",
                    "config": {"var": "after", "type": "string", "value": "yes"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "after", "e"),
        )
        assert await runner.run(doc) == "finished"
        assert "RuntimeError: boom" in str(runner._variables["err"])
        assert runner._variables["after"] == "yes"

    async def test_retry_succeeds_after_transient_failures(self) -> None:
        calls.clear()
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "f",
                    "kind": "tool",
                    "tool": "test.flaky",
                    "config": {},
                    "save_as": "out",
                    "on_error": {"policy": "retry", "retries": 3, "delay_ms": 0},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "f", "e"),
        )
        assert await runner.run(doc) == "finished"
        assert runner._variables["out"] == "ok"
        assert len(calls) == 3

    async def test_retry_exhausted_raises(self) -> None:
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "bad",
                    "kind": "tool",
                    "tool": "test.boom",
                    "config": {},
                    "on_error": {"policy": "retry", "retries": 1, "delay_ms": 0},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "e"),
        )
        with pytest.raises(FlowError, match="node bad"):
            await runner.run(doc)

    async def test_invalid_policy_rejected(self) -> None:
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "n",
                    "kind": "set",
                    "config": {"var": "a", "value": "1"},
                    "on_error": {"policy": "explode"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "n", "e"),
        )
        with pytest.raises(FlowError, match="policy"):
            await runner.run(doc)


# ------------------------------------------------------------------ assets


class TestAssets:
    async def test_asset_interpolation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ASSET_DB_PASSWORD", "s3cret!")
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "set",
                    "config": {
                        "var": "dsn",
                        "type": "string",
                        "value": "Server=h;Uid=u;Pwd=${asset:db.password}",
                    },
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        await runner.run(doc)
        assert runner._variables["dsn"] == "Server=h;Uid=u;Pwd=s3cret!"

    async def test_asset_secret_redacted_in_log(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMITHCORE_ASSET_TOKEN", "topsecret")
        log: list[tuple[str, str]] = []
        registry = _registry()

        @tool("test.echo", tool_description="echo")
        def echo(config: dict) -> str:
            return "x"

        registry.register(echo)
        runner = FlowRunner(registry, log=lambda lv, m: log.append((lv, m)))
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.echo",
                    "config": {"auth": "${asset:token}"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        await runner.run(doc)
        rendered = " ".join(msg for _, msg in log)
        assert "topsecret" not in rendered
        assert "***" in rendered

    async def test_unknown_asset_fails_node(self) -> None:
        runner = FlowRunner(_registry(), assets=EnvAssetProvider(prefix="SMITHCORE_NOPE_"))
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "set", "config": {"var": "x", "value": "${asset:gone}"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        with pytest.raises(FlowError, match="node t"):
            await runner.run(doc)


# ------------------------------------------------------------------ keys


class TestSelectorKeys:
    async def test_key_resolves_from_store(self, tmp_path: Path) -> None:
        store = SelectorStore(tmp_path / "sel.json")
        store.put("ok", {"automation_id": "btn1"})

        runner = FlowRunner(_registry(), selector_store=store)
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.need_selector",
                    "config": {"key": "ok"},
                    "save_as": "out",
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        await runner.run(doc)
        assert runner._variables["out"] == "done"

    async def test_missing_key_raises_in_production(self, tmp_path: Path) -> None:
        runner = FlowRunner(_registry(), selector_store=SelectorStore(tmp_path / "s.json"))
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.need_selector",
                    "config": {"key": "ghost"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        with pytest.raises(FlowError, match="SMITHCORE_DEV_CAPTURE"):
            await runner.run(doc)

    async def test_missing_key_captured_in_dev_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = SelectorStore(tmp_path / "s.json")
        captured: list[str] = []

        async def fake_capture() -> CapturedSelector:
            captured.append("x")
            return CapturedSelector(selector={"automation_id": "btn1"})

        monkeypatch.setattr(
            "smithcore.windows.tools.selector_capture.capture_once_async", fake_capture
        )
        runner = FlowRunner(_registry(), selector_store=store, dev_capture=True)
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.need_selector",
                    "config": {"key": "ok"},
                    "save_as": "out",
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        await runner.run(doc)
        assert captured == ["x"]
        assert store.get("ok") == {"automation_id": "btn1"}

    async def test_stale_key_recaptured_mid_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = SelectorStore(tmp_path / "s.json")
        store.put("ok", {"automation_id": "stale"})

        async def fake_capture() -> CapturedSelector:
            return CapturedSelector(selector={"automation_id": "fresh"})

        monkeypatch.setattr(
            "smithcore.windows.tools.selector_capture.capture_once_async", fake_capture
        )
        runner = FlowRunner(_registry(), selector_store=store, dev_capture=True)
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.need_selector",
                    "config": {"key": "ok"},
                    "save_as": "out",
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        await runner.run(doc)
        assert store.get("ok") == {"automation_id": "fresh"}


# ------------------------------------------------------------------ subflows


class TestSubflows:
    async def test_global_subflow_reads_globals_but_does_not_leak(self) -> None:
        sub = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.add",
                    "config": {"a": "$G_seed", "b": 1},
                    "save_as": "inner",
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        root = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "sub", "kind": "flow", "config": {"doc": sub}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        root["variables"] = [{"name": "G_seed", "type": "number", "value": "41"}]
        runner = FlowRunner(_registry(), variables={"G_seed": 41})
        assert await runner.run(root) == "finished"
        # The child read the global $G_seed, but its own variable did not leak.
        assert "inner" not in runner._variables
        assert runner._variables["G_seed"] == 41

    async def test_subflow_from_file(self, tmp_path: Path) -> None:
        sub = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "set", "config": {"var": "x", "value": "from-file"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        path = tmp_path / "sub.json"
        path.write_text(json.dumps(sub), encoding="utf-8")
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "sub", "kind": "flow", "config": {"path": str(path)}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        await runner.run(doc)
        # A subflow's local variable does not leak into the parent scope.
        assert "x" not in runner._variables

    async def test_subflow_error_respects_on_error(self) -> None:
        bad_sub = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "bad", "kind": "tool", "tool": "test.boom", "config": {}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "e"),
        )
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "sub",
                    "kind": "flow",
                    "config": {"doc": bad_sub},
                    "on_error": {"policy": "continue", "save_error_as": "sub_err"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        assert await runner.run(doc) == "finished"
        assert "boom" in str(runner._variables["sub_err"])

    async def test_self_recursion_is_capped(self, tmp_path: Path) -> None:
        path = tmp_path / "self.json"
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "sub", "kind": "flow", "config": {"path": str(path)}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        path.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(FlowError, match="nesting"):
            await FlowRunner(_registry()).run(json.loads(path.read_text(encoding="utf-8")))

    async def test_isolated_subflow_inputs_and_outputs(self) -> None:
        sub = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "sum",
                    "kind": "tool",
                    "tool": "test.add",
                    "config": {"a": "$x", "b": 1},
                    "save_as": "y",
                },
                {"id": "leak", "kind": "set", "config": {"var": "internal", "value": "secret"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sum", "leak", "e"),
        )
        runner = FlowRunner(_registry(), variables={"x": 100, "seed": 41})
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "sub",
                    "kind": "flow",
                    "config": {
                        "doc": sub,
                        "inputs": {"x": "$seed"},
                        "outputs": {"result": "y"},
                    },
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        assert await runner.run(doc) == "finished"
        assert runner._variables["result"] == 42
        assert runner._variables["x"] == 100  # parent x is not shadowed by the child
        assert "y" not in runner._variables  # undeclared child variable did not leak
        assert "internal" not in runner._variables

    async def test_isolated_outputs_accept_name_list(self) -> None:
        sub = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "set", "kind": "set", "config": {"var": "made", "value": 7}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "set", "e"),
        )
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "sub",
                    "kind": "flow",
                    "config": {"doc": sub, "outputs": ["made"]},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        assert await runner.run(doc) == "finished"
        assert runner._variables["made"] == 7

    async def test_global_outputs_return_values(self) -> None:
        sub = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "set", "kind": "set", "config": {"var": "made", "value": 7}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "set", "e"),
        )
        runner = FlowRunner(_registry())
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "sub",
                    "kind": "flow",
                    "config": {"doc": sub, "outputs": ["made"]},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        assert await runner.run(doc) == "finished"
        # Explicitly declared output comes back; nothing else does.
        assert runner._variables["made"] == 7

    async def test_globals_reach_nested_subflows_without_inputs(self) -> None:
        inner = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.add",
                    "config": {"a": "$G_shared_counter", "b": 1},
                    "save_as": "result",
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        middle = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "sub", "kind": "flow", "config": {"doc": inner, "outputs": ["result"]}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        root = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "sub", "kind": "flow", "config": {"doc": middle, "outputs": ["result"]}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        root["variables"] = [
            {"name": "G_shared_counter", "type": "number", "value": "41"}
        ]
        runner = FlowRunner(_registry(), variables={"G_shared_counter": 41})
        assert await runner.run(root) == "finished"
        # The deepest subflow read the root global with no inputs passed down.
        assert runner._variables["result"] == 42


# ------------------------------------------------------------------ fail node


class TestFailNode:
    async def test_business_fail_propagates_unwrapped(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "bad",
                    "kind": "fail",
                    "config": {"mode": "business", "message": "amount is $amount"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "e"),
        )
        with pytest.raises(BusinessError, match="amount is 0"):
            await FlowRunner(_registry(), variables={"amount": 0}).run(doc)

    async def test_system_fail_propagates(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "bad", "kind": "fail", "config": {"mode": "system"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "e"),
        )
        with pytest.raises(InfrastructureError):
            await FlowRunner(_registry()).run(doc)

    async def test_bad_mode_is_a_flow_error(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "bad", "kind": "fail", "config": {"mode": "nope"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "e"),
        )
        with pytest.raises(FlowError, match="mode"):
            await FlowRunner(_registry()).run(doc)


# ------------------------------------------------------------------ validation


class TestValidate:
    def test_clean_document(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "tool", "tool": "test.typed_add", "config": {"a": 1, "b": 2}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        assert validate_document(doc, registry=_registry()) == []

    def test_problems(self) -> None:
        doc = _doc(
            [
                {"id": "dup", "kind": "end", "config": {}},
                {"id": "dup", "kind": "start", "config": {}},
                {"id": "bad_tool", "kind": "tool", "tool": "nope", "config": {}},
                {"id": "bad_kind", "kind": "quantum", "config": {}},
                {"id": "bad_flow", "kind": "flow", "config": {}},
            ],
            [
                {"id": "e1", "source": "dup", "source_handle": "out", "target": "ghost"},
                {"id": "e2", "source": "ghost", "source_handle": "out", "target": "t"},
            ],
        )
        problems = validate_document(doc, registry=_registry())
        text = "\n".join(problems)
        assert "duplicate node id 'dup'" in text
        assert "exactly one start node" in text
        assert "'nope' is not registered" in text
        assert "unknown kind 'quantum'" in text
        assert "'path' or 'doc'" in text
        assert "target 'ghost'" in text
        assert "source 'ghost'" in text

    def test_flow_inputs_outputs_are_validated(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "sub",
                    "kind": "flow",
                    "config": {"doc": {}, "inputs": "no", "outputs": "no"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "sub", "e"),
        )
        text = "\n".join(validate_document(doc))
        assert "'inputs' must be an object" in text
        assert "'outputs' must be a list or object" in text

    def test_fail_mode_is_validated(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "bad", "kind": "fail", "config": {"mode": "nope"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "bad", "e"),
        )
        assert "fail mode" in "\n".join(validate_document(doc))

    def test_identifier_names_are_enforced(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "set", "kind": "set", "config": {"var": "$bad", "value": "1"}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.typed_add",
                    "config": {"a": 1, "b": 2},
                    "save_as": "$bad",
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "set", "t", "e"),
        )
        doc["variables"] = [{"name": "$x", "type": "string", "value": "v"}]
        text = "\n".join(validate_document(doc, registry=_registry()))
        assert text.count("plain identifier") >= 3

    def test_ref_type_checked_against_declared_variable(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.typed_add",
                    "config": {"a": "$n", "b": 1},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        doc["variables"] = [{"name": "n", "type": "string", "value": "x"}]
        text = "\n".join(validate_document(doc, registry=_registry()))
        assert "expects integer" in text and "$n is declared string" in text

        doc["variables"] = [{"name": "n", "type": "number", "value": "3"}]
        problems = validate_document(doc, registry=_registry())
        assert not any("expects integer" in problem for problem in problems)

    def test_tool_schema_check(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "t", "kind": "tool", "tool": "test.typed_add", "config": {"a": "x", "b": 2}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        problems = validate_document(doc, registry=_registry())
        assert any("test.typed_add" in p for p in problems)

    def test_key_must_exist_in_store(self, tmp_path: Path) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {
                    "id": "t",
                    "kind": "tool",
                    "tool": "test.need_selector",
                    "config": {"key": "ghost"},
                },
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "t", "e"),
        )
        problems = validate_document(
            doc, registry=_registry(), selector_store=SelectorStore(tmp_path / "s.json")
        )
        assert any("no selector stored for key 'ghost'" in p for p in problems)

    def test_on_error_spec_checked(self) -> None:
        doc = _doc(
            [
                {"id": "s", "kind": "start", "config": {}},
                {"id": "n", "kind": "set", "config": {"var": "x"}, "on_error": {"policy": "wrong"}},
                {"id": "e", "kind": "end", "config": {}},
            ],
            _chain("s", "n", "e"),
        )
        problems = validate_document(doc)
        assert any("policy" in p for p in problems)
