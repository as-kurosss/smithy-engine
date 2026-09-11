"""ElementSelector — builder-style selector for finding Windows UI elements."""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from smithcore.core.errors import ElementNotFound, InvalidInput, PlatformError


@dataclass
class ElementSelector:
    """Builder-style selector for finding Windows UI elements.

    Uses ``uiautomation.FindControl`` with a compare function.
    """

    pid: int | None = None
    name: str | None = None
    automation_id: str | None = None
    control_type: str | None = None
    class_name: str | None = None

    def with_pid(self, pid: int) -> ElementSelector:
        """Filter by process ID (returns a new selector)."""
        return replace(self, pid=pid)

    def with_name(self, name: str) -> ElementSelector:
        """Filter by element name (returns a new selector).

        Supports glob-style wildcards (``*`` and ``?``) via
        :mod:`fnmatch` when the name contains ``*`` or ``?``.
        Without wildcards the match is exact.
        """
        return replace(self, name=name)

    def with_automation_id(self, automation_id: str) -> ElementSelector:
        """Filter by automation ID (returns a new selector)."""
        return replace(self, automation_id=automation_id)

    def with_control_type(self, control_type: str) -> ElementSelector:
        """Filter by control type name, e.g. Button, Edit, Window.

        Returns a new selector; the original is left unchanged.
        """
        return replace(self, control_type=control_type)

    def with_class_name(self, class_name: str) -> ElementSelector:
        """Filter by class name (returns a new selector)."""
        return replace(self, class_name=class_name)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> ElementSelector | None:
        """Build a selector from tool-config fields, or ``None`` if none present.

        Recognized fields: ``name``, ``automation_id``, ``control_type``,
        ``class_name``, ``pid``.

        Raises:
            InvalidInput: If ``pid`` is not an integer or ``control_type``
                is not a known control type name.
        """
        keys = ("name", "automation_id", "control_type", "class_name", "pid")
        if not any(key in config for key in keys):
            return None
        if "pid" in config:
            pid = config["pid"]
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise InvalidInput(
                    "Invalid 'pid': expected an integer",
                    param="pid",
                    input_value=pid,
                )
        if "control_type" in config:
            raw_ct = config["control_type"]
            if not isinstance(raw_ct, str) or parse_control_type(raw_ct) is None:
                raise InvalidInput(
                    f"Unknown control_type: {raw_ct!r}",
                    param="control_type",
                    input_value=raw_ct,
                )
        selector = cls()
        if "name" in config:
            selector = selector.with_name(config["name"])
        if "automation_id" in config:
            selector = selector.with_automation_id(config["automation_id"])
        if "control_type" in config:
            selector = selector.with_control_type(config["control_type"])
        if "class_name" in config:
            selector = selector.with_class_name(config["class_name"])
        if "pid" in config:
            selector = selector.with_pid(config["pid"])
        return selector

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict of non-None fields (for JSON serialization)."""
        d: dict[str, Any] = {}
        if self.pid is not None:
            d["pid"] = self.pid
        if self.name is not None:
            d["name"] = self.name
        if self.automation_id is not None:
            d["automation_id"] = self.automation_id
        if self.control_type is not None:
            d["control_type"] = self.control_type
        if self.class_name is not None:
            d["class_name"] = self.class_name
        return d

    def find_first(self, root: Any, automation: Any) -> Any:
        """Find the first matching element under ``root``.

        Args:
            root: A ``uiautomation`` control to search under.
            automation: The ``uiautomation.uiautomation`` module.

        Returns:
            The first matching control.

        Raises:
            ElementNotFound: If no element matches.
            PlatformError: If the UIA call fails.
        """
        compare = self._build_compare()
        try:
            element = automation.FindControl(root, compare)
        except Exception as exc:
            raise PlatformError(
                "find_first failed",
                source=exc,
            ) from exc
        if element is None:
            raise ElementNotFound(
                "No element found matching selector",
                selector=self.to_dict(),
            )
        return element

    def find_from_desktop(self) -> Any:
        """Find the first matching element starting from the desktop root.

        When only ``pid`` is set (no name / automation_id / control_type /
        class_name), the search is narrowed to ``WindowControl(pid=…)``,
        which avoids a full desktop tree traversal and returns quickly.

        Returns:
            The first matching control.

        Raises:
            ElementNotFound: If no element matches.
            PlatformError: If UIA init fails.
        """
        try:
            import uiautomation as auto
        except Exception as exc:
            raise PlatformError(
                "UIAutomation init failed",
                source=exc,
            ) from exc

        # Fast path: PID only — find the window directly without
        # scanning the entire desktop tree (which can hang on large
        # UIA trees when only filtering by PID).
        if (
            self.pid is not None
            and self.name is None
            and self.automation_id is None
            and self.control_type is None
            and self.class_name is None
        ):
            return _find_window_by_pid(self.pid)

        root = auto.GetRootControl()
        return self.find_first(root, auto.uiautomation)

    def count_from_desktop(self, limit: int = 2) -> int:
        """Count matching elements under the desktop root, up to *limit*.

        A bounded Python tree walk (stops as soon as *limit* matches are
        found) used to check selector uniqueness — the desktop equivalent
        of Playwright's strict mode. Slower than the native ``FindControl``
        used by :meth:`find_from_desktop`, so this is a dev-time / validation
        helper, not a runtime search path.

        Args:
            limit: Stop counting after this many matches (must be >= 1).

        Returns:
            Number of matches, capped at *limit*.

        Raises:
            PlatformError: If UIA init fails.
        """
        if limit < 1:
            limit = 1
        try:
            import uiautomation as auto
        except Exception as exc:
            raise PlatformError(
                "UIAutomation init failed",
                source=exc,
            ) from exc

        compare = self._build_compare()
        count = 0
        stack: list[Any] = [auto.GetRootControl()]
        while stack:
            node = stack.pop()
            try:
                matched = compare(node, 0)
            except Exception:
                matched = False
            if matched:
                count += 1
                if count >= limit:
                    return count
            try:
                child = node.GetFirstChildControl()
            except Exception:
                continue
            while child is not None:
                stack.append(child)
                try:
                    child = child.GetNextSiblingControl()
                except Exception:
                    break
        return count

    def _build_compare(self) -> Callable[[Any, int], bool]:
        """Build a compare function for FindControl."""
        # Capture values for the closure
        name = self.name
        automation_id = self.automation_id
        control_type = self.control_type
        class_name = self.class_name
        pid = self.pid

        ct_value = parse_control_type(control_type) if control_type else None
        use_wildcard = name is not None and _has_wildcard(name)

        def compare(ctrl: Any, depth: int) -> bool:
            # All set fields combine with AND so pid scoping always applies.
            # Stale elements raise COMError on property access; treat them
            # as non-matches so the search keeps going instead of failing.
            try:
                return _compare_fields(ctrl)
            except Exception:
                return False

        def _compare_fields(ctrl: Any) -> bool:
            if name is not None:
                if use_wildcard:
                    if not fnmatch.fnmatch(str(ctrl.Name or ""), name):
                        return False
                elif ctrl.Name != name:
                    return False
            if automation_id is not None and ctrl.AutomationId != automation_id:
                return False
            if ct_value is not None and ctrl.ControlType != ct_value:
                return False
            if class_name is not None and ctrl.ClassName != class_name:
                return False
            if pid is not None and ctrl.ProcessId != pid:  # noqa: SIM103 — explicit chain reads better
                return False
            return True

        return compare


def _find_window_by_pid(pid: int) -> Any:
    """Find the first visible window belonging to *pid*.

    Uses the Win32 ``EnumWindows`` API directly so the search is
    bounded to top-level windows instead of traversing the entire
    UIA tree.

    Raises:
        ElementNotFound: If no visible window belongs to *pid*.
        PlatformError: If the Win32 call fails.
    """
    import ctypes
    import ctypes.wintypes

    import uiautomation as auto

    result_hwnd: int | None = None

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)  # type: ignore[untyped-decorator]
    def _enum_callback(hwnd: int, _lparam: int) -> bool:
        nonlocal result_hwnd
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            out_pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(
                hwnd,
                ctypes.byref(out_pid),
            )
            if out_pid.value == pid:
                result_hwnd = hwnd
                return False  # stop enumeration
        return True  # continue

    ctypes.windll.user32.EnumWindows(_enum_callback, 0)

    if result_hwnd is not None:
        return auto.ControlFromHandle(result_hwnd)

    raise ElementNotFound(
        f"No window found for PID {pid}",
        selector={"pid": pid},
    )


# Control type string → integer mapping. Values are the official UIA
# ControlTypeIds from UIAutomationClient.h (they also match the
# ``uiautomation.ControlType`` enum members).
_CONTROL_TYPE_MAP: dict[str, int] = {
    "button": 50000,
    "calendar": 50001,
    "checkbox": 50002,
    "combobox": 50003,
    "edit": 50004,
    "hyperlink": 50005,
    "image": 50006,
    "listitem": 50007,
    "list": 50008,
    "menu": 50009,
    "menubar": 50010,
    "menuitem": 50011,
    "progressbar": 50012,
    "radiobutton": 50013,
    "scrollbar": 50014,
    "slider": 50015,
    "spinner": 50016,
    "statusbar": 50017,
    "tab": 50018,
    "tabitem": 50019,
    "text": 50020,
    "toolbar": 50021,
    "tooltip": 50022,
    "tree": 50023,
    "treeitem": 50024,
    "custom": 50025,
    "group": 50026,
    "thumb": 50027,
    "datagrid": 50028,
    "dataitem": 50029,
    "document": 50030,
    "splitbutton": 50031,
    "window": 50032,
    "pane": 50033,
    "header": 50034,
    "headeritem": 50035,
    "table": 50036,
    "titlebar": 50037,
    "separator": 50038,
    "semanticzoom": 50039,
    "appbar": 50040,
}


def parse_control_type(s: str) -> int | None:
    """Parse a control type string into its numeric UIA identifier.

    Returns ``None`` for unknown names. Case-insensitive.
    """
    return _CONTROL_TYPE_MAP.get(s.lower())


def _has_wildcard(s: str) -> bool:
    """Return True if *s* contains fnmatch wildcards (``*`` or ``?``)."""
    return "*" in s or "?" in s
