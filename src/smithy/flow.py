"""Flow-v2 execution core: interpolation, conditions, typed values, graph runner.

This is the engine-side executor for flow documents (the same format the
designer edits and the debugger steps through). It is deliberately
interactive-free: no gates, no breakpoints, no REPL — those live in the
designer's debugger on top of :class:`FlowRunner`. Used standalone:

    from smithy.flow import FlowRunner
    from smithy.core.registry import ToolRegistry

    runner = FlowRunner(ToolRegistry(), log=print)
    await runner.run(doc)   # raises FlowError on failure

Flows are data, not code: they can be validated, allowlisted and
distributed by the orchestrator. The runner therefore never executes
anything but registered tools, and supports:

- ``on_error`` per node: ``stop`` (default) / ``continue`` (save the
  error into a variable and proceed) / ``retry`` (bounded retries);
- ``key`` in tool configs — selectors from a :class:`SelectorStore`
  (the dev-capture workflow works in flows too, via
  ``SMITHY_DEV_CAPTURE``);
- ``${asset:name}`` interpolation — runtime secrets via an
  :class:`AssetProvider`; asset values are redacted from logs;
- ``flow`` nodes — subflows (inline document or file), sharing the
  variable scope, with recursion depth capped.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from smithy.core.errors import ElementNotFound
from smithy.core.redact import redact_text
from smithy.core.selectors import SelectorStore

if TYPE_CHECKING:
    from smithy.core.assets import AssetProvider
    from smithy.core.registry import ToolRegistry

FLOW_VERSION = 2

_VAR_RE = re.compile(r"\$(\w+)((?:\.\w+|\[[^\[\]]+\])*)")
_REF_PART_RE = re.compile(r"\.(\w+)|\[([^\[\]]+)\]")
_ASSET_RE = re.compile(r"\$\{asset:([^}]+)\}")

_REPR_LIMIT = 2000

_ERROR_POLICIES = ("stop", "continue", "retry")
_MAX_FLOW_DEPTH = 8


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
            raise FlowError(f"cannot resolve ${name}{path}: {type(exc).__name__}: {exc}") from exc
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


def interpolate(
    value: Any,
    variables: dict[str, Any],
    *,
    resolve_asset: Any = None,
) -> Any:
    """Substitute ``$name``/``$name.pid``/``${asset:name}`` in config values.

    A string that is a single reference keeps the referenced value's type;
    mixed text interpolates the referenced value as text. Unknown variable
    names are left as-is. ``${asset:name}`` resolves through *resolve_asset*
    (an :class:`~smithy.core.assets.AssetProvider`); unknown assets raise,
    which fails the node.
    """
    if isinstance(value, str):
        if resolve_asset is not None and "${asset:" in value:
            value = _ASSET_RE.sub(lambda m: str(resolve_asset(m.group(1))), value)
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
        return [interpolate(item, variables, resolve_asset=resolve_asset) for item in value]
    if isinstance(value, dict):
        return {
            key: interpolate(item, variables, resolve_asset=resolve_asset)
            for key, item in value.items()
        }
    return value


def evaluate_condition(
    condition: dict[str, Any],
    variables: dict[str, Any],
    *,
    resolve_asset: Any = None,
) -> bool:
    var = str(condition.get("var") or "")
    op = str(condition.get("op") or "exists")
    left: Any = lookup(variables, var)
    right: Any = interpolate(condition.get("value"), variables, resolve_asset=resolve_asset)
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


def parse_set_value(
    raw: Any,
    vtype: str,
    variables: dict[str, Any],
    *,
    resolve_asset: Any = None,
) -> Any:
    """Interpret a ``set`` node value with ``$ref`` support.

    A value that is a single reference (``$app``, ``$app.pid``) takes the
    referenced value as-is, keeping its type. Anything else is interpolated
    as text first, then parsed according to *vtype*.
    """
    if isinstance(raw, str):
        exact = _VAR_RE.fullmatch(raw)
        if exact and exact.group(1) in variables:
            return _resolve_ref(exact.group(1), exact.group(2), variables)
        return parse_typed_value(interpolate(raw, variables, resolve_asset=resolve_asset), vtype)
    return parse_typed_value(raw, vtype)


def _parse_on_error(raw: Any) -> dict[str, Any]:
    """Normalize an ``on_error`` spec; raises on invalid policies."""
    if raw is None:
        return {"policy": "stop"}
    if not isinstance(raw, dict):
        raise FlowError("on_error must be an object")
    policy = str(raw.get("policy") or "stop")
    if policy not in _ERROR_POLICIES:
        raise FlowError(f"unknown on_error policy {policy!r} (expected one of {_ERROR_POLICIES})")
    spec: dict[str, Any] = {"policy": policy}
    if policy == "retry":
        retries = raw.get("retries", 2)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise FlowError("on_error.retries must be an int >= 0")
        delay_ms = raw.get("delay_ms", 500)
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or delay_ms < 0:
            raise FlowError("on_error.delay_ms must be an int >= 0")
        spec["retries"] = retries
        spec["delay_ms"] = delay_ms
    save_error_as = raw.get("save_error_as")
    if save_error_as is not None:
        if not isinstance(save_error_as, str) or not save_error_as:
            raise FlowError("on_error.save_error_as must be a non-empty string")
        spec["save_error_as"] = save_error_as
    return spec


LogFn = Any  # Callable[[str, str], None]


def _noop_log(level: str, msg: str) -> None:
    pass


class FlowRunner:
    """Executes a flow graph node by node against a tool registry.

    The runner keeps loop state and the variable scope (a plain dict it
    mutates — pass the same dict to share scope with a debugger/REPL).

    Args:
        assets: Asset provider for ``${asset:name}`` interpolation
            (default: ``EnvAssetProvider``).
        selector_store: Selector registry for ``key`` fields in tool
            configs (default: ``SMITHY_SELECTOR_STORE`` env or
            ``selectors.json``).
        dev_capture: Interactive re-capture of missing/stale keys
            (default: ``SMITHY_DEV_CAPTURE`` env).
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        variables: dict[str, Any] | None = None,
        edges: list[dict[str, Any]] | None = None,
        log: LogFn | None = None,
        assets: AssetProvider | None = None,
        selector_store: SelectorStore | None = None,
        dev_capture: bool | None = None,
    ) -> None:
        self._registry = registry
        self._variables: dict[str, Any] = variables if variables is not None else {}
        self._edges: list[dict[str, Any]] = edges or []
        self._log = log or _noop_log
        self._logging = log is not None
        self._loops: dict[str, dict[str, Any]] = {}
        self._assets = assets
        self._selector_store = selector_store
        self._dev_capture = dev_capture
        self._secrets: list[str] = []
        self._flow_depth = 0

    # ------------------------------------------------------------ navigation

    def next_by_handle(self, node_id: str, handle: str) -> str | None:
        for edge in self._edges:
            if edge.get("source") == node_id and edge.get("source_handle") == handle:
                return str(edge.get("target"))
        return None

    # ------------------------------------------------------------ interpolation

    def _resolve_asset(self, name: str) -> str:
        from smithy.core.assets import EnvAssetProvider

        assets = self._assets if self._assets is not None else EnvAssetProvider()
        value = str(assets.get(name))
        if value:
            self._secrets.append(value)
        return value

    def _render_config(self, config: dict[str, Any]) -> str:
        rendered = json.dumps(config, ensure_ascii=False, default=str)
        return short(redact_text(rendered, self._secrets))

    def _interpolate(self, value: Any) -> Any:
        return interpolate(value, self._variables, resolve_asset=self._resolve_asset)

    # ------------------------------------------------------------ selector keys

    def _store(self) -> SelectorStore:
        import os

        if self._selector_store is None:
            path = os.environ.get("SMITHY_SELECTOR_STORE", "selectors.json")
            self._selector_store = SelectorStore(path)
        return self._selector_store

    def _dev_capture_enabled(self) -> bool:
        import os

        if self._dev_capture is not None:
            return self._dev_capture
        return os.environ.get("SMITHY_DEV_CAPTURE", "").strip().lower() in ("1", "true", "yes")

    async def _apply_selector_key(self, config: dict[str, Any]) -> str | None:
        """Resolve a tool config's ``key`` field from the selector store.

        Returns the key (or ``None``); mutates *config* in place. In dev
        capture mode a missing key is recorded interactively; in
        production it is a hard error.
        """
        key = config.get("key")
        config.pop("key", None)
        if key is None:
            return None
        if not isinstance(key, str) or not key:
            raise FlowError("config.key must be a non-empty string")
        entry = self._store().get(key)
        if entry is not None:
            for field_name, value in entry.items():
                config.setdefault(field_name, value)
            return key
        if not self._dev_capture_enabled():
            raise FlowError(
                f"no selector stored for key {key!r} in {self._store().path} — "
                "record it with dev capture (SMITHY_DEV_CAPTURE=1) or store it manually"
            )
        from smithy.windows.tools.selector_capture import capture_once_async

        captured = await capture_once_async()
        self._store().put(key, captured.selector)
        config.update(captured.selector)
        return key

    # ------------------------------------------------------------ node steps

    async def _run_tool(self, node: dict[str, Any]) -> None:
        name = str(node.get("tool") or "")
        if not name:
            raise FlowError("tool node has no tool name")
        config = self._interpolate(dict(node.get("config") or {}))
        key = await self._apply_selector_key(config)
        if self._logging:
            self._log("info", f"▶ {name} {self._render_config(config)}")
        start = time.perf_counter()
        try:
            result = await self._registry.execute(name, config)
        except asyncio.CancelledError:
            raise
        except ElementNotFound:
            if key is None or not self._dev_capture_enabled():
                raise
            from smithy.windows.tools.selector_capture import capture_once_async

            captured = await capture_once_async()
            self._store().put(key, captured.selector)
            for field_name in ("name", "automation_id", "control_type", "class_name"):
                config.pop(field_name, None)
            config.update(captured.selector)
            result = await self._registry.execute(name, config)
        except Exception as exc:
            if self._logging:
                self._log("error", f"✗ {name}: {type(exc).__name__}: {exc}")
            raise
        if self._logging:
            elapsed = (time.perf_counter() - start) * 1000
            rendered = json.dumps(jsonable(result), ensure_ascii=False, default=str)
            rendered = redact_text(rendered, self._secrets)
            self._log("info", f"✓ {name} ({elapsed:.0f} ms) → {short(rendered)}")
        save_as = node.get("save_as")
        if save_as:
            self._variables[str(save_as)] = result

    async def _run_subflow(self, node: dict[str, Any]) -> None:
        config = self._interpolate(dict(node.get("config") or {}))
        path = config.get("path")
        doc = config.get("doc")
        if (path is None) == (doc is None):
            raise FlowError("flow node needs exactly one of 'path' or 'doc'")
        if path is not None:
            if not isinstance(path, str) or not path:
                raise FlowError("flow node 'path' must be a non-empty string")
            try:
                doc = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FlowError(f"cannot read subflow {path!r}: {exc}") from exc
        if not isinstance(doc, dict):
            raise FlowError("flow node 'doc' must be a flow document object")
        if self._flow_depth >= _MAX_FLOW_DEPTH:
            raise FlowError(f"subflow nesting deeper than {_MAX_FLOW_DEPTH}")
        inputs = config.get("inputs")
        if inputs is not None:
            if not isinstance(inputs, dict):
                raise FlowError("flow node 'inputs' must be an object")
            for var, value in inputs.items():
                self._variables[str(var)] = value
        child = FlowRunner(
            self._registry,
            variables=self._variables,
            log=self._log,
            assets=self._assets,
            selector_store=self._selector_store,
            dev_capture=self._dev_capture,
        )
        child._flow_depth = self._flow_depth + 1
        await child.run(doc)

    async def _run_with_on_error(self, node: dict[str, Any], step: Any) -> None:
        """Run *step* honoring the node's ``on_error`` spec."""
        spec = _parse_on_error(node.get("on_error"))
        policy = spec["policy"]
        if policy == "stop":
            await step(node)
            return
        node_id = str(node["id"])
        attempts = spec.get("retries", 0) + 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                await step(node)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                if attempt < attempts - 1:
                    self._log(
                        "warning",
                        f"node {node_id}: attempt {attempt + 1}/{attempts} failed "
                        f"({type(exc).__name__}: {exc}) — retrying",
                    )
                    await asyncio.sleep(spec.get("delay_ms", 500) / 1000)
        if policy == "continue":
            var = str(spec.get("save_error_as") or "_error")
            self._variables[var] = f"{type(last).__name__}: {last}"
            self._log(
                "error",
                f"✗ node {node_id} failed (policy=continue) → ${var} = {last}",
            )
            return
        raise last  # type: ignore[misc]

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
        if not evaluate_condition(condition, self._variables, resolve_asset=self._resolve_asset):
            del self._loops[node_id]
            self._log("debug", "while condition is false → done")
            return self.next_by_handle(node_id, "done")
        st["i"] += 1
        return self.next_by_handle(node_id, "body")

    async def execute_node(self, node: dict[str, Any]) -> str | None:
        """Execute one node; return the next node id or ``None`` to finish."""
        kind = str(node.get("kind") or "")
        node_id = str(node["id"])
        if node.get("on_error") is not None:
            _parse_on_error(node.get("on_error"))
        if kind == "start":
            return self.next_by_handle(node_id, "out")
        if kind == "end":
            return None
        if kind == "tool":
            await self._run_with_on_error(node, self._run_tool)
            return self.next_by_handle(node_id, "out")
        if kind == "flow":
            await self._run_with_on_error(node, self._run_subflow)
            return self.next_by_handle(node_id, "out")
        if kind == "set":
            cfg = dict(node.get("config") or {})
            var = str(cfg.get("var") or "")
            if not var:
                raise FlowError("set node needs a variable name")
            vtype = str(cfg.get("type") or "auto")
            value = parse_set_value(
                cfg.get("value"), vtype, self._variables, resolve_asset=self._resolve_asset
            )
            self._variables[var] = value
            if self._logging:
                rendered = redact_text(repr(jsonable(value)), self._secrets)
                self._log("debug", f"set ${var} = {short(rendered)}")
            return self.next_by_handle(node_id, "out")
        if kind == "if":
            condition = dict(node.get("condition") or {})
            branch = evaluate_condition(
                condition, self._variables, resolve_asset=self._resolve_asset
            )
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
                raise FlowError(f"node {node['id']}: {type(exc).__name__}: {exc}") from exc
            node = nodes.get(next_id) if next_id is not None else None
        return "finished"


