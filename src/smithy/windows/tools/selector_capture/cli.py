"""CLI entry point for selector-capture.

Usage::

    python -m smithy.windows.tools.selector_capture single -o selectors.json
    python -m smithy.windows.tools.selector_capture series -o recording.json
    python -m smithy.windows.tools.selector_capture record -o flow.json
    python -m smithy.windows.tools.selector_capture emit -i flow.json -o bot.py
"""

from __future__ import annotations

import argparse
import logging
import sys

from smithy.windows.tools.selector_capture.emit import emit_file
from smithy.windows.tools.selector_capture.generate import ToolType
from smithy.windows.tools.selector_capture.recorder import (
    _copy_to_clipboard,
    run_record_mode,
    run_series_mode,
    run_single_mode,
)

logger = logging.getLogger(__name__)


def _maybe_emit(capture_path: str, bot_path: str | None) -> None:
    """Write the generated bot script unless recording was cancelled."""
    if bot_path is None:
        return
    try:
        emit_file(capture_path, bot_path)
    except FileNotFoundError:
        logger.warning("Nothing captured — skipping codegen for %s", bot_path)
        return
    logger.info("Generated bot: %s", bot_path)


def main(argv: list[str] | None = None) -> None:
    """Parse CLI arguments and dispatch to the appropriate capture mode."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="selector-capture",
        description="Capture UI element selectors and generate flow configs",
    )
    parser.add_argument(
        "-B",
        "--backend",
        default="windows",
        help="Platform backend (default: windows)",
    )

    sub = parser.add_subparsers(dest="command", help="Capture mode")

    # ── single ──
    p_single = sub.add_parser("single", help="Capture a single element and exit")
    p_single.add_argument("-o", "--output", default="selectors.json", help="Output JSON file")
    p_single.add_argument("-d", "--description", help="Description for the capture")
    p_single.add_argument(
        "-t",
        "--tool",
        choices=[t.value for t in ToolType],
        default="click",
        help="Tool type for config generation (default: click)",
    )
    p_single.add_argument("--text", default="", help="Text for input_text / set_text")
    p_single.add_argument(
        "--duration-ms",
        type=int,
        default=1000,
        help="Duration for wait tool (ms)",
    )
    p_single.add_argument("--clip", action="store_true", help="Copy config to clipboard")
    p_single.add_argument(
        "--emit",
        default=None,
        metavar="BOT.py",
        help="Also write the generated bot script to BOT.py",
    )

    # ── series ──
    p_series = sub.add_parser(
        "series",
        help="Record all clicks & inputs; Ctrl+Shift+F2 to stop",
    )
    p_series.add_argument("-o", "--output", default="recording.json", help="Output JSON file")
    p_series.add_argument(
        "--emit",
        default=None,
        metavar="BOT.py",
        help="Also write the generated bot script to BOT.py",
    )

    # ── record ──
    p_record = sub.add_parser(
        "record",
        help="Interactive: CTRL to capture, prompts for tool/params",
    )
    p_record.add_argument("-o", "--output", default="flow.json", help="Output JSON file")
    p_record.add_argument(
        "--emit",
        default=None,
        metavar="BOT.py",
        help="Also write the generated bot script to BOT.py",
    )

    # ── emit ──
    p_emit = sub.add_parser(
        "emit",
        help="Render a capture JSON file as a replayable bot script",
    )
    p_emit.add_argument("-i", "--input", default="flow.json", help="Input capture JSON file")
    p_emit.add_argument("-o", "--output", default="bot.py", help="Output bot script")
    p_emit.add_argument("--clip", action="store_true", help="Copy script to clipboard")

    args = parser.parse_args(argv)

    if args.backend != "windows":
        parser.error(f"unsupported backend {args.backend!r}: only 'windows' is available")

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "single":
        tool_type = ToolType(args.tool)
        run_single_mode(
            output=args.output,
            description=args.description,
            tool_type=tool_type,
            text=args.text,
            duration_ms=args.duration_ms,
            clip=args.clip,
        )
        _maybe_emit(args.output, args.emit)
    elif args.command == "series":
        run_series_mode(output=args.output)
        _maybe_emit(args.output, args.emit)
    elif args.command == "record":
        run_record_mode(output=args.output)
        _maybe_emit(args.output, args.emit)
    elif args.command == "emit":
        source = emit_file(args.input, args.output)
        logger.info("Generated bot: %s", args.output)
        if args.clip and _copy_to_clipboard(source):
            logger.info("Copied to clipboard")
