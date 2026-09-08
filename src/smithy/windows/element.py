"""SafeUIElement — thread-safe wrapper around a UIA element."""

from __future__ import annotations

from typing import Any

from smithy.core.blocking import run_blocking


class SafeUIElement:
    """Thread-safe wrapper around a UIA element.

    UIA elements are COM objects and not thread-safe. This wrapper
    runs UIA operations in a thread executor to avoid blocking the
    asyncio event loop.
    """

    def __init__(self, element: Any) -> None:
        self._element = element

    @property
    def element(self) -> Any:
        """Access the underlying UIA element (not thread-safe)."""
        return self._element

    async def click(self) -> None:
        """Click the element (runs in thread executor)."""
        await run_blocking(self._element.Click)

    async def get_name(self) -> str:
        """Get the element's name."""
        return str(await run_blocking(self._element.Name))

    async def get_control_type(self) -> str:
        """Get the element's control type."""
        return str(await run_blocking(self._element.ControlTypeName))

    async def get_automation_id(self) -> str:
        """Get the element's automation ID."""
        return str(await run_blocking(self._element.AutomationId))

    async def get_class_name(self) -> str:
        """Get the element's class name."""
        return str(await run_blocking(self._element.ClassName))

    async def get_pid(self) -> int:
        """Get the owning process ID."""
        return int(await run_blocking(self._element.ProcessId))

    async def get_rect(self) -> str:
        """Get the bounding rectangle as ``left,top,right,bottom``."""
        rect = await run_blocking(self._element.BoundingRectangle)
        left = getattr(rect, "left", None)
        top = getattr(rect, "top", None)
        right = getattr(rect, "right", None)
        bottom = getattr(rect, "bottom", None)
        return f"{left},{top},{right},{bottom}"

    async def get_info(self) -> dict[str, Any]:
        """Return a dict snapshot of identifying properties.

        Individual ``None`` values are skipped so the result is always
        JSON-serializable.
        """
        result: dict[str, Any] = {}
        name = await self.get_name()
        if name:
            result["name"] = name
        ctype = await self.get_control_type()
        if ctype and ctype != "None":
            result["control_type"] = ctype
        auto_id = await self.get_automation_id()
        if auto_id and auto_id != "None":
            result["automation_id"] = auto_id
        class_name = await self.get_class_name()
        if class_name and class_name != "None":
            result["class_name"] = class_name
        pid = await self.get_pid()
        if pid:
            result["pid"] = pid
        rect = await self.get_rect()
        if rect and rect != "None,None,None,None":
            result["rect"] = rect
        return result
