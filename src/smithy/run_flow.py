"""Run a v2 flow document as a standalone program.

Usage:
  python -m smithy.run_flow flow.json [--set NAME=VALUE ...] [--vars FILE] [--payload FILE]
  python -m smithy.run_flow flow.json --tools my_tools.py
  python -m smithy.run_flow flow.json --validate
  python -m smithy.run_flow flow.json --transactional --queue NAME --db q.db
  python -m smithy.run_flow flow.json --transactional --queue NAME --cloud URL --agent ID

Modes:
  run (default)          execute the flow once; variables come from
                         --vars/--payload/--set (CLI --set wins)
  --validate             static checks only — nothing executes
  --transactional        REFramework loop over a queue: each work item's
                         payload becomes the flow's variables, the flow
                         runs once per item, results are stored back.
                         Queue backend: --db (local SQLite) or
                         --cloud/--agent (smithy-cloud, token from the
                         SMITHY_CLOUD_TOKEN env var)

Variable precedence: flow defaults < --payload < --vars < --set.

Exit codes: 0 — finished; 1 — validation or node failure; 2 — stopped
(SIGTERM/SIGINT/Ctrl+C), so a supervising service can distinguish a
crash from a requested stop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import importlib.util
import json
import os
import signal
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from smithy.core.selectors import SelectorStore
from smithy.flow import FlowError, FlowRunner, jsonable, parse_typed_value, validate_document

if TYPE_CHECKING:
    from smithy.core.registry import ToolRegistry

_EXIT_FINISHED = 0
_EXIT_FAILED = 1
_EXIT_STOPPED = 2


def _load_registry() -> ToolRegistry:
    """Windows tools when available; empty registry keeps any flow runnable."""
    from smithy.core.registry import ToolRegistry

    registry = ToolRegistry()
    try:
        from smithy.windows.tools import windows_tools

        for tool in windows_tools():
            registry.register(tool)
    except ImportError:
        pass
    return registry


def _print_log(level: str, msg: str) -> None:
    print(f"[{level}] {msg}", flush=True)


def _load_doc(path: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return document


def _load_vars_file(path: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit(f"{path}: variables file must hold a JSON object")
    return document


def _load_tools(spec: str) -> list[Any]:
    """Import *spec* (a ``.py`` path or module name) and collect tools.

    Convention: the module may define ``TOOLS = [...]``; otherwise every
    module-level ``AbstractTool`` instance is registered.
    """
    from smithy.core.tool import AbstractTool

    if spec.endswith(".py"):
        module_spec = importlib.util.spec_from_file_location("smithy_user_tools", spec)
        if module_spec is None or module_spec.loader is None:
            raise SystemExit(f"cannot import tools module: {spec}")
        module: Any = importlib.util.module_from_spec(module_spec)
        sys.modules["smithy_user_tools"] = module
        module_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(spec)

    declared = getattr(module, "TOOLS", None)
    if declared is not None:
        items = list(declared)
    else:
        items = [v for v in vars(module).values() if isinstance(v, AbstractTool)]
    if not items:
        raise SystemExit(f"{spec}: no tools found (define TOOLS = [...] or tool instances)")
    return items


def _build_queue(args: argparse.Namespace) -> Any:
    from smithy.core.queue import SqliteQueue

    if args.db:
        return SqliteQueue(args.db)
    if not args.cloud or not args.agent:
        message = (
            "--transactional needs a queue backend: --db PATH (local) "
            "or --cloud URL --agent ID (smithy-cloud)"
        )
        raise SystemExit(message)
    from smithy.core.http_queue import HttpQueue

    token = os.environ.get(args.token_env) or None
    if not token:
        raise SystemExit(f"queue token is empty: set env {args.token_env!r}")
    return HttpQueue(
        args.cloud,
        agent_id=args.agent,
        token=token,
        allow_insecure=args.insecure,
    )


def _parse_sets(items: list[str], parser: argparse.ArgumentParser) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep:
            parser.error(f"--set expects NAME=VALUE, got {item!r}")
        variables[name.strip()] = parse_typed_value(value, "auto")
    return variables


def _install_stop_handler(task: asyncio.Task[Any]) -> None:
    def _request_stop() -> None:
        task.cancel()

    for sig in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, sig, None)
        if handler is not None:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(handler, lambda *_: _request_stop())


async def _run_once(runner: FlowRunner, doc: dict[str, Any]) -> str:
    task = asyncio.create_task(runner.run(doc))
    _install_stop_handler(task)
    try:
        return await task
    except asyncio.CancelledError:
        print("run stopped (terminated)", file=sys.stderr)
        raise


async def _run_transactional(
    doc: dict[str, Any],
    registry: ToolRegistry,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """REFramework loop: each work item's payload drives one flow run."""
    from smithy.core.transactions import run_transactions_async

    queue = _build_queue(args)

    async def process_fn(item: Any) -> dict[str, Any]:
        variables: dict[str, Any] = dict(item.payload)
        runner = FlowRunner(registry, variables=variables, log=_print_log)
        await runner.run(doc)
        snapshot: dict[str, Any] = jsonable(variables)
        return snapshot

    report = await run_transactions_async(
        queue,
        process_fn,
        queue_name=args.queue,
        run_id=args.run_id or f"flow-{uuid.uuid4().hex[:8]}",
        lease_seconds=args.lease_seconds,
    )
    _print_log(
        "info",
        f"transactions done: {report.processed} "
        f"(ok={report.succeeded}, business={report.business_failed}, "
        f"system={report.system_failed}) — {report.stop_reason}",
    )
    summary: dict[str, Any] = {
        "processed": report.processed,
        "succeeded": report.succeeded,
        "business_failed": report.business_failed,
        "system_failed": report.system_failed,
        "stop_reason": report.stop_reason,
    }
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smithy.run_flow", description=__doc__)
    parser.add_argument(
        "flow",
        nargs="?",
        default=None,
        help="path to a v2 flow document (JSON), or omitted with --pack",
    )
    parser.add_argument(
        "--pack",
        default=None,
        metavar="DIR",
        help="verify the pack manifest in DIR and run a stage from it "
        "(requires --stage; tools.py and selectors.json are picked up from the pack)",
    )
    parser.add_argument(
        "--stage",
        default=None,
        metavar="NAME",
        help="pack entry to run: init / process / end (see pack.json)",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="pre-set a variable before the run (auto-typed, repeatable)",
    )
    parser.add_argument("--vars", default=None, metavar="FILE", help="JSON object of variables")
    parser.add_argument(
        "--payload", default=None, metavar="FILE", help="work-item payload as variables"
    )
    parser.add_argument(
        "--tools",
        action="append",
        default=[],
        metavar="MODULE_OR_PY",
        help="custom tools module (.py with TOOLS = [...] or tool instances)",
    )
    parser.add_argument(
        "--validate", action="store_true", help="check the document without executing"
    )
    parser.add_argument(
        "--capture", action="store_true", help="record missing/stale selector keys (dev mode)"
    )
    parser.add_argument(
        "--transactional",
        action="store_true",
        help="run the flow once per work item (REFramework loop)",
    )
    parser.add_argument("--queue", default=None, help="queue name (transactional mode)")
    parser.add_argument("--db", default=None, help="local SQLite queue file (transactional)")
    parser.add_argument("--cloud", default=None, help="smithy-cloud API base URL")
    parser.add_argument("--agent", default=None, help="agent id for the cloud queue")
    parser.add_argument(
        "--token-env", default="SMITHY_CLOUD_TOKEN", help="env var holding the queue token"
    )
    parser.add_argument("--insecure", action="store_true", help="allow plain-http cloud URL")
    parser.add_argument("--run-id", default=None, help="transactional run id (default: generated)")
    parser.add_argument("--lease-seconds", type=int, default=300, help="claim lease seconds")
    args = parser.parse_args(argv)

    flow_path: Path
    pack_dir: Path | None = None
    if args.pack:
        from smithy.pack import load_manifest

        pack_dir = Path(args.pack)
        try:
            manifest = load_manifest(pack_dir)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return _EXIT_FAILED
        if not args.stage:
            parser.error("--pack requires --stage NAME (init/process/end/…)")
        entry = manifest.get("entry", {}).get(args.stage)
        if not entry:
            parser.error(
                f"pack {args.pack!r} has no entry {args.stage!r} "
                f"(has: {', '.join(manifest.get('entry', {}) or {}) or 'none'})"
            )
        flow_path = pack_dir / entry
    else:
        if not args.flow:
            parser.error("provide a flow file or --pack DIR --stage NAME")
        flow_path = Path(args.flow)

    try:
        doc = _load_doc(str(flow_path))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read flow file: {exc}", file=sys.stderr)
        return _EXIT_FAILED

    registry = _load_registry()
    for tools_spec in args.tools:
        try:
            for tool_instance in _load_tools(tools_spec):
                registry.register(tool_instance)
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            return _EXIT_FAILED
    if pack_dir is not None and (pack_dir / "tools.py").is_file():
        try:
            for tool_instance in _load_tools(str(pack_dir / "tools.py")):
                registry.register(tool_instance)
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            return _EXIT_FAILED

    if args.validate:
        import os

        store_path = os.environ.get("SMITHY_SELECTOR_STORE", "selectors.json")
        store = SelectorStore(store_path) if Path(store_path).exists() else None
        problems = validate_document(doc, registry=registry, selector_store=store)
        if problems:
            for problem in problems:
                print(f"problem: {problem}", file=sys.stderr)
            return _EXIT_FAILED
        print("validation passed: document is runnable")
        return _EXIT_FINISHED

    variables: dict[str, Any] = {}
    for source in (args.payload, args.vars):
        if source is not None:
            try:
                variables.update(_load_vars_file(source))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"cannot read variables file: {exc}", file=sys.stderr)
                return _EXIT_FAILED
    variables.update(_parse_sets(args.set, parser))

    selector_store_path: SelectorStore | None = None
    if pack_dir is not None and (pack_dir / "selectors.json").is_file():
        selector_store_path = SelectorStore(pack_dir / "selectors.json")
    runner = FlowRunner(
        registry,
        variables=variables,
        log=_print_log,
        dev_capture=True if args.capture else None,
        selector_store=selector_store_path,
    )

    try:
        if args.transactional:
            if not args.queue:
                parser.error("--transactional requires --queue NAME")
            asyncio.run(_run_transactional(doc, registry, args))
        else:
            asyncio.run(_run_once(runner, doc))
    except FlowError as exc:
        print(f"flow failed: {exc}", file=sys.stderr)
        return _EXIT_FAILED
    except (KeyboardInterrupt, asyncio.CancelledError):
        return _EXIT_STOPPED
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return _EXIT_FAILED
    return _EXIT_FINISHED


if __name__ == "__main__":
    sys.exit(main())
