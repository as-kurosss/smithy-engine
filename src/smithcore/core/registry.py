"""Tool registry for centralized management and execution."""

from __future__ import annotations

import threading
import warnings
from typing import Any

from smithcore.core.errors import InvalidInput
from smithcore.core.schema import validate_against_schema
from smithcore.core.tool import Tool


class ToolRegistry:
    """Registry of tools keyed by name.

    Stores tools and dispatches execute calls by name. The schema is
    captured once at registration, so a tool whose ``schema()`` is
    expensive is not re-evaluated on every call.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def register(self, tool: Tool) -> None:
        """Register a tool by its name property.

        Re-registering an existing name overwrites the previous tool
        and emits a ``UserWarning`` so silent replacement is explicit.
        A tool whose ``schema()`` returns an empty dict also emits a
        ``UserWarning`` — input validation is silently disabled for it.
        """
        schema = tool.schema()
        with self._lock:
            if tool.name in self._tools:
                warnings.warn(
                    f"Tool {tool.name!r} is already registered and will be overwritten",
                    UserWarning,
                    stacklevel=2,
                )
            if not schema:
                warnings.warn(
                    f"Tool {tool.name!r} returns an empty schema(); input validation is disabled",
                    UserWarning,
                    stacklevel=2,
                )
            self._tools[tool.name] = tool
            self._schemas[tool.name] = schema

    def get(self, name: str) -> Tool | None:
        """Get a registered tool by name."""
        with self._lock:
            return self._tools.get(name)

    def list_tools(self) -> list[str]:
        """Return names of all registered tools."""
        with self._lock:
            return sorted(self._tools.keys())

    async def execute(
        self,
        name: str,
        config: dict[str, Any],
    ) -> Any:
        """Execute a tool by name with JSON parameters.

        The config is validated against the tool's ``schema()`` first;
        violations raise ``InvalidInput`` without running the tool.

        Raises InvalidInput if the tool is not found.
        """
        with self._lock:
            tool = self._tools.get(name)
            schema = self._schemas.get(name)
        if tool is None or schema is None:
            raise InvalidInput(f"Tool '{name}' not found")
        problems = validate_against_schema(schema, config)
        if problems:
            raise InvalidInput(
                f"Tool {name!r} rejected config: " + "; ".join(problems),
                param=None,
                input_value=config,
            )
        return await tool.execute(config)
