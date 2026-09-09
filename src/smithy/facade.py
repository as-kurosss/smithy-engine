"""Smithy — Facade for creating RPA bots with simple API."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from smithy.core.assets import AssetProvider, EnvAssetProvider
from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.core.events import EventBus, Middleware, ToolEvent
from smithy.core.registry import ToolRegistry
from smithy.core.selectors import SelectorStore
from smithy.core.tool import Tool


class _SupportsPid(Protocol):
    pid: int


@dataclass(frozen=True)
class ProcessHandle:
    """Handle for a launched process. Contains PID for filtering UIA elements."""

    pid: int
    name: str


@dataclass(frozen=True)
class ClickResult:
    """Result of a click operation."""

    status: str


@dataclass(frozen=True)
class InputTextResult:
    """Result of an input_text operation."""

    status: str


@dataclass(frozen=True)
class SetTextResult:
    """Result of a set_text operation."""

    status: str


class Smithy:
    """Main SDK class for creating RPA bots.

    Usage::

        bot = Smithy(tools=[ClickTool()])
        app = await bot.process_run("notepad.exe")
        await bot.click(app, name="File")
        await bot.process_stop(app)
    """

    def __init__(
        self,
        *,
        tools: list[Tool] | None = None,
        assets: AssetProvider | None = None,
        selector_store: str | Path | None = None,
        dev_capture: bool | None = None,
        trace: str | Path | None = None,
    ) -> None:
        self._registry = ToolRegistry()
        self._event_bus = EventBus()
        self._assets: AssetProvider = assets if assets is not None else EnvAssetProvider()
        self._selector_store_path = selector_store
        self._selector_store: SelectorStore | None = None
        self._active_key: str | None = None
        if dev_capture is None:
            dev_capture = os.environ.get("SMITHY_DEV_CAPTURE", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )
        self._dev_capture = dev_capture
        if trace is not None:
            from smithy.trace import FlowTracer

            self._event_bus.add_middleware(FlowTracer(trace))
        if tools:
            for t in tools:
                self._registry.register(t)

    def asset(self, name: str) -> str:
        """Fetch a runtime secret by reference name.

        Values come from the configured :class:`AssetProvider` (by
        default ``SMITHY_ASSET_*`` environment variables) and are
        returned to bot code only — they never pass through tool
        configs or results, so they cannot leak into the audit log.

        Args:
            name: Asset reference (e.g. ``"db.password"``).

        Returns:
            The secret value.
        """
        return self._assets.get(name)

    def register(self, tool: Tool) -> None:
        """Register a tool for use by this bot."""
        self._registry.register(tool)

    def add_middleware(self, middleware: Middleware) -> None:
        """Add a middleware to the event pipeline.

        Args:
            middleware: An async callable that receives a ToolEvent
                and returns a ToolEvent or None to stop propagation.
        """
        self._event_bus.add_middleware(middleware)

    async def _execute(self, tool_name: str, config: dict[str, Any]) -> Any:
        """Execute a tool, capture timing, and emit event through middleware."""
        start = time.perf_counter()
        error: Exception | None = None
        result: Any = None
        try:
            result = await self._registry.execute(tool_name, config)
        except Exception as exc:
            error = exc
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            metadata: dict[str, Any] = {}
            if self._active_key is not None:
                metadata["selector_key"] = self._active_key
                self._active_key = None
            event = ToolEvent(
                tool_name=tool_name,
                config=config,
                result=result,
                error=error,
                duration_ms=elapsed_ms,
                metadata=metadata,
            )
            await self._event_bus.emit(event)
        return result

    def _store(self) -> SelectorStore:
        """Lazily create the selector store (default ``selectors.json``)."""
        if self._selector_store is None:
            self._selector_store = SelectorStore(self._selector_store_path or "selectors.json")
        return self._selector_store

    async def _keyed_config(self, key: str | None, base: dict[str, Any]) -> dict[str, Any]:
        """Fill selector fields for *key* into *base*.

        A stored selector is merged with setdefault semantics — explicit
        fields always win. A missing key in dev mode triggers an
        interactive capture; in production it is a hard error.
        """
        if key is None:
            return base
        entry = self._store().get(key)
        if entry is not None:
            for field_name, value in entry.items():
                base.setdefault(field_name, value)
            self._active_key = key
            return base
        if not self._dev_capture:
            raise InvalidInput(
                f"No selector stored for key {key!r} in {self._store().path} — enable "
                "dev capture (SMITHY_DEV_CAPTURE=1) to record it, or pass "
                "selector fields explicitly"
            )
        from smithy.windows.tools.selector_capture import capture_once_async

        captured = await capture_once_async()
        self._store().put(key, captured.selector)
        base.update(captured.selector)
        self._active_key = key
        return base

    async def _execute_keyed(self, tool_name: str, config: dict[str, Any], key: str | None) -> Any:
        """Execute a keyed selector tool; re-capture on a stale selector.

        In dev mode, ``ElementNotFound`` (the stored selector no longer
        matches the UI) prompts a fresh capture, persists it, and retries
        once. Production runs never re-capture — a stale selector fails
        honestly.
        """
        try:
            return await self._execute(tool_name, config)
        except ElementNotFound:
            if key is None or not self._dev_capture:
                raise
            from smithy.windows.tools.selector_capture import capture_once_async

            captured = await capture_once_async()
            self._store().put(key, captured.selector)
            for field_name in ("name", "automation_id", "control_type", "class_name"):
                config.pop(field_name, None)
            config.update(captured.selector)
            self._active_key = key
            return await self._execute(tool_name, config)

    async def process_run(self, command: str, **kwargs: Any) -> ProcessHandle:
        """Launch a process and return a handle with PID.

        Args:
            command: Executable path or name (e.g. "notepad.exe").
            **kwargs: Additional parameters passed to the process tool.

        Returns:
            ProcessHandle with pid and name for filtering UIA elements.
        """
        result = await self._execute(
            "windows.process", {"action": "start", "command": command, **kwargs}
        )
        return ProcessHandle(pid=result["pid"], name=command)

    async def process_stop(
        self,
        handle: ProcessHandle | None = None,
        *,
        pid: int | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Stop a running process.

        Provide *handle*, *pid*, or *name* to identify the process.

        Args:
            handle: ProcessHandle to stop.
            pid: Process ID to stop.
            name: Process image name to stop (e.g. "notepad.exe").

        Returns:
            Dict with "status" key.
        """
        result: dict[str, Any]
        if handle is not None:
            result = await self._execute("windows.process", {"action": "stop", "pid": handle.pid})
        elif pid is not None:
            result = await self._execute("windows.process", {"action": "stop", "pid": pid})
        elif name is not None:
            result = await self._execute("windows.process", {"action": "stop", "name": name})
        else:
            raise ValueError("Provide handle, pid, or name")
        return result

    async def click(
        self,
        handle: _SupportsPid | None = None,
        *,
        button: str = "left",
        clicks: int = 1,
        x: int | None = None,
        y: int | None = None,
        key: str | None = None,
        **kwargs: Any,
    ) -> ClickResult:
        """Click a UI element or screen coordinates.

        When *handle* is provided, the PID is forwarded to the click tool
        so it can narrow the UIA search scope automatically.
        Coordinates win over selector fields when both are given.

        Args:
            handle: ProcessHandle to scope element search by PID.
            button: Mouse button — ``"left"`` (default) or ``"right"``.
            clicks: Click count — ``1`` (default) or ``2`` (double-click).
            x: Screen X coordinate (with *y* clicks a raw point).
            y: Screen Y coordinate (with *x* clicks a raw point).
            key: Selector-store key (dev-capture workflow).
            **kwargs: Selector fields (name, automation_id, etc.) or
                "element" key for a pre-resolved element.

        Returns:
            ClickResult.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, {"button": button, "clicks": clicks, **kwargs})
        if x is not None:
            config["x"] = x
        if y is not None:
            config["y"] = y
        result = await self._execute_keyed("windows.click", config, key)
        return ClickResult(status=result.get("status", "clicked"))

    async def wait(
        self,
        handle: _SupportsPid | None = None,
        *,
        name: str | None = None,
        automation_id: str | None = None,
        control_type: str | None = None,
        class_name: str | None = None,
        pid: int | None = None,
        timeout_ms: int = 10000,
        interval_ms: int = 500,
        wait_for: str = "appear",
        key: str | None = None,
    ) -> bool:
        """Wait for a UI element to appear or disappear.

        Polls the desktop for a matching element at *interval_ms*
        until the condition holds or *timeout_ms* elapsed.

        Args:
            handle: ProcessHandle to scope the search by PID.
            name: Element name to match (supports wildcards: * and ?).
            automation_id: UI Automation identifier.
            control_type: Control type (e.g. Button, Edit, Window).
            class_name: Window class name.
            pid: Process ID filter.
            timeout_ms: Maximum wait time in milliseconds.
            interval_ms: Polling interval in milliseconds.
            wait_for: ``"appear"`` (default) or ``"disappear"``.
            key: Selector-store key (dev-capture workflow).

        Returns:
            ``True`` if the condition held in time, ``False`` otherwise.
        """
        config: dict[str, Any] = {
            "timeout_ms": timeout_ms,
            "interval_ms": interval_ms,
            "wait_for": wait_for,
        }
        if handle is not None:
            config["pid"] = handle.pid
        if name is not None:
            config["name"] = name
        if automation_id is not None:
            config["automation_id"] = automation_id
        if control_type is not None:
            config["control_type"] = control_type
        if class_name is not None:
            config["class_name"] = class_name
        if pid is not None:
            config["pid"] = pid
        config = await self._keyed_config(key, config)
        result = await self._execute("windows.wait", config)
        return bool(result)

    async def delay(self, duration_ms: int) -> None:
        """Pause bot execution for a specified duration.

        Args:
            duration_ms: Delay duration in milliseconds.
        """
        await self._execute("windows.delay", {"duration_ms": duration_ms})

    async def screenshot(
        self,
        path: str,
        handle: _SupportsPid | None = None,
        *,
        pid: int | None = None,
        image_format: str = "png",
    ) -> dict[str, Any]:
        """Capture a screenshot and save it to a file.

        Requires ``mss`` and ``Pillow`` (included in ``smithy[windows]``).

        Args:
            path: File path to save the screenshot.
            handle: ProcessHandle to capture that window.
            pid: Process ID to capture that window.
            image_format: Image format — ``"png"`` (default) or ``"jpg"``.

        Returns:
            Dict with ``"status"``, ``"path"``, and ``"format"`` keys.
        """
        config: dict[str, Any] = {"path": path, "format": image_format}
        if handle is not None:
            config["pid"] = handle.pid
        elif pid is not None:
            config["pid"] = pid
        out: dict[str, Any] = await self._execute("windows.screenshot", config)
        return out

    async def input_text(
        self,
        handle: _SupportsPid | None = None,
        *,
        text: str,
        key: str | None = None,
        **kwargs: Any,
    ) -> InputTextResult:
        """Type plain text into a UI element or the focused window.

        If *handle* or selector fields are provided, focuses the
        element first.  Otherwise types into the focused window.

        Args:
            handle: ProcessHandle to scope element search by PID.
            text: Plain text to type.
            key: Selector-store key (dev-capture workflow).
            **kwargs: ``element_key`` or selector fields (name,
                automation_id, control_type, class_name, pid).

        Returns:
            InputTextResult.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, {"text": text, **kwargs})
        result = await self._execute_keyed("windows.input_text", config, key)
        return InputTextResult(status=result.get("status", "typed"))

    async def keyboard(
        self,
        handle: _SupportsPid | None = None,
        *,
        keys: str,
        **kwargs: Any,
    ) -> InputTextResult:
        """Send key combinations and key presses.

        Bracketed tokens are key events; everything else is plain text.
        ``[CTRL]S`` — hold Ctrl, type S.  ``[CTRL!]S`` — tap Ctrl, type S.

        If *handle* or selector fields are provided, focuses the
        element first.  Otherwise sends keys to the focused window.

        Args:
            handle: ProcessHandle to scope element search by PID.
            keys: Key presses with bracket syntax.
            **kwargs: ``element_key`` or selector fields (name,
                automation_id, control_type, class_name, pid).

        Returns:
            InputTextResult.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        result = await self._execute("windows.keyboard", {"keys": keys, **kwargs})
        return InputTextResult(status=result.get("status", "sent"))

    async def set_text(
        self,
        handle: _SupportsPid | None = None,
        *,
        text: str,
        key: str | None = None,
        **kwargs: Any,
    ) -> SetTextResult:
        """Replace the entire text of a UI element via UIA ValuePattern.

        Args:
            handle: ProcessHandle to scope element search by PID.
            text: Text to set.
            key: Selector-store key (dev-capture workflow).
            **kwargs: ``element_key`` or selector fields (name,
                automation_id, control_type, class_name, pid).

        Returns:
            SetTextResult.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, {"text": text, **kwargs})
        result = await self._execute_keyed("windows.set_text", config, key)
        return SetTextResult(status=result.get("status", "set"))

    async def get_element(
        self,
        handle: _SupportsPid | None = None,
        *,
        key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Read a UI element's attributes.

        Args:
            handle: ProcessHandle to scope element search by PID.
            key: Selector-store key (dev-capture workflow).
            **kwargs: ``element_key`` or selector fields (name,
                automation_id, control_type, class_name, pid).

        Returns:
            Dict describing the element (name, control_type,
            automation_id, class_name, pid, rect).
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, dict(kwargs))
        out: dict[str, Any] = await self._execute_keyed("windows.get_element", config, key)
        return out

    async def scroll(
        self,
        handle: _SupportsPid | None = None,
        *,
        direction: str = "down",
        wheel_clicks: int = 3,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Scroll the wheel over a UI element or the focused window.

        Args:
            handle: ProcessHandle to scope element search by PID.
            direction: Scroll direction — ``"up"`` or ``"down"`` (default).
            wheel_clicks: Number of wheel notches.
            **kwargs: Optional selector fields (name, automation_id, etc.).

        Returns:
            Dict with ``"status"`` and ``"direction"`` keys.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        out: dict[str, Any] = await self._execute(
            "windows.scroll",
            {"direction": direction, "wheel_clicks": wheel_clicks, **kwargs},
        )
        return out

    async def hover(
        self,
        handle: _SupportsPid | None = None,
        *,
        key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Move the mouse over a UI element (opens tooltips/menus).

        Args:
            handle: ProcessHandle to scope element search by PID.
            key: Selector-store key (dev-capture workflow).
            **kwargs: Selector fields (name, automation_id, etc.).

        Returns:
            Dict with ``"status"`` key.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, dict(kwargs))
        out: dict[str, Any] = await self._execute_keyed("windows.hover", config, key)
        return out

    async def exists(
        self,
        handle: _SupportsPid | None = None,
        *,
        key: str | None = None,
        **kwargs: Any,
    ) -> bool:
        """Check whether a UI element exists right now.

        Args:
            handle: ProcessHandle to scope element search by PID.
            key: Selector-store key (dev-capture workflow).
            **kwargs: Selector fields (name, automation_id, etc.).

        Returns:
            ``True`` if the element exists, ``False`` otherwise.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, dict(kwargs))
        result = await self._execute("windows.exists", config)
        return bool(result)

    async def get_text(
        self,
        handle: _SupportsPid | None = None,
        *,
        key: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Read the visible text of a UI element.

        Args:
            handle: ProcessHandle to scope element search by PID.
            key: Selector-store key (dev-capture workflow).
            **kwargs: Selector fields (name, automation_id, etc.).

        Returns:
            Element text (empty string when unreadable).
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, dict(kwargs))
        result = await self._execute_keyed("windows.get_text", config, key)
        if isinstance(result, dict):
            return str(result.get("text", ""))
        return str(result)

    async def window(
        self,
        handle: _SupportsPid | None = None,
        *,
        action: str,
        pid: int | None = None,
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        """Manage a top-level window by PID.

        Pair ``action="activate"`` before clicks to fix the classic flake
        where clicks miss because the window is not foreground.

        Args:
            handle: ProcessHandle owning the window.
            action: ``"activate"``, ``"minimize"``, ``"maximize"``,
                ``"restore"``, ``"move"``, or ``"close"``.
            pid: Process ID (alternative to *handle*).
            x: Left edge (``"move"`` only).
            y: Top edge (``"move"`` only).
            width: Width (``"move"`` only).
            height: Height (``"move"`` only).

        Returns:
            Dict with ``"status"``, ``"action"``, and ``"pid"`` keys.
        """
        config: dict[str, Any] = {"action": action}
        if handle is not None:
            config["pid"] = handle.pid
        elif pid is not None:
            config["pid"] = pid
        if x is not None:
            config["x"] = x
        if y is not None:
            config["y"] = y
        if width is not None:
            config["width"] = width
        if height is not None:
            config["height"] = height
        out: dict[str, Any] = await self._execute("windows.window", config)
        return out

    async def select(
        self,
        handle: _SupportsPid | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Select an item in a dropdown, combobox, or list.

        Args:
            handle: ProcessHandle to scope element search by PID.
            **kwargs: Selector fields identifying the item (name, pid, …).

        Returns:
            Dict with ``"status"`` key.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        out: dict[str, Any] = await self._execute("windows.select", kwargs)
        return out

    async def drag(
        self,
        handle: _SupportsPid | None = None,
        *,
        from_x: int | None = None,
        from_y: int | None = None,
        to_x: int | None = None,
        to_y: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Drag from one UI point to another.

        Each endpoint is either coordinates (``from_x``/``from_y``) or
        selector fields with ``from_``/``to_`` prefixes (``from_name``,
        ``to_name``, ``from_pid``, …). Both endpoints must resolve.

        Args:
            handle: ProcessHandle to scope element search by PID.
            from_x: Start X coordinate.
            from_y: Start Y coordinate.
            to_x: End X coordinate.
            to_y: End Y coordinate.
            **kwargs: ``from_*``/``to_*`` selector fields.

        Returns:
            Dict with ``"status"``, ``"from"``, and ``"to"`` keys.
        """
        config: dict[str, Any] = dict(kwargs)
        if handle is not None:
            config.setdefault("from_pid", handle.pid)
        if from_x is not None:
            config["from_x"] = from_x
        if from_y is not None:
            config["from_y"] = from_y
        if to_x is not None:
            config["to_x"] = to_x
        if to_y is not None:
            config["to_y"] = to_y
        out: dict[str, Any] = await self._execute("windows.drag", config)
        return out

    async def clipboard(
        self,
        *,
        action: str,
        text: str | None = None,
    ) -> dict[str, Any] | str:
        """Read or write the system clipboard text.

        Requires ``pyperclip`` (included in ``smithy[windows]``).

        Args:
            action: ``"get"`` reads, ``"set"`` writes.
            text: Text to put on the clipboard (``"set"`` only).

        Returns:
            Clipboard text for ``"get"``; dict with ``"status"`` for ``"set"``.
        """
        config: dict[str, Any] = {"action": action}
        if text is not None:
            config["text"] = text
        result = await self._execute("windows.clipboard", config)
        if action == "get":
            if isinstance(result, dict):
                return str(result.get("text", ""))
            return str(result)
        out: dict[str, Any] = result
        return out

    async def list_elements(
        self,
        handle: _SupportsPid | None = None,
        *,
        max_items: int = 50,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """List direct child elements of a window or container.

        Use this to discover stable automation IDs before writing the bot.

        Args:
            handle: ProcessHandle to scope element search by PID.
            max_items: Max children to return.
            **kwargs: Selector fields for the parent element.

        Returns:
            Dict with ``"items"`` and ``"count"`` keys.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        out: dict[str, Any] = await self._execute(
            "windows.list_elements", {"max_items": max_items, **kwargs}
        )
        return out

    async def highlight(
        self,
        handle: _SupportsPid | None = None,
        *,
        color: str = "red",
        duration_ms: int = 1000,
        key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Flash a colored rectangle around an element for debugging.

        Args:
            handle: ProcessHandle to scope element search by PID.
            color: ``"red"`` (default), ``"green"``, ``"blue"``, ``"yellow"``.
            duration_ms: How long to show the rectangle.
            key: Selector-store key (dev-capture workflow).
            **kwargs: Selector fields (name, automation_id, etc.).

        Returns:
            Dict with ``"status"`` and ``"color"`` keys.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(
            key, {"color": color, "duration_ms": duration_ms, **kwargs}
        )
        out: dict[str, Any] = await self._execute_keyed("windows.highlight", config, key)
        return out

    async def get_table(
        self,
        handle: _SupportsPid | None = None,
        *,
        max_rows: int = 100,
        key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Extract rows of a table/list/grid element as JSON.

        Args:
            handle: ProcessHandle to scope element search by PID.
            max_rows: Max data rows to extract.
            key: Selector-store key (dev-capture workflow).
            **kwargs: Selector fields for the table container.

        Returns:
            Dict with ``"rows"``, ``"count"`` and optional ``"columns"``.
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, {"max_rows": max_rows, **kwargs})
        out: dict[str, Any] = await self._execute_keyed("windows.get_table", config, key)
        return out

    async def control_action(
        self,
        handle: _SupportsPid | None = None,
        *,
        action: str,
        key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Perform a native UIA pattern action (no mouse clicks).

        Args:
            handle: ProcessHandle to scope element search by PID.
            action: ``"invoke"``, ``"toggle"``, ``"expand"``,
                ``"collapse"``, ``"select"``, or ``"focus"``.
            key: Selector-store key (dev-capture workflow).
            **kwargs: Selector fields for the target element.

        Returns:
            Dict with ``"status"`` and ``"action"`` (plus
            ``"toggle_state"`` for toggles).
        """
        if handle is not None:
            kwargs.setdefault("pid", handle.pid)
        config = await self._keyed_config(key, {"action": action, **kwargs})
        out: dict[str, Any] = await self._execute_keyed("windows.control_action", config, key)
        return out

    async def process_wait(
        self,
        handle: _SupportsPid | None = None,
        *,
        pid: int | None = None,
        timeout_ms: int = 30000,
    ) -> dict[str, Any]:
        """Wait for a process to exit and report its exit code.

        Args:
            handle: ProcessHandle whose PID to wait for.
            pid: Process ID to wait for.
            timeout_ms: Max wait in milliseconds.

        Returns:
            Dict with ``"status"`` (``"exited"``/``"timeout"``) and
            ``"exit_code"`` when the process exited in time.
        """
        resolved_pid = handle.pid if handle is not None else pid
        out: dict[str, Any] = await self._execute(
            "windows.process", {"action": "wait", "pid": resolved_pid, "timeout_ms": timeout_ms}
        )
        return out

    async def process_status(
        self,
        handle: _SupportsPid | None = None,
        *,
        pid: int | None = None,
    ) -> dict[str, Any]:
        """Query whether a process is running (and its exit code when done).

        Args:
            handle: ProcessHandle whose PID to query.
            pid: Process ID to query.

        Returns:
            Dict with ``"running"`` and ``"exit_code"`` keys.
        """
        resolved_pid = handle.pid if handle is not None else pid
        out: dict[str, Any] = await self._execute(
            "windows.process", {"action": "status", "pid": resolved_pid}
        )
        return out

    async def call(self, name: str, **kwargs: Any) -> Any:
        """Execute a tool by name. For custom and non-standard tools.

        Args:
            name: Tool name (e.g. "data.read_table").
            **kwargs: Tool parameters.

        Returns:
            Tool execution result.
        """
        return await self._execute(name, kwargs)
