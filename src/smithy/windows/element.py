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

        All properties are read in a single blocking executor hop (one
        COM round-trip batch instead of seven sequential await hops).
        Individual ``None`` values are skipped so the result is always
        JSON-serializable.
        """
        (
            name,
            ctype,
            auto_id,
            class_name,
            pid,
            rect,
        ) = await run_blocking(_read_info, self._element)
        result: dict[str, Any] = {}
        if name:
            result["name"] = name
        if ctype and ctype != "None":
            result["control_type"] = ctype
        if auto_id and auto_id != "None":
            result["automation_id"] = auto_id
        if class_name and class_name != "None":
            result["class_name"] = class_name
        if pid:
            result["pid"] = pid
        if rect is not None:
            left, top, right, bottom = rect
            if any(value is not None for value in rect):
                result["rect"] = f"{left},{top},{right},{bottom}"
        return result


def _read_info(element: Any) -> tuple[str, str, str, str, int, tuple[Any, ...] | None]:
    """Read all identifying properties in one executor hop."""
    name = str(element.Name)
    ctype = str(element.ControlTypeName)
    auto_id = str(element.AutomationId)
    class_name = str(element.ClassName)
    pid = int(element.ProcessId)
    rect = getattr(element, "BoundingRectangle", None)
    rect_values: tuple[Any, ...] | None = None
    if rect is not None:
        rect_values = (
            getattr(rect, "left", None),
            getattr(rect, "top", None),
            getattr(rect, "right", None),
            getattr(rect, "bottom", None),
        )
    return name, ctype, auto_id, class_name, pid, rect_values