# ------------------------------------------------------------------ validation


def validate_document(
    doc: Any,
    *,
    registry: ToolRegistry | None = None,
    selector_store: SelectorStore | None = None,
) -> list[str]:
    """Static checks for a flow document (dry-run mode).

    Verifies the version, the start node, unique node ids, edge
    endpoints, node shapes, ``on_error`` specs, and — when a *registry*
    is given — that every tool node's tool is registered and its
    interpolated-free config passes the tool's schema. When a
    *selector_store* is given, ``key`` fields must resolve.

    Returns:
        A list of human-readable problems (empty = the document is fine).
    """
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["document must be an object"]
    if doc.get("version") != FLOW_VERSION:
        problems.append(f"unsupported flow version {doc.get('version')!r}")
        return problems

    nodes: dict[str, dict[str, Any]] = {}
    for raw in doc.get("nodes") or []:
        if not isinstance(raw, dict):
            problems.append("node entry must be an object")
            continue
        node_id = str(raw.get("id") or "")
        if not node_id:
            problems.append("node without an id")
            continue
        if node_id in nodes:
            problems.append(f"duplicate node id {node_id!r}")
            continue
        nodes[node_id] = raw

    starts = [n for n in nodes.values() if n.get("kind") == "start"]
    if len(starts) != 1:
        problems.append(f"expected exactly one start node, found {len(starts)}")

    for node_id, node in nodes.items():
        kind = str(node.get("kind") or "")
        if kind not in ("start", "end", "tool", "set", "if", "loop", "flow"):
            problems.append(f"node {node_id!r}: unknown kind {kind!r}")
        if kind == "tool" and not node.get("tool"):
            problems.append(f"node {node_id!r}: tool node without a tool name")
        if kind == "flow":
            config = node.get("config") or {}
            if (config.get("path") is None) == (config.get("doc") is None):
                problems.append(f"node {node_id!r}: flow node needs exactly one of 'path' or 'doc'")
        if node.get("on_error") is not None:
            try:
                _parse_on_error(node.get("on_error"))
            except FlowError as exc:
                problems.append(f"node {node_id!r}: {exc}")
        if kind == "tool" and registry is not None:
            name = str(node.get("tool") or "")
            tool_obj = registry.get(name)
            if tool_obj is None:
                problems.append(f"node {node_id!r}: tool {name!r} is not registered")
            else:
                from smithy.core.schema import validate_against_schema

                config = node.get("config") or {}
                for field_problem in validate_against_schema(tool_obj.schema(), config):
                    problems.append(f"node {node_id!r}: {name}: {field_problem}")
                key = config.get("key") if isinstance(config, dict) else None
                if isinstance(key, str) and key:
                    if selector_store is not None:
                        if selector_store.get(key) is None:
                            problems.append(
                                f"node {node_id!r}: no selector stored for key {key!r} "
                                f"in {selector_store.path}"
                            )
                    else:
                        import os

                        store_path = os.environ.get("SMITHY_SELECTOR_STORE", "selectors.json")
                        try:
                            disk_store = SelectorStore(store_path)
                            if disk_store.get(key) is None:
                                problems.append(
                                    f"node {node_id!r}: no selector stored for key {key!r} "
                                    f"in {disk_store.path}"
                                )
                        except Exception:
                            pass

    for edge in doc.get("edges") or []:
        if not isinstance(edge, dict):
            problems.append("edge entry must be an object")
            continue
        source = edge.get("source")
        target = edge.get("target")
        if source not in nodes:
            problems.append(f"edge source {source!r} is not a node")
        if target not in nodes:
            problems.append(f"edge target {target!r} is not a node")
        if not edge.get("source_handle"):
            problems.append(f"edge {source!r}→{target!r} has no source_handle")
    return problems
