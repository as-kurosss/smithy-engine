"""Core capture logic for inspecting UIA elements at screen coordinates.

Ported from the Rust ``selector-capture`` crate.  Uses the ``uiautomation``
pip package (same backend as :mod:`smithy.windows.selector`).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

try:
    import uiautomation as auto
except Exception:  # pragma: no cover - optional Windows backend
    auto = cast("Any", None)  # fallback keeps the module importable off-Windows

from smithy.core.errors import PlatformError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathNode:
    """A single node in the UI Automation tree path from desktop root to the
    target element.

    Each attribute mirrors the corresponding UIA property.  ``None`` means
    the property was empty or unavailable on the original element.
    """

    control_type: str
    class_name: str | None = None
    name: str | None = None
    automation_id: str | None = None
    control_type_name: str | None = None
    localized_control_type: str | None = None
    accelerator_key: str | None = None
    access_key: str | None = None
    aria_role: str | None = None
    aria_properties: str | None = None
    framework_id: str | None = None
    help_text: str | None = None
    item_status: str | None = None
    item_type: str | None = None
    orientation: str | None = None
    provider_description: str | None = None
    process_id: str | None = None
    native_window_handle: str | None = None
    bounding_rectangle: str | None = None
    is_enabled: str | None = None
    is_offscreen: str | None = None
    is_keyboard_focusable: str | None = None
    has_keyboard_focus: str | None = None
    is_control_element: str | None = None
    is_content_element: str | None = None
    is_password: str | None = None
    is_required_for_form: str | None = None
    is_data_valid_for_form: str | None = None


@dataclass(frozen=True, slots=True)
class BestSelector:
    """Flat optimal selector describing the captured target element.

    Extracted from the last :class:`PathNode` of the full path.  Provides
    convenience helpers for display and dict serialisation.
    """

    control_type: str
    name: str | None = None
    class_name: str | None = None
    automation_id: str | None = None

    def label(self) -> str:
        """Return a human-readable label for display.

        Prefers ``name``; falls back to ``control_type`` when the name is
        absent.
        """
        return self.name if self.name is not None else self.control_type

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict, omitting ``None`` values."""
        d: dict[str, Any] = {"control_type": self.control_type}
        if self.name is not None:
            d["name"] = self.name
        if self.class_name is not None:
            d["class_name"] = self.class_name
        if self.automation_id is not None:
            d["automation_id"] = self.automation_id
        return d

    def has_any(self) -> bool:
        """Return ``True`` if *any* identifying field is set."""
        return bool(
            self.name is not None
            or self.automation_id is not None
            or self.class_name is not None
            or self.control_type
        )


