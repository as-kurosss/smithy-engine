"""ProcessTool — manage Windows processes (start, stop)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import PureWindowsPath
from typing import Any

from smithy.core.blocking import run_blocking
from smithy.core.errors import InvalidInput, PlatformError
from smithy.core.tool import AbstractTool

#: Env var with a comma-separated allowlist override, e.g.
#: ``SMITHY_ALLOWED_COMMANDS="notepad.exe,calc.exe"``. An explicitly empty
#: value denies everything.
ENV_ALLOWLIST_VAR = "SMITHY_ALLOWED_COMMANDS"

# Default allowlist of executables (case-insensitive).
# cmd.exe, powershell.exe and explorer.exe are intentionally excluded:
# explorer.exe accepts arbitrary launch targets as arguments, which turns
# it into an arbitrary-code-execution vector.
_DEFAULT_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "notepad.exe",
        "calc.exe",
        "mspaint.exe",
        "write.exe",
        "wordpad.exe",
    }
)


def _normalize_entries(entries: Iterable[str]) -> frozenset[str]:
    """Normalize allowlist entries to lowercase basenames (Windows semantics)."""
    # PureWindowsPath: treat backslash paths consistently on every host OS —
    # the allowlist always targets Windows commands, even when the bot config
    # was authored on Linux/macOS.
    return frozenset(
        PureWindowsPath(entry.strip()).name.lower() for entry in entries if entry.strip()
    )


def _default_allowed_commands() -> frozenset[str]:
    """Default allowlist, overridden by ``SMITHY_ALLOWED_COMMANDS`` when set."""
    raw = os.environ.get(ENV_ALLOWLIST_VAR)
    if raw is None:
        return _DEFAULT_ALLOWED_COMMANDS
    return _normalize_entries(raw.split(","))


def _is_command_allowed(cmd: str, allowed: frozenset[str]) -> bool:
    """Check if the executable is in the allowlist (Windows path semantics)."""
    return PureWindowsPath(cmd.strip()).name.lower() in allowed


def _resolve_command_path(command: str) -> str:
    """Resolve a bare command name to an absolute path via ``PATH``.

    ``CreateProcess`` searches the current directory before ``PATH`` for
    bare names, so a planted ``notepad.exe`` in the working directory
    would shadow the system one. Resolving explicitly (and refusing when
    the name is not on ``PATH``) closes that hole.

    Raises:
        InvalidInput: If the bare name is not found on ``PATH``.
    """
    if "\\" in command or "/" in command:
        return command
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
    exts = [""] if any(command.lower().endswith(ext.lower()) for ext in pathext) else pathext
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for ext in exts:
            candidate = os.path.join(directory, command + ext)
            if os.path.isfile(candidate):
                return candidate
    raise InvalidInput(
        f"Command '{command}' not found on PATH",
        param="command",
        input_value=command,
    )


class ProcessTool(AbstractTool):
    """Manage Windows processes: start or stop.

    Args:
        allowed_commands: Executables this instance may start. Defaults to
            the built-in demo list, overridden by the
            ``SMITHY_ALLOWED_COMMANDS`` env var (comma-separated) when set.
    """

    def __init__(self, allowed_commands: Iterable[str] | None = None) -> None:
        if allowed_commands is None:
            self._allowed = _default_allowed_commands()
        else:
            self._allowed = _normalize_entries(allowed_commands)

    @property
    def allowed_commands(self) -> frozenset[str]:
        """Executables this instance may start (lowercase basenames)."""
        return self._allowed

    @property
    def name(self) -> str:
        return "windows.process"

    @property
    def description(self) -> str:
        return "Manages Windows processes: start or stop"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "stop"]},
                "command": {
                    "type": "string",
                    "description": "Executable path",
                },
                "args": {"type": "array", "items": {"type": "string"}},
                "working_dir": {"type": "string"},
                "pid": {"type": "integer", "description": "Process ID to stop"},
                "name": {"type": "string", "description": "Process image name to stop"},
            },
            "required": ["action"],
        }

    async def execute(
        self,
        config: dict[str, Any],
    ) -> Any:
        raw_action = config.get("action", "")
        if not isinstance(raw_action, str):
            raise InvalidInput(
                "Invalid 'action': expected a string",
                param="action",
                input_value=raw_action,
            )
        action = raw_action.lower()

        try:
            if action == "start":
                return await _action_start(config, self._allowed)
            if action == "stop":
                return await _action_stop(config)
        except (InvalidInput, PlatformError):
            raise
        except Exception as exc:
            raise PlatformError(
                f"Process action '{action}' failed",
                source=exc,
            ) from exc

        raise InvalidInput(
            f"Unknown process action: {action}",
            param="action",
            input_value=action,
        )


async def _action_start(
    config: dict[str, Any],
    allowed: frozenset[str],
) -> dict[str, Any]:
    """Start a new process."""
    command = config.get("command")
    if not isinstance(command, str) or not command:
        raise InvalidInput(
            "Missing 'command' for start action",
            param="command",
            input_value=command,
        )

    if not _is_command_allowed(command, allowed):
        raise InvalidInput(
            f"Command '{command}' is not in the allowed list",
            param="command",
            input_value=command,
        )

    args = config.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise InvalidInput(
            "Invalid 'args': expected a list of strings",
            param="args",
            input_value=args,
        )
    working_dir = config.get("working_dir")
    if working_dir is not None and not isinstance(working_dir, str):
        raise InvalidInput(
            "Invalid 'working_dir': expected a string",
            param="working_dir",
            input_value=working_dir,
        )

    def _start() -> int:
        resolved = _resolve_command_path(command)
        proc = subprocess.Popen(
            [resolved, *args],
            cwd=working_dir,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        return proc.pid

    pid = await run_blocking(_start)
    return {"status": "started", "pid": pid}


async def _action_stop(config: dict[str, Any]) -> dict[str, Any]:
    """Stop a process by PID or name."""
    pid = config.get("pid")
    name = config.get("name")

    if pid is None and name is None:
        raise InvalidInput(
            "Must provide 'pid' or 'name' for stop action",
            param="pid",
        )
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int)):
        raise InvalidInput(
            "Invalid 'pid': expected an integer",
            param="pid",
            input_value=pid,
        )
    if name is not None and (not isinstance(name, str) or not name):
        raise InvalidInput(
            "Invalid 'name': expected a non-empty string",
            param="name",
            input_value=name,
        )

    if pid is not None:

        def _stop_by_pid() -> None:
            result = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise PlatformError(
                    f"taskkill for pid {pid} failed: {result.stderr}",
                )

        await run_blocking(_stop_by_pid)
        return {"status": "stopped", "method": "pid", "pid": pid}

    if name is None:  # narrowed by the pid-branch above; explicit, not assert
        raise InvalidInput(
            "Must provide 'pid' or 'name' for stop action",
            param="name",
        )

    def _stop_by_name() -> None:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise PlatformError(
                f"taskkill for {name} failed: {result.stderr}",
            )

    await run_blocking(_stop_by_name)
    return {"status": "stopped", "method": "name", "name": name}
