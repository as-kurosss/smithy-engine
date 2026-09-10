"""Selector capture — inspect UIA elements at screen coordinates.

Public API::

    from smithy.windows.tools.selector_capture import (
        BestSelector,
        CaptureRecord,
        PathNode,
        capture_at_point,
        capture_once,
        path_to_dicts,
    )
"""

from smithy.windows.tools.selector_capture.api import (
    CaptureCancelled,
    CapturedSelector,
    capture_once,
    capture_once_async,
)
from smithy.windows.tools.selector_capture.capture import (
    BestSelector,
    CaptureRecord,
    PathNode,
    capture_at_point,
    path_to_dicts,
)
from smithy.windows.tools.selector_capture.flowgen import nodes_to_flow
from smithy.windows.tools.selector_capture.record import record_series

__all__ = [
    "BestSelector",
    "CapturedSelector",
    "CaptureCancelled",
    "CaptureRecord",
    "PathNode",
    "capture_at_point",
    "capture_once",
    "capture_once_async",
    "nodes_to_flow",
    "path_to_dicts",
    "record_series",
]
