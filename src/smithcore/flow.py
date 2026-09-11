"""Flow-v2 execution core: interpolation, conditions, typed values, graph runner.

This is the engine-side executor for flow documents (the same format the
designer edits and the debugger steps through). It is deliberately
interactive-free: no gates, no breakpoints, no REPL — those live in the
designer's debugger on top of :class:`FlowRunner`. Used standalone:

    from smithcore.flow import FlowRunner
    from smithcore.core.registry import ToolRegistry

    runner = FlowRunner(ToolRegistry(), log=print)
    await runner.run(doc)   # raises FlowError on failure

Flows are data, not code: they can be validated, allowlisted and
distributed by the orchestrator. The runner therefore never executes
anything but registered tools, and supports:

- ``on_error`` per node: ``stop`` (default) / ``continue`` (save the
  error into a variable and proceed) / ``retry`` (bounded retries);
- ``key`` in tool configs — selectors from a :class:`SelectorStore`
  (the dev-capture workflow works in flows too, via
  ``SMITHCORE_DEV_CAPTURE``);
- ``${asset:name}`` interpolation — runtime secrets via an
  :class:`AssetProvider`; asset values are redacted from logs;
- ``flow`` nodes — subflows (inline document or file); in global scope a
  subflow *reads* the parent variables but its own writes stay local, and
  declared ``outputs`` are the only values copied back.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from smithcore.core.errors import BusinessError, Cancelled, ElementNotFound, InfrastructureError
from smithcore.core.redact import redact_text
from smithcore.core.selectors import SelectorStore

if TYPE_CHECKING:
    from smithcore.core.assets import AssetProvider
    from smithcore.core.registry import ToolRegistry

FLOW_VERSION = 2

_VAR_RE = re.compile(r"\$(\w+)((?:\.\w+|\[[^\[\]]+\])*)")
_REF_PART_RE = re.compile(r"\.(\w+)|\[([^\[\]]+)\]")
_ASSET_RE = re.compile(r"\$\{asset:([^}]+)\}")
#: Variable names are plain Python identifiers — a leading ``$`` is only
#: ever the *reference* syntax, never part of a name.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")


def _identifier_ok(name: Any) -> bool:
    return isinstance(name, str) and bool(_IDENTIFIER_RE.match(name))


def _collect_strings(value: Any) -> list[str]:
    """Every non-empty string leaf in *value* (used to register secrets)."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        return [item for child in value.values() for item in _collect_strings(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _collect_strings(child)]
    return []


#: A variable whose name starts with this prefix is global (visible to
#: every subflow at any depth).
_GLOBAL_PREFIX = "G_"

_VAR_TYPES = ("auto", "string", "number", "bool", "json")

#: Declared variable type → JSON-schema field types it may feed (exact ``$ref``).
_SCHEMA_TYPE_OK: dict[str, frozenset[str]] = {
    "integer": frozenset({"integer", "number", "auto"}),
    "number": frozenset({"integer", "number", "auto"}),
    "string": frozenset({"string", "auto"}),
    "boolean": frozenset({"bool", "auto"}),
    "array": frozenset({"json", "auto"}),
    "object": frozenset({"json", "auto"}),
}


def _declared_types(doc: dict[str, Any]) -> dict[str, str]:
    """Declared type per variable (typed-list form only; object form is untyped)."""
    types: dict[str, str] = {}
    raw = doc.get("variables")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    types[name] = str(item.get("type") or "auto")
    return types


def _global_names(doc: dict[str, Any]) -> set[str]:
    """Names prefixed ``G_`` — visible to every subflow at any depth."""
    names: set[str] = set()
    raw = doc.get("variables")
    if isinstance(raw, dict):
        names.update(key for key in raw if isinstance(key, str) and key.startswith(_GLOBAL_PREFIX))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name.startswith(_GLOBAL_PREFIX):
                    names.add(name)
    return names


