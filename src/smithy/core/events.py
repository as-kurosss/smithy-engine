"""Middleware event system for tool observability."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ToolEvent:
    """Event emitted after each tool execution.

    Attributes:
        tool_name: Fully-qualified tool name (e.g. ``"windows.click"``).
        config: The config dict passed to the tool.
        result: Tool return value (``None`` on error).
        error: Exception if the tool raised, else ``None``.
        duration_ms: Wall-clock execution time in milliseconds.
        timestamp: UTC timestamp of event creation.
        metadata: Arbitrary data — middleware can attach session IDs,
            tracing info, etc.  Merged from ``__meta__`` in tool results.
    """

    tool_name: str
    config: dict[str, Any]
    result: Any = None
    error: Exception | None = None
    duration_ms: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Middleware(Protocol):
    """Protocol for event middleware.

    A middleware receives a :class:`ToolEvent`, may transform it, and
    returns it for the next middleware.  Return ``None`` to stop
    propagation.
    """

    async def __call__(self, event: ToolEvent) -> ToolEvent | None: ...


class EventBus:
    """Ordered middleware pipeline for tool events."""

    def __init__(self) -> None:
        self._middlewares: list[Middleware] = []

    def add_middleware(self, middleware: Middleware) -> None:
        """Append a middleware to the pipeline."""
        self._middlewares.append(middleware)

    def remove_middleware(self, middleware: Middleware) -> None:
        """Remove a middleware from the pipeline.

        Raises:
            ValueError: If the middleware is not registered.
        """
        self._middlewares.remove(middleware)

    async def emit(self, event: ToolEvent) -> ToolEvent | None:
        """Run *event* through the middleware pipeline.

        Each middleware is isolated: if one raises, the exception is logged
        and skipped, so a broken middleware can never mask a tool result
        or error.

        Returns the final (possibly transformed) event, or ``None`` if
        a middleware stopped propagation.
        """
        current: ToolEvent | None = event
        for mw in self._middlewares:
            if current is None:
                return None
            try:
                current = await mw(current)
            except Exception:
                logger.exception(
                    "event middleware %r failed; skipping it",
                    mw,
                )
        return current
