"""Record → flow: event reduction and v2 conversion (no pynput/desktop needed)."""

from __future__ import annotations

import queue
import threading

import pytest

from smithy.flow import validate_document
from smithy.windows.tools.selector_capture.capture import BestSelector
from smithy.windows.tools.selector_capture.flowgen import nodes_to_flow
from smithy.windows.tools.selector_capture.generate import FlowNode
from smithy.windows.tools.selector_capture.record import _reduce_events
from smithy.windows.tools.selector_capture.recorder import SeriesEvent


def _selector() -> BestSelector:
    return BestSelector(control_type="Edit", name="Field")


def test_nodes_to_flow_chains_tools_and_validates() -> None:
    nodes = [
        FlowNode(tool="windows.click", args={"name": "OK"}),
        FlowNode(tool="windows.input_text", args={"name": "Notes", "text": "hi"}),
    ]
    doc = nodes_to_flow(nodes, name="recording")

    assert doc["version"] == 2
    assert doc["name"] == "recording"
    assert [n["id"] for n in doc["nodes"]] == ["start", "step1", "step2", "end"]
    assert [e["target"] for e in doc["edges"]] == ["step1", "step2", "end"]
    assert all(e["source_handle"] == "out" for e in doc["edges"])
    assert doc["nodes"][1]["tool"] == "windows.click"
    assert doc["nodes"][1]["label"] == "OK"
    assert validate_document(doc) == []


def test_nodes_to_flow_empty_is_start_to_end() -> None:
    doc = nodes_to_flow([])
    assert validate_document(doc) == []
    assert [e["target"] for e in doc["edges"]] == ["end"]


def test_nodes_to_flow_rejects_nameless_tool() -> None:
    with pytest.raises(ValueError):
        nodes_to_flow([FlowNode(tool="", args={})])


def test_reduce_events_click_then_text() -> None:
    events: queue.Queue[SeriesEvent] = queue.Queue()
    events.put(SeriesEvent("mouse_down"))
    for char in "hi":
        events.put(SeriesEvent("input", char))
    events.put(SeriesEvent("stop"))

    captured: list[FlowNode] = []
    _reduce_events(
        events,
        threading.Event(),
        capture_point=lambda x, y: ([], _selector()),
        mouse_position=lambda: (1.0, 2.0),
        emit=captured.append,
    )

    assert [n.tool for n in captured] == ["windows.click", "windows.input_text"]
    assert captured[0].args == {"name": "Field", "control_type": "Edit"}
    assert captured[1].args["text"] == "hi"


def test_reduce_events_backspace_edits_buffer() -> None:
    events: queue.Queue[SeriesEvent] = queue.Queue()
    events.put(SeriesEvent("mouse_down"))
    for char in ("a", "b", "\b", "c"):
        events.put(SeriesEvent("input", char))
    events.put(SeriesEvent("stop"))

    captured: list[FlowNode] = []
    _reduce_events(
        events,
        threading.Event(),
        capture_point=lambda x, y: ([], _selector()),
        mouse_position=lambda: (0.0, 0.0),
        emit=captured.append,
    )

    assert captured[-1].args["text"] == "ac"


def test_reduce_events_text_before_any_click_is_dropped() -> None:
    events: queue.Queue[SeriesEvent] = queue.Queue()
    events.put(SeriesEvent("input", "x"))
    events.put(SeriesEvent("stop"))

    captured: list[FlowNode] = []
    _reduce_events(
        events,
        threading.Event(),
        capture_point=lambda x, y: ([], _selector()),
        mouse_position=lambda: (0.0, 0.0),
        emit=captured.append,
    )
    assert captured == []