@dataclass
class CaptureRecord:
    """A single capture record bundling the full path, best selector, and
    metadata required for serialisation and later reference.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    description: str | None = None
    full_path: list[PathNode] = field(default_factory=list)
    best_selector: BestSelector = field(
        default_factory=lambda: BestSelector(control_type="Unknown"),
    )


# ---------------------------------------------------------------------------
# Element helpers
# ---------------------------------------------------------------------------


def read_node(element: Any) -> PathNode:
    """Read the identifying UIA properties of *element* into a :class:`PathNode`.

    Empty strings are treated as absent (mapped to ``None``).
    Boolean / numeric values are serialised as strings.

    Args:
        element: A ``uiautomation`` control object.

    Returns:
        A :class:`PathNode` with the element's properties.
    """
    try:
        raw_ct = element.ControlType
    except Exception:
        raw_ct = None

    control_type = str(raw_ct) if raw_ct is not None and raw_ct != 0 else "Unknown"

    def _non_empty(value: Any) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        return s if s else None

    def _str_or_none(attr: str) -> str | None:
        return _non_empty(getattr(element, attr, None))

    # BoundingRectangle → dict with left/top/right/bottom or None.
    rect_str: str | None = None
    try:
        rect = element.BoundingRectangle
        if rect is not None:
            left = getattr(rect, "left", None)
            top = getattr(rect, "top", None)
            right = getattr(rect, "right", None)
            bottom = getattr(rect, "bottom", None)
            if left is not None:
                rect_str = f"{left},{top},{right},{bottom}"
    except Exception:
        pass

    return PathNode(
        control_type=control_type,
        class_name=_str_or_none("ClassName"),
        name=_str_or_none("Name"),
        automation_id=_str_or_none("AutomationId"),
        control_type_name=_str_or_none("ControlTypeName"),
        localized_control_type=_str_or_none("LocalizedControlType"),
        accelerator_key=_str_or_none("AcceleratorKey"),
        access_key=_str_or_none("AccessKey"),
        aria_role=_str_or_none("AriaRole"),
        aria_properties=_str_or_none("AriaProperties"),
        framework_id=_str_or_none("FrameworkId"),
        help_text=_str_or_none("HelpText"),
        item_status=_str_or_none("ItemStatus"),
        item_type=_str_or_none("ItemType"),
        orientation=_str_or_none("Orientation"),
        provider_description=_str_or_none("ProviderDescription"),
        process_id=_str_or_none("ProcessId"),
        native_window_handle=_str_or_none("NativeWindowHandle"),
        bounding_rectangle=rect_str,
        is_enabled=_str_or_none("IsEnabled"),
        is_offscreen=_str_or_none("IsOffscreen"),
        is_keyboard_focusable=_str_or_none("IsKeyboardFocusable"),
        has_keyboard_focus=_str_or_none("HasKeyboardFocus"),
        is_control_element=_str_or_none("IsControlElement"),
        is_content_element=_str_or_none("IsContentElement"),
        is_password=_str_or_none("IsPassword"),
        is_required_for_form=_str_or_none("IsRequiredForForm"),
        is_data_valid_for_form=_str_or_none("IsDataValidForForm"),
    )


def contains_point(element: Any, x: int, y: int) -> bool:
    """Check whether the bounding rectangle of *element* contains ``(x, y)``.

    Args:
        element: A ``uiautomation`` control object.
        x: Horizontal screen coordinate in logical pixels.
        y: Vertical screen coordinate in logical pixels.

    Returns:
        ``True`` if the point lies inside the element's bounding rectangle.
    """
    try:
        rect = element.BoundingRectangle
    except Exception:
        return False

    if rect is None:
        return False

    left = getattr(rect, "left", None)
    top = getattr(rect, "top", None)
    right = getattr(rect, "right", None)
    bottom = getattr(rect, "bottom", None)

    if left is None or top is None or right is None or bottom is None:
        return False

    return bool(left <= x <= right and top <= y <= bottom)


# ---------------------------------------------------------------------------
# Tree traversal
# ---------------------------------------------------------------------------


def find_deepest_at_point(
    root: Any,
    x: int,
    y: int,
) -> Any:
    """Walk the UIA tree downward from *root* and return the deepest element
    that contains the point ``(x, y)``.

    Uses ``Control.GetFirstChildControl()`` and
    ``Control.GetNextSiblingControl()`` for child/sibling iteration.

    Args:
        root: The starting UIA element (typically the desktop root).
        x: Horizontal screen coordinate in logical pixels.
        y: Vertical screen coordinate in logical pixels.

    Returns:
        The deepest UIA element containing the point.
    """
    current = root

    while True:
        first_child = current.GetFirstChildControl()
        if first_child is None:
            return current

        found_child = None
        child = first_child
        while child is not None:
            if contains_point(child, x, y):
                found_child = child
                break
            child = child.GetNextSiblingControl()

        if found_child is None:
            return current

        current = found_child


# ---------------------------------------------------------------------------
# Selector extraction
# ---------------------------------------------------------------------------


def best_selector_from_path(path: list[PathNode]) -> BestSelector:
    """Extract a :class:`BestSelector` from the target (last) node of *path*.

    Args:
        path: Ordered list of :class:`PathNode` from root to target.

    Returns:
        A :class:`BestSelector` mirroring the target node's properties.

    Raises:
        ValueError: If *path* is empty.
    """
    if not path:
        msg = "Cannot build BestSelector from an empty path"
        raise ValueError(msg)

    target = path[-1]
    return BestSelector(
        control_type=target.control_type,
        name=target.name,
        class_name=target.class_name,
        automation_id=target.automation_id,
    )


# ---------------------------------------------------------------------------
# Main capture API
# ---------------------------------------------------------------------------


def capture_at_point(
    x: float,
    y: float,
) -> tuple[list[PathNode], BestSelector]:
    """Capture the UIA element at screen coordinates ``(x, y)``.

    Initialises a fresh ``UIAutomation`` instance, walks the control-view
    tree downward to locate the deepest element that contains the point,
    then walks back up to the desktop root to build the full path.

    Args:
        x: Horizontal screen coordinate in logical pixels.
        y: Vertical screen coordinate in logical pixels.

    Returns:
        A tuple ``(path, best_selector)`` where *path* is the full
        element path from desktop root to the target, and
        *best_selector* is the optimal flat selector for the target.

    Raises:
        RuntimeError: If the UIA root element, control-view walker, or
            tree walker cannot be obtained.
    """
    ix = int(x)
    iy = int(y)

    if auto is None:
        raise PlatformError(
            "UIAutomation backend is not available. "
            "Install it with: pip install uiautomation (Windows only)."
        )
    root = auto.GetRootControl()
    if root is None:
        msg = "Failed to obtain the UIA root element"
        raise RuntimeError(msg)

    target = find_deepest_at_point(root, ix, iy)

    # Walk upward using Control.GetParentControl() to build the full path.
    path: list[PathNode] = []
    current = target
    while current is not None:
        path.append(read_node(current))
        current = current.GetParentControl()

    path.reverse()
    selector = best_selector_from_path(path)

    logger.debug(
        "Captured element at (%d, %d): %s — %d nodes in path",
        ix,
        iy,
        selector.label(),
        len(path),
    )

    return path, selector


def path_to_dicts(path: list[PathNode]) -> list[dict[str, Any]]:
    """Convert a list of :class:`PathNode` to a list of plain dicts.

    Omits fields whose value is ``None``.

    Args:
        path: Ordered list of :class:`PathNode` from root to target.

    Returns:
        A list of dicts suitable for JSON serialisation.
    """
    return [{k: v for k, v in asdict(node).items() if v is not None} for node in path]
