"""ProcessTool — manage Windows processes (start, stop)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath
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

    A path-qualified command is accepted only when it resolves to the
    same file that a bare-name ``PATH`` lookup of its basename finds —
    otherwise ``C:\\temp\\notepad.exe`` could run an attacker binary that
    merely shares a basename with an allowlisted command.

    Raises:
        InvalidInput: If the name is not found on ``PATH``, or a
            path-qualified command is not the on-``PATH`` executable.
    """
    if "\\" in command or "/" in command:
        candidate = Path(command)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not candidate.is_file():
            raise InvalidInput(
                f"Command '{command}' does not exist",
                param="command",
                input_value=command,
            )
        try:
            on_path = _resolve_command_path(candidate.name)
        except InvalidInput as exc:
            raise InvalidInput(
                f"Command '{command}' is not available on PATH (refusing a "
                "path-qualified executable that PATH does not expose)",
                param="command",
                input_value=command,
            ) from exc
        if candidate.resolve() != Path(on_path).resolve():
            raise InvalidInput(
                f"Command '{command}' is not the on-PATH '{candidate.name}' "
                "(refusing a possibly planted executable)",
                param="command",
                input_value=command,
            )
        return str(candidate.resolve())
    pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
    exts = [""] if any(command.lower().endswith(ext.lower()) for ext in pathext) else pathext
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for ext in exts:
            candidate_path = os.path.join(directory, command + ext)
            if os.path.isfile(candidate_path):
                return candidate_path
    raise InvalidInput(
        f"Command '{command}' not found on PATH",
        param="command",
        input_value=command,
    )


def _system32(*parts: str) -> str:
    """Absolute path under ``%SystemRoot%\\System32`` (no PATH lookups)."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.path.join(root, "System32", *parts)


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
                "action": {
                    "type": "string",
                    "enum": ["start", "stop", "wait", "status"],
                },
                "command": {
                    "type": "string",
                    "description": "Executable path",
                },
                "args": {"type": "array", "items": {"type": "string"}},
                "working_dir": {"type": "string"},
                "pid": {
                    "type": "integer",
                    "description": "Process ID to stop, wait for, or query",
                },
                "name": {"type": "string", "description": "Process image name to stop"},
                "timeout_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 30000,
                    "description": "Max wait for the wait action",
                },
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
                return await _action_stop(config, self._allowed)
            if action == "wait":
                return await _action_wait(config)
            if action == "status":
                return await _action_status(config)
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


async def _action_stop(config: dict[str, Any], allowed: frozenset[str]) -> dict[str, Any]:
    """Stop a process by PID or name.

    Stopping by image *name* is restricted to the configured allowlist —
    otherwise any flow could terminate arbitrary processes (e.g. system
    services). Stop by PID is not name-restricted (the caller already has
    a handle/pid it obtained itself).
    """
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
    if name is not None and not _is_command_allowed(name, allowed):
        raise InvalidInput(
            f"Command '{name}' is not in the allowed list",
            param="name",
            input_value=name,
        )

    if pid is not None:

        def _stop_by_pid() -> None:
            image = _query_image_name(pid)
            if not _is_command_allowed(image, allowed):
                raise InvalidInput(
                    f"Refusing to stop pid {pid} ({image!r}): its executable is not "
                    "in the allowed list",
                    param="pid",
                    input_value=pid,
                )
            result = subprocess.run(
                [_system32("taskkill.exe"), "/F", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=30,
                # The agent forces PYTHONUTF8=1, but taskkill writes the
                # console OEM codepage; strict UTF-8 decoding would raise in
                # the reader thread. Replace is enough — the text is only
                # surfaced in the error message.
                encoding="utf-8",
                errors="replace",
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
            [_system32("taskkill.exe"), "/F", "/IM", name],
            capture_output=True,
            text=True,
            timeout=30,
            # Same OEM-codepage caveat as _stop_by_pid above.
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise PlatformError(
                f"taskkill for {name} failed: {result.stderr}",
            )

    await run_blocking(_stop_by_name)
    return {"status": "stopped", "method": "name", "name": name}


def _query_image_name(pid: int) -> str:
    """Return the executable basename of *pid* (runs in an executor).

    Raises:
        PlatformError: If the process is gone or access is denied.
    """
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD,
        ctypes.wintypes.BOOL,
        ctypes.wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise PlatformError(f"Cannot inspect pid {pid} (it may not exist or access is denied)")
    try:
        size = ctypes.wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise PlatformError(f"Cannot read the image name of pid {pid}")
        return PureWindowsPath(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def _check_pid(config: dict[str, Any]) -> int:
    pid = config.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise InvalidInput(
            "Missing or invalid 'pid': expected an integer",
            param="pid",
            input_value=pid,
        )
    return pid


def _check_timeout_ms(config: dict[str, Any]) -> int:
    timeout_ms = config.get("timeout_ms", 30000)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
        raise InvalidInput(
            "Invalid 'timeout_ms': expected an integer >= 1",
            param="timeout_ms",
            input_value=timeout_ms,
        )
    return timeout_ms


_STILL_ACTIVE = 259
_SYNCHRONIZE = 0x00100000
_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_ERROR_ACCESS_DENIED = 5


async def _action_wait(config: dict[str, Any]) -> dict[str, Any]:
    """Wait for a process to exit and report its exit code."""
    pid = _check_pid(config)
    timeout_ms = _check_timeout_ms(config)
    outcome = await run_blocking(_wait_for_exit, pid, timeout_ms)
    if outcome is None:
        return {"status": "timeout", "pid": pid}
    return {"status": "exited", "pid": pid, "exit_code": outcome}


def _wait_for_exit(pid: int, timeout_ms: int) -> int | None:
    """Wait for *pid* to exit (runs in an executor).

    Returns the exit code, or ``None`` on timeout.
    """
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        raise PlatformError(
            f"Cannot open process {pid} for waiting (it may not exist or access is denied)"
        )
    try:
        wait_result = kernel32.WaitForSingleObject(handle, ctypes.c_uint(timeout_ms))
        if wait_result == _WAIT_TIMEOUT:
            return None
        if wait_result != _WAIT_OBJECT_0:
            raise PlatformError(f"WaitForSingleObject failed for pid {pid}: {wait_result}")
        exit_code = ctypes.wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise PlatformError(f"GetExitCodeProcess failed for pid {pid}")
        return int(exit_code.value)
    finally:
        kernel32.CloseHandle(handle)


async def _action_status(config: dict[str, Any]) -> dict[str, Any]:
    """Query whether a process is running (and its exit code when done)."""
    pid = _check_pid(config)
    running, exit_code = await run_blocking(_query_status, pid)
    return {"pid": pid, "running": running, "exit_code": exit_code}


def _query_status(pid: int) -> tuple[bool, int | None]:
    """Read process liveness (runs in an executor).

    Raises:
        PlatformError: If the process exists but access is denied
            (distinguishable from "not running").
    """
    import ctypes
    import ctypes.wintypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        last_error = getattr(kernel32, "GetLastError", lambda: 0)()
        if last_error == _ERROR_ACCESS_DENIED:
            raise PlatformError(f"Access denied opening pid {pid} (elevated/protected)")
        return False, None
    try:
        exit_code = ctypes.wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False, None
        code = int(exit_code.value)
        return (code == _STILL_ACTIVE), (None if code == _STILL_ACTIVE else code)
    finally:
        kernel32.CloseHandle(handle)