def check_variable_types(doc: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """Fail-fast checks: every declared variable value matches its type.

    Returns a list of human-readable problems (empty = fine). ``auto``
    variables are never checked.
    """
    problems: list[str] = []
    for name, dtype in _declared_types(doc).items():
        if dtype == "auto" or name not in values:
            continue
        value = values[name]
        if dtype == "string":
            ok = isinstance(value, str)
        elif dtype == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif dtype == "bool":
            ok = isinstance(value, bool)
        elif dtype == "json":
            ok = value is None or isinstance(value, (dict, list, str, int, float, bool))
        else:
            problems.append(f"variable {name!r}: unknown type {dtype!r}")
            continue
        if not ok:
            problems.append(f"variable {name!r}: expected {dtype}, got {type(value).__name__}")
    return problems


def _ref_type_problems(
    node_id: str,
    tool_name: str,
    tool_schema: dict[str, Any],
    config: dict[str, Any],
    declared: dict[str, str],
) -> list[str]:
    """Type-check exact ``$ref`` field values against declared variable types."""
    problems: list[str] = []
    properties = tool_schema.get("properties") or {}
    for key, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        allowed = _SCHEMA_TYPE_OK.get(str(spec.get("type")))
        if allowed is None:
            continue
        value = config.get(key)
        if not isinstance(value, str):
            continue
        match = _VAR_RE.fullmatch(value)
        if match is None:
            continue
        variable = match.group(1)
        declared_type = declared.get(variable)
        if declared_type is None or declared_type == "auto":
            continue
        if declared_type not in allowed:
            problems.append(
                f"node {node_id!r}: {tool_name}: {key!r} expects {spec.get('type')}, "
                f"but ${variable} is declared {declared_type}"
            )
    return problems


_REPR_LIMIT = 2000

_ERROR_POLICIES = ("stop", "continue", "retry")
_MAX_FLOW_DEPTH = 8

#: Source handles each node kind may emit (validated by ``validate_document``).
_ALLOWED_HANDLES: dict[str, frozenset[str]] = {
    "start": frozenset({"out"}),
    "tool": frozenset({"out", "error"}),
    "flow": frozenset({"out", "error"}),
    "set": frozenset({"out", "error"}),
    "if": frozenset({"true", "false", "error"}),
    "loop": frozenset({"body", "done", "error"}),
}

#: Hard cap on node executions per run. A cycle of synchronous nodes would
#: otherwise spin forever (it never yields to the event loop, so even
#: ``asyncio.wait_for`` cannot cancel it).
_DEFAULT_MAX_STEPS = 100_000

#: Subflow file cap (DoS guard independent of node validation).
_MAX_SUBFLOW_BYTES = 5_000_000
#: Max tracked secret values (bounds memory on long runs).
_MAX_SECRETS = 1000


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
    (an :class:`~smithcore.core.assets.AssetProvider`); unknown assets raise,
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
    if vtype in ("auto", "json") and not isinstance(value, str):
        # Already a structured/typed value — keep it instead of
        # stringifying a Python repr that is not valid JSON.
        return value
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
            configs (default: ``SMITHCORE_SELECTOR_STORE`` env or
            ``selectors.json``).
        dev_capture: Interactive re-capture of missing/stale keys
            (default: ``SMITHCORE_DEV_CAPTURE`` env).
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
        max_steps: int = _DEFAULT_MAX_STEPS,
    ) -> None:
        self._registry = registry
        self._variables: dict[str, Any] = variables if variables is not None else {}
        self._edges: list[dict[str, Any]] = list(edges or [])
        self._edges_default: list[dict[str, Any]] = list(edges or [])
        self._max_steps = max_steps
        self._log = log or _noop_log
        self._logging = log is not None
        self._loops: dict[str, dict[str, Any]] = {}
        self._assets = assets
        self._selector_store = selector_store
        self._dev_capture = dev_capture
        self._secrets: list[str] = []
        self._secrets_set: set[str] = set()
        self._secret_names: set[str] = set()
        self._globals: dict[str, Any] = {}
        self._flow_depth = 0

    # ------------------------------------------------------------ navigation

    def next_by_handle(self, node_id: str, handle: str) -> str | None:
        for edge in self._edges:
            if edge.get("source") == node_id and edge.get("source_handle") == handle:
                return str(edge.get("target"))
        return None

    # ------------------------------------------------------------ interpolation

    def _track_secret(self, value: str) -> None:
        if not value or value in self._secrets_set:
            return
        if len(self._secrets) >= _MAX_SECRETS:
            return
        self._secrets_set.add(value)
        self._secrets.append(value)

    def _resolve_asset(self, name: str) -> str:
        from smithcore.core.assets import asset_provider_from_env

        assets = self._assets if self._assets is not None else asset_provider_from_env()
        value = str(assets.get(name))
        if value:
            self._track_secret(value)
        return value

    def _render_config(self, config: dict[str, Any]) -> str:
        rendered = json.dumps(config, ensure_ascii=False, default=str)
        return short(redact_text(rendered, self._secrets))

    def _remember_secrets(self, value: Any) -> None:
        """Register secret string leaves so they are redacted everywhere."""
        for secret in _collect_strings(value):
            self._track_secret(secret)

    def public_variables(self) -> dict[str, Any]:
        """Variables safe to report back: secrets removed by name and by value."""
        out: dict[str, Any] = {}
        for key, value in self._variables.items():
            if key in self._secret_names:
                continue
            if isinstance(value, str) and value in self._secrets_set:
                continue
            out[key] = value
        return out

    def _interpolate(self, value: Any) -> Any:
        return interpolate(value, self._variables, resolve_asset=self._resolve_asset)

    # ------------------------------------------------------------ selector keys

    def _store(self) -> SelectorStore:
        import os

        if self._selector_store is None:
            path = os.environ.get("SMITHCORE_SELECTOR_STORE", "selectors.json")
            self._selector_store = SelectorStore(path)
        return self._selector_store

    def _dev_capture_enabled(self) -> bool:
        import os

        if self._dev_capture is not None:
            return self._dev_capture
        return os.environ.get("SMITHCORE_DEV_CAPTURE", "").strip().lower() in ("1", "true", "yes")

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
                "record it with dev capture (SMITHCORE_DEV_CAPTURE=1) or store it manually"
            )
        from smithcore.windows.tools.selector_capture import capture_once_async

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
        tool_obj = self._registry.get(name)
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
            from smithcore.windows.tools.selector_capture import capture_once_async

            captured = await capture_once_async()
            self._store().put(key, captured.selector)
            for field_name in ("name", "automation_id", "control_type", "class_name"):
                config.pop(field_name, None)
            config.update(captured.selector)
            result = await self._registry.execute(name, config)
        except Exception as exc:
            if self._logging:
                message = redact_text(f"{type(exc).__name__}: {exc}", self._secrets)
                self._log("error", f"✗ {name}: {message}")
            raise
        secret_result = getattr(tool_obj, "produces_secrets", False)
        if secret_result:
            self._remember_secrets(result)
        if self._logging:
            elapsed = (time.perf_counter() - start) * 1000
            rendered = json.dumps(jsonable(result), ensure_ascii=False, default=str)
            rendered = redact_text(rendered, self._secrets)
            self._log("info", f"✓ {name} ({elapsed:.0f} ms) → {short(rendered)}")
        save_as = node.get("save_as")
        if save_as:
            self._variables[str(save_as)] = result
            if secret_result:
                self._secret_names.add(str(save_as))

    async def _run_subflow(self, node: dict[str, Any]) -> None:
        # Only ``path``/``inputs`` are interpolated: an inline ``doc`` is a
        # nested flow document and must be evaluated in the child's scope,
        # not rewritten with the parent's variables.
        raw = dict(node.get("config") or {})
        config = dict(raw)
        if "path" in raw:
            config["path"] = self._interpolate(raw["path"])
        if "inputs" in raw:
            config["inputs"] = self._interpolate(raw["inputs"])
        path = config.get("path")
        doc = config.get("doc")
        if (path is None) == (doc is None):
            raise FlowError("flow node needs exactly one of 'path' or 'doc'")
        if path is not None:
            if not isinstance(path, str) or not path:
                raise FlowError("flow node 'path' must be a non-empty string")
            try:
                from smithcore.core.files import ENV_FILE_ROOT, confine_path

                confined = confine_path(Path(path), env_var=ENV_FILE_ROOT)
                size = confined.stat().st_size if confined.is_file() else 0
                if size > _MAX_SUBFLOW_BYTES:
                    raise FlowError(f"subflow {path!r} too large ({size} bytes)")
                # Off the event loop: plain file IO goes to the default
                # thread pool, not the single COM/UIA worker thread.
                raw_text: str = await asyncio.to_thread(confined.read_text, encoding="utf-8")
                if len(raw_text.encode("utf-8")) > _MAX_SUBFLOW_BYTES:
                    raise FlowError(f"subflow {path!r} too large")
                doc = json.loads(raw_text)
            except FlowError:
                raise
            except (OSError, json.JSONDecodeError) as exc:
                raise FlowError(f"cannot read subflow {path!r}: {exc}") from exc
        if not isinstance(doc, dict):
            raise FlowError("flow node 'doc' must be a flow document object")
        if self._flow_depth >= _MAX_FLOW_DEPTH:
            raise FlowError(f"subflow nesting deeper than {_MAX_FLOW_DEPTH}")

        inputs = config.get("inputs")
        if inputs is not None and not isinstance(inputs, dict):
            raise FlowError("flow node 'inputs' must be an object")

        # Every subflow runs isolated: it starts with the project globals
        # (declared on the main flow) plus the declared inputs, its own writes
        # stay local, and only declared ``outputs`` are copied back.
        child_variables: dict[str, Any] = dict(self._globals)
        for var, value in (inputs or {}).items():
            child_variables[str(var)] = value

        child = FlowRunner(
            self._registry,
            variables=child_variables,
            log=self._log,
            assets=self._assets,
            selector_store=self._selector_store,
            dev_capture=self._dev_capture,
            max_steps=self._max_steps,
        )
        child._flow_depth = self._flow_depth + 1
        child._globals = self._globals
        child._secrets = self._secrets
        child._secrets_set = self._secrets_set
        child._secret_names = self._secret_names
        await child.run(doc)

        outputs = config.get("outputs")
        if outputs is not None:
            self._copy_outputs(outputs, child_variables)

    def _copy_outputs(self, outputs: Any, child_variables: dict[str, Any]) -> None:
        """Copy declared child variables into the parent scope."""
        if isinstance(outputs, list):
            for name in outputs:
                if not isinstance(name, str) or not name:
                    raise FlowError("flow node 'outputs' entries must be non-empty strings")
                if name in child_variables:
                    self._variables[name] = child_variables[name]
            return
        if isinstance(outputs, dict):
            for parent_name, child_name in outputs.items():
                if not isinstance(parent_name, str) or not isinstance(child_name, str):
                    raise FlowError("flow node 'outputs' keys and values must be strings")
                if child_name in child_variables:
                    self._variables[parent_name] = child_variables[child_name]
            return
        raise FlowError("flow node 'outputs' must be a list or an object")

    async def _run_guarded(
        self, node: dict[str, Any], step: Any, *, continue_handle: str
    ) -> str | None:
        """Run *step* (which returns an output handle) honoring ``on_error``.

        ``stop`` fails the run; ``retry`` retries then fails; ``continue``
        saves the error and takes *continue_handle*. Returns the resolved
        next node id (or ``None``).
        """
        spec = _parse_on_error(node.get("on_error"))
        policy = spec["policy"]
        node_id = str(node["id"])
        if policy == "stop":
            handle = await step(node)
            return self.next_by_handle(node_id, handle)
        attempts = spec.get("retries", 0) + 1 if policy == "retry" else 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                handle = await step(node)
                return self.next_by_handle(node_id, handle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                if policy == "retry" and attempt < attempts - 1:
                    detail = redact_text(f"{type(exc).__name__}: {exc}", self._secrets)
                    self._log(
                        "warning",
                        f"node {node_id}: attempt {attempt + 1}/{attempts} failed "
                        f"({detail}) — retrying",
                    )
                    await asyncio.sleep(spec.get("delay_ms", 500) / 1000)
                    continue
                if policy == "continue":
                    var = str(spec.get("save_error_as") or "_error")
                    self._variables[var] = f"{type(exc).__name__}: {exc}"
                    detail = redact_text(f"{type(exc).__name__}: {exc}", self._secrets)
                    self._log(
                        "error",
                        f"✗ node {node_id} failed (policy=continue) → ${var} = {detail}",
                    )
                    return self.next_by_handle(node_id, continue_handle)
                raise
        raise last  # type: ignore[misc]

    async def _step_tool(self, node: dict[str, Any]) -> str:
        await self._run_tool(node)
        return "out"

    async def _step_flow(self, node: dict[str, Any]) -> str:
        await self._run_subflow(node)
        return "out"

    def _raise_fail(self, node: dict[str, Any]) -> None:
        """End the run with a business or infrastructure failure.

        ``BusinessError`` means bad data (terminal, no retry); the
        transaction runner records ``business_failed``. ``InfrastructureError``
        is a system failure (the item is retried within its budget).
        """
        config = dict(node.get("config") or {})
        mode = str(config.get("mode") or "business")
        message = self._interpolate(config.get("message"))
        text = str(message).strip() if message is not None else ""
        if not text:
            text = f"flow stopped at node {node.get('id')!r}"
        if mode == "business":
            raise BusinessError(text)
        if mode == "system":
            raise InfrastructureError(text)
        raise FlowError(f"fail node 'mode' must be 'business' or 'system', got {mode!r}")

    async def _step_set(self, node: dict[str, Any]) -> str:
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
        return "out"

    async def _step_if(self, node: dict[str, Any]) -> str:
        condition = dict(node.get("condition") or {})
        branch = evaluate_condition(condition, self._variables, resolve_asset=self._resolve_asset)
        self._log(
            "debug",
            f"if {condition.get('var')} {condition.get('op')} → {branch}",
        )
        return "true" if branch else "false"

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

    def _step_loop(self, node: dict[str, Any]) -> str:
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
                return "done"
            var_name = str(spec.get("as") or "item")
            self._variables[var_name] = items[st["i"]]
            self._log("debug", f"loop iteration {st['i'] + 1}/{len(items)} → {var_name}")
            st["i"] += 1
            return "body"
        if st["i"] >= int(st["max"]):
            del self._loops[node_id]
            self._log("error", f"while-loop hit max_iterations={st['max']} → done")
            return "done"
        condition = dict(spec.get("condition") or {})
        if not evaluate_condition(condition, self._variables, resolve_asset=self._resolve_asset):
            del self._loops[node_id]
            self._log("debug", "while condition is false → done")
            return "done"
        st["i"] += 1
        return "body"

    async def execute_node(self, node: dict[str, Any]) -> str | None:
        """Execute one node; return the next node id or ``None`` to finish."""
        kind = str(node.get("kind") or "")
        node_id = str(node["id"])
        if kind == "start":
            return self.next_by_handle(node_id, "out")
        if kind == "end":
            return None
        if kind == "fail":
            self._raise_fail(node)
        if node.get("on_error") is not None:
            _parse_on_error(node.get("on_error"))
        if kind == "tool":
            return await self._run_guarded(node, self._step_tool, continue_handle="out")
        if kind == "flow":
            return await self._run_guarded(node, self._step_flow, continue_handle="out")
        if kind == "set":
            return await self._run_guarded(node, self._step_set, continue_handle="out")
        if kind == "if":
            return await self._run_guarded(node, self._step_if, continue_handle="false")
        if kind == "loop":
            return await self._run_guarded(
                node, self._step_loop_sync_wrapper, continue_handle="done"
            )
        raise FlowError(f"unknown node kind {kind!r}")

    async def _step_loop_sync_wrapper(self, node: dict[str, Any]) -> str:
        return self._step_loop(node)

    # ------------------------------------------------------------- full run

    def _start_node(self, doc: dict[str, Any]) -> dict[str, Any]:
        if doc.get("version") != FLOW_VERSION:
            raise FlowError(f"unsupported flow version {doc.get('version')!r}")
        nodes = doc.get("nodes")
        if not isinstance(nodes, list):
            raise FlowError("flow 'nodes' must be an array")
        start_node: dict[str, Any] | None = next(
            (n for n in nodes if isinstance(n, dict) and n.get("kind") == "start"), None
        )
        if start_node is None:
            raise FlowError("flow has no start node")
        return start_node

    def _collect_nodes(self, doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
        nodes: dict[str, dict[str, Any]] = {}
        for raw in doc.get("nodes") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                raise FlowError("flow has a node without an id")
            nodes[str(raw["id"])] = raw
        return nodes

    async def run(self, doc: dict[str, Any]) -> str:
        """Execute the whole graph; returns ``"finished"`` on success.

        Raises:
            FlowError: On validation problems, a node without an id, a
                dangling edge, a run that exceeds ``max_steps`` (a cycle),
                or a failing node (the node id is part of the message).
        """
        if not isinstance(doc, dict):
            raise FlowError("flow document must be an object")
        start = self._start_node(doc)
        nodes = self._collect_nodes(doc)
        if "edges" in doc:
            self._edges = list(doc.get("edges") or [])
        else:
            self._edges = list(self._edges_default)
        self._loops = {}
        # Globals declared on this document become visible to every subflow
        # at any depth; a child inherits what the parent already has.
        declared_globals = _global_names(doc)
        if declared_globals:
            self._globals = {
                **self._globals,
                **{
                    name: self._variables[name]
                    for name in declared_globals
                    if name in self._variables
                },
            }
        steps = 0
        node: dict[str, Any] | None = start
        while node is not None:
            node_id = str(node.get("id"))
            steps += 1
            if steps > self._max_steps:
                raise FlowError(
                    f"flow exceeded {self._max_steps} node executions "
                    "(probable cycle) at node "
                    f"{node_id!r}"
                )
            try:
                next_id: str | None = await self.execute_node(node)
            except asyncio.CancelledError:
                raise
            except (BusinessError, InfrastructureError, Cancelled):
                # Domain failures must reach the caller unchanged so the
                # transaction runner can classify them (business/system/stop).
                raise
            except Exception as exc:
                next_id = self._route_error_edge(node, exc)
                if next_id is None:
                    if isinstance(exc, FlowError):
                        raise FlowError(f"node {node_id}: {exc}") from exc
                    raise FlowError(f"node {node_id}: {type(exc).__name__}: {exc}") from exc
            if next_id is None:
                node = None
                break
            node = nodes.get(next_id)
            if node is None:
                raise FlowError(f"edge target {next_id!r} is not a node")
        return "finished"

    def _route_error_edge(self, node: dict[str, Any], exc: Exception) -> str | None:
        """Return the ``error`` edge target for *node*, or ``None``.

        The error is saved into ``on_error.save_error_as`` (default
        ``$_error``) so the recovery branch can inspect it.
        """
        node_id = str(node.get("id"))
        target = self.next_by_handle(node_id, "error")
        if target is None:
            return None
        spec = _parse_on_error(node.get("on_error")) if node.get("on_error") is not None else {}
        var = str(spec.get("save_error_as") or "_error")
        self._variables[var] = f"{type(exc).__name__}: {exc}"
        if self._logging:
            detail = redact_text(f"{type(exc).__name__}: {exc}", self._secrets)
            self._log("error", f"✗ node {node_id} → error edge (${var} = {detail})")
        return target


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
    variables = doc.get("variables")
    if variables is not None and not isinstance(variables, (dict, list)):
        problems.append("'variables' must be an object or a list")
    elif isinstance(variables, dict):
        for key in variables:
            if not _identifier_ok(key):
                problems.append(f"variable name {key!r} must be a plain identifier (no '$')")
    elif isinstance(variables, list):
        for item in variables:
            if not isinstance(item, dict):
                continue
            if not _identifier_ok(item.get("name")):
                problems.append(
                    f"variable name {item.get('name')!r} must be a plain identifier (no '$')"
                )
            vtype = item.get("type")
            if vtype is not None and vtype not in _VAR_TYPES:
                problems.append(f"variable {item.get('name')!r}: unknown type {vtype!r}")
    declared = _declared_types(doc)

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
        if kind not in ("start", "end", "tool", "set", "if", "loop", "flow", "fail"):
            problems.append(f"node {node_id!r}: unknown kind {kind!r}")
        if kind == "tool" and not node.get("tool"):
            problems.append(f"node {node_id!r}: tool node without a tool name")
        if kind == "fail":
            config = node.get("config") or {}
            if isinstance(config, dict):
                mode = config.get("mode")
                if mode is not None and mode not in ("business", "system"):
                    problems.append(
                        f"node {node_id!r}: fail mode must be business or system, got {mode!r}"
                    )
        if kind == "flow":
            config = node.get("config") or {}
            if (config.get("path") is None) == (config.get("doc") is None):
                problems.append(f"node {node_id!r}: flow node needs exactly one of 'path' or 'doc'")
            if not isinstance(config, dict):
                problems.append(f"node {node_id!r}: flow node 'config' must be an object")
            else:
                scope = config.get("scope")
                if scope is not None and scope not in ("shared", "isolated"):
                    problems.append(
                        f"node {node_id!r}: flow scope must be shared or isolated, got {scope!r}"
                    )
                inputs = config.get("inputs")
                if inputs is not None and not isinstance(inputs, dict):
                    problems.append(f"node {node_id!r}: flow 'inputs' must be an object")
                elif isinstance(inputs, dict):
                    for name in inputs:
                        if not _identifier_ok(name):
                            problems.append(
                                f"node {node_id!r}: input name {name!r} must be a plain identifier"
                            )
                outputs = config.get("outputs")
                if outputs is not None and not isinstance(outputs, (list, dict)):
                    problems.append(f"node {node_id!r}: flow 'outputs' must be a list or object")
                elif isinstance(outputs, dict):
                    for parent, child in outputs.items():
                        if not _identifier_ok(parent) or not _identifier_ok(child):
                            problems.append(
                                f"node {node_id!r}: output mapping {parent!r} -> {child!r} "
                                "must use plain identifiers"
                            )
                elif isinstance(outputs, list):
                    for name in outputs:
                        if not _identifier_ok(name):
                            problems.append(
                                f"node {node_id!r}: output name {name!r} must be a plain identifier"
                            )
        if kind == "set":
            config = node.get("config") or {}
            if not isinstance(config, dict) or not str(config.get("var") or ""):
                problems.append(f"node {node_id!r}: set node needs config.var")
            elif not _identifier_ok(config.get("var")):
                problems.append(
                    f"node {node_id!r}: set variable {config.get('var')!r} must be a plain "
                    "identifier (no '$')"
                )
        if kind == "if":
            condition = node.get("condition")
            if not isinstance(condition, dict) or not condition.get("var"):
                problems.append(f"node {node_id!r}: if node needs a condition with 'var'")
        if kind == "loop":
            loop = node.get("loop")
            if not isinstance(loop, dict) or not loop.get("mode"):
                problems.append(f"node {node_id!r}: loop node needs loop.mode")
            elif loop.get("mode") == "foreach":
                for field in ("var", "as"):
                    value = loop.get(field)
                    if value is not None and not _identifier_ok(value):
                        problems.append(
                            f"node {node_id!r}: loop {field} {value!r} must be a plain identifier"
                        )
        save_as = node.get("save_as")
        if save_as is not None and not _identifier_ok(save_as):
            problems.append(
                f"node {node_id!r}: save_as {save_as!r} must be a plain identifier (no '$')"
            )
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
                from smithcore.core.schema import validate_against_schema

                config = node.get("config") or {}
                if not _looks_interpolated(config):
                    for field_problem in validate_against_schema(tool_obj.schema(), config):
                        problems.append(f"node {node_id!r}: {name}: {field_problem}")
                if isinstance(config, dict):
                    problems.extend(
                        _ref_type_problems(node_id, name, tool_obj.schema(), config, declared)
                    )
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

                        store_path = os.environ.get("SMITHCORE_SELECTOR_STORE", "selectors.json")
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
        handle = edge.get("source_handle")
        if not handle:
            problems.append(f"edge {source!r}→{target!r} has no source_handle")
        elif isinstance(source, str) and source in nodes:
            allowed = _ALLOWED_HANDLES.get(str(nodes[source].get("kind") or ""))
            if allowed is not None and handle not in allowed:
                problems.append(
                    f"edge {source!r}→{target!r}: handle {handle!r} is not valid for a "
                    f"{nodes[source].get('kind')!r} node (allowed: {sorted(allowed)})"
                )
    return problems


def _looks_interpolated(config: Any) -> bool:
    """True when *config* contains ``$var``/``${asset:...}`` references.

    Interpolated values are only known at run time, so static schema
    validation would produce false positives (e.g. ``"$delay"`` for an
    integer field).
    """
    if isinstance(config, str):
        return bool(_VAR_RE.search(config)) or "${asset:" in config
    if isinstance(config, dict):
        return any(_looks_interpolated(value) for value in config.values())
    if isinstance(config, list):
        return any(_looks_interpolated(value) for value in config)
    return False
