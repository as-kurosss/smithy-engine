"""Run a v2 flow document as a standalone program.

Usage:  python -m smithy.run_flow flow.json [--set NAME=VALUE ...]

Exit codes: 0 — the flow finished; 1 — validation or node failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from smithy.flow import FlowError, FlowRunner, parse_typed_value

if TYPE_CHECKING:
    from smithy.core.registry import ToolRegistry


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="smithy.run_flow", description=__doc__)
    parser.add_argument("flow", help="path to a v2 flow document (JSON)")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="pre-set a variable before the run (auto-typed, repeatable)",
    )
    args = parser.parse_args(argv)

    try:
        doc = json.loads(Path(args.flow).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read flow file: {exc}", file=sys.stderr)
        return 1

    variables: dict[str, object] = {}
    for item in args.set:
        name, sep, value = item.partition("=")
        if not sep:
            parser.error(f"--set expects NAME=VALUE, got {item!r}")
        variables[name.strip()] = parse_typed_value(value, "auto")

    runner = FlowRunner(_load_registry(), variables=variables, log=_print_log)
    try:
        asyncio.run(runner.run(doc))
    except FlowError as exc:
        print(f"flow failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
