"""Flow-v2 execution core: interpolation, conditions, typed values, graph runner.

This is the engine-side executor for flow documents (the same format the
designer edits and the debugger steps through). It is deliberately
interactive-free: no gates, no breakpoints, no REPL — those live in the
designer's debugger on top of :class:`FlowRunner`. Used standalone:

    from smithy.flow import FlowRunner
    from smithy.core.registry import ToolRegistry

    runner = FlowRunner(ToolRegistry(), log=print)
    await runner.run(doc)   # raises FlowError on failure
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from smithy.core.registry import ToolRegistry

FLOW_VERSION = 2

_VAR_RE = re.compile(r"\$(\w+)((?:\.\w+|\[[^\[\]]+\])*)")
_REF_PART_RE = re.compile(r"\.(\w+)|\[([^\[\]]+)\]")

_REPR_LIMIT = 2000


class FlowError(Exception):
    """Raised for flow misuse, unsupported constructs or node failures."""


def short(text: str, limit: int = _REPR_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def jsonable(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _resolve_ref(name: str, path: str, variables: dict[str, Any]) -> Any:
    """Resolve ``name.attr[0].key`` against the variable scope.

    Dots read attributes (or dict keys), brackets index lists/strings by
    position or dicts by quoted/unquoted key. Underscore-prefixed
    attributes are blocked, mirroring the REPL sandbox.
    """
    value: Any = variables[name]
    if not path:
        return value
    for attr, bracket in _REF_PART_RE.findall(path):
        try:
            if attr:
                if isinstance(value, dict):
                    value = value[attr]
                else:
                    if attr.startswith("_"):
                        raise FlowError(f"attribute {attr!r} is not allowed")
                    value = getattr(value, attr)
            else:
                key: Any = bracket
                if len(bracket) >= 2 and bracket[0] == bracket[-1] and bracket[0] in "'\"":
                    key = bracket[1:-1]
                elif isinstance(value, list) and bracket.lstrip("-").isdigit():
                    key = int(bracket)
                value = value[key]
        except FlowError:
            raise
        except Exception as exc:
            raise FlowError(
                f"cannot resolve ${name}{path}: {type(exc).__name__}: {exc}"
            ) from exc
    return value


def lookup(variables: dict[str, Any], path: str) -> Any:
    """Look up ``name`` or ``name.attr[0]`` in the scope; ``None`` when absent."""
    match = re.match(r"\w+", path)
    if match is None or match.group(0) not in variables:
        return None
    try:
        return _resolve_ref(match.group(0), path[match.end() :], variables)
    except FlowError:
        return None


def interpolate(value: Any, variables: dict[str, Any]) -> Any:
    """Substitute ``$name``/``$name.pid`` in config values from the scope.

    A string that is a single reference keeps the referenced value's type;
    mixed text interpolates the referenced value as text. Unknown variable
    names are left as-is.
    """
    if isinstance(value, str):
        exact = _VAR_RE.fullmatch(value)
        if exact and exact.group(1) in variables:
            return _resolve_ref(exact.group(1), exact.group(2), variables)

        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                return match.group(0)
            try:
                return str(_resolve_ref(name, match.group(2), variables))
            except FlowError:
                return match.group(0)

        return _VAR_RE.sub(sub, value)
    if isinstance(value, list):
        return [interpolate(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: interpolate(item, variables) for key, item in value.items()}
    return value


def evaluate_condition(condition: dict[str, Any], variables: dict[str, Any]) -> bool:
    var = str(condition.get("var") or "")
    op = str(condition.get("op") or "exists")
    left: Any = lookup(variables, var)
    right: Any = interpolate(condition.get("value"), variables)
    if op == "exists":
        return left is not None
    if op == "is_empty":
        return left is None or left == "" or left == [] or left == {}
    try:
        if op == "eq":
            return bool(left == right)
        if op == "ne":
            return bool(left != right)
        if op == "contains":
            return bool(right in left)
        if op == "not_contains":
            return bool(right not in left)
        if op == "gt":
            return bool(left > right)
        if op == "lt":
            return bool(left < right)
    except TypeError as exc:
        raise FlowError(f"cannot compare {left!r} {op} {right!r}: {exc}") from exc
    raise FlowError(f"unknown operator {op!r}")


def parse_typed_value(value: Any, vtype: str) -> Any:
    """Interpret a ``set`` node value according to its declared type."""
    text = "" if value is None else str(value)
    if vtype == "string":
        return text
    if vtype == "number":
        try:
            return int(text)
        except ValueError:
            return float(text)  # ValueError propagates as a node failure
    if vtype == "bool":
        return text.strip().lower() in ("true", "1", "yes", "on")
    if vtype == "json":
        return json.loads(text)  # JSONDecodeError propagates as a node failure
    # auto: JSON literals when parseable, raw string otherwise
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


def parse_set_value(raw: Any, vtype: str, variables: dict[str, Any]) -> Any:
    """Interpret a ``set`` node value with ``$ref`` support.

    A value that is a single reference (``$app``, ``$app.pid``) takes the
    referenced value as-is, keeping its type. Anything else is interpolated
    as text first, then parsed according to *vtype*.
    """
    if isinstance(raw, str):
        exact = _VAR_RE.fullmatch(raw)
        if exact and exact.group(1) in variables:
            return _resolve_ref(exact.group(1), exact.group(2), variables)
        return parse_typed_value(interpolate(raw, variables), vtype)
    return parse_typed_value(raw, vtype)


LogFn = Any  # Callable[[str, str], None]


def _noop_log(level: str, msg: str) -> None:
    pass


class FlowRunner:
    """Executes a flow graph node by node against a tool registry.

    The runner keeps loop state and the variable scope (a plain dict it
    mutates — pass the same dict to share scope with a debugger/REPL).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        variables: dict[str, Any] | None = None,
        edges: list[dict[str, Any]] | None = None,
        log: LogFn | None = None,
    ) -> None:
        self._registry = registry
        self._variables: dict[str, Any] = variables if variables is not None else {}
        self._edges: list[dict[str, Any]] = edges or []
        self._log = log or _noop_log
        self._loops: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ navigation

    def next_by_handle(self, node_id: str, handle: str) -> str | None:
        for edge in self._edges:
            if edge.get("source") == node_id and edge.get("source_handle") == handle:
                return str(edge.get("target"))
        return None

    # ------------------------------------------------------------ node steps

    async def _run_tool(self, node: dict[str, Any]) -> None:
        name = str(node.get("tool") or "")
        if not name:
            raise FlowError("tool node has no tool name")
        config = interpolate(dict(node.get("config") or {}), self._variables)
        self._log("info", f"▶ {name} {json.dumps(config, ensure_ascii=False, default=str)}")
        start = time.perf_counter()
        try:
            result = await self._registry.execute(name, config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log("error", f"✗ {name}: {type(exc).__name__}: {exc}")
            raise
        elapsed = (time.perf_counter() - start) * 1000
        rendered = json.dumps(jsonable(result), ensure_ascii=False, default=str)
        self._log("info", f"✓ {name} ({elapsed:.0f} ms) → {short(rendered)}")
        save_as = node.get("save_as")
        if save_as:
            self._variables[str(save_as)] = result

    def _init_loop(self, spec: dict[str, Any]) -> dict[str, Any]:
        max_iter = int(spec.get("max_iterations") or 100)
        if str(spec.get("mode") or "foreach") == "while":
            return {"mode": "while", "i": 0, "max": max_iter}
        seq = lookup(self._variables, str(spec.get("var") or ""))
        if seq is None:
            raise FlowError(f"loop variable ${spec.get('var')!r} is not defined")
        try:
            items = list(seq)
        except TypeError:
            items = [seq]
        return {"mode": "foreach", "items": items, "i": 0}

    def _step_loop(self, node: dict[str, Any]) -> str | None:
        node_id = str(node["id"])
        spec = dict(node.get("loop") or {})
        st = self._loops.get(node_id)
        if st is None:
            st = self._init_loop(spec)
            self._loops[node_id] = st
        if st["mode"] == "foreach":
            items = st["items"]
            if st["i"] >= len(items):
                del self._loops[node_id]
                self._log("debug", "loop exhausted → done")
                return self.next_by_handle(node_id, "done")
            var_name = str(spec.get("as") or "item")
            self._variables[var_name] = items[st["i"]]
            self._log("debug", f"loop iteration {st['i'] + 1}/{len(items)} → {var_name}")
            st["i"] += 1
            return self.next_by_handle(node_id, "body")
        if st["i"] >= int(st["max"]):
            del self._loops[node_id]
            self._log("error", f"while-loop hit max_iterations={st['max']} → done")
            return self.next_by_handle(node_id, "done")
        condition = dict(spec.get("condition") or {})
        if not evaluate_condition(condition, self._variables):
            del self._loops[node_id]
            self._log("debug", "while condition is false → done")
            return self.next_by_handle(node_id, "done")
        st["i"] += 1
        return self.next_by_handle(node_id, "body")

    async def execute_node(self, node: dict[str, Any]) -> str | None:
        """Execute one node; return the next node id or ``None`` to finish."""
        kind = str(node.get("kind") or "")
        node_id = str(node["id"])
        if kind == "start":
            return self.next_by_handle(node_id, "out")
        if kind == "end":
            return None
        if kind == "tool":
            await self._run_tool(node)
            return self.next_by_handle(node_id, "out")
        if kind == "set":
            cfg = dict(node.get("config") or {})
            var = str(cfg.get("var") or "")
            if not var:
                raise FlowError("set node needs a variable name")
            vtype = str(cfg.get("type") or "auto")
            value = parse_set_value(cfg.get("value"), vtype, self._variables)
            self._variables[var] = value
            self._log("debug", f"set ${var} = {short(repr(jsonable(value)))}")
            return self.next_by_handle(node_id, "out")
        if kind == "if":
            condition = dict(node.get("condition") or {})
            branch = evaluate_condition(condition, self._variables)
            self._log(
                "debug",
                f"if {condition.get('var')} {condition.get('op')} → {branch}",
            )
            return self.next_by_handle(node_id, "true" if branch else "false")
        if kind == "loop":
            return self._step_loop(node)
        raise FlowError(f"unknown node kind {kind!r}")

    # ------------------------------------------------------------- full run

    def _start_node(self, doc: dict[str, Any]) -> dict[str, Any]:
        if doc.get("version") != FLOW_VERSION:
            raise FlowError(f"unsupported flow version {doc.get('version')!r}")
        start_node: dict[str, Any] | None = next(
            (n for n in doc.get("nodes") or [] if n.get("kind") == "start"), None
        )
        if start_node is None:
            raise FlowError("flow has no start node")
        return start_node

    async def run(self, doc: dict[str, Any]) -> str:
        """Execute the whole graph; returns ``"finished"`` on success.

        Raises:
            FlowError: On validation problems or a failing node (the node
                id is part of the message).
        """
        start = self._start_node(doc)
        nodes = {str(n["id"]): n for n in doc.get("nodes") or []}
        self._edges = list(doc.get("edges") or self._edges)
        node: dict[str, Any] | None = start
        while node is not None:
            try:
                next_id: str | None = await self.execute_node(node)
            except FlowError as exc:
                raise FlowError(f"node {node['id']}: {exc}") from exc
            except Exception as exc:
                raise FlowError(
                    f"node {node['id']}: {type(exc).__name__}: {exc}"
                ) from exc
            node = nodes.get(next_id) if next_id is not None else None
        return "finished"
