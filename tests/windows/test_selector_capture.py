"""Tests for selector_capture — BestSelector, PathNode, ToolType, generate module.

Ported from the Rust ``selector-capture/src/generate.rs`` test suite and
adapted to the existing pytest style in ``tests/windows/test_selector.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from smithcore.windows.tools.selector_capture.capture import (
    BestSelector,
    CaptureRecord,
    PathNode,
    capture_at_point,
    path_to_dicts,
    read_node,
)
from smithcore.windows.tools.selector_capture.generate import (
    FlowNode,
    GenerateParams,
    ToolType,
    build_inline_selector,
    generate_action_config,
    generate_nodes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_selector() -> BestSelector:
    """Full-selector fixture matching the Rust ``sample_selector()``."""
    return BestSelector(
        control_type="Button",
        name="Submit",
        class_name="ButtonClass",
        automation_id="btnSubmit",
    )


def _make_mock_element(
    *,
    control_type: int = 50000,
    name: str = "",
    class_name: str = "",
    automation_id: str = "",
) -> MagicMock:
    """Create a MagicMock mimicking a ``uiautomation`` control object."""
    el = MagicMock()
    el.ControlType = control_type
    el.Name = name
    el.ClassName = class_name
    el.AutomationId = automation_id
    return el


# ===================================================================
# BestSelector
# ===================================================================


class TestBestSelector:
    """Tests for :class:`BestSelector` — label(), to_dict(), has_any()."""

    def test_label_returns_name_when_present(self) -> None:
        sel = _sample_selector()
        assert sel.label() == "Submit"

    def test_label_falls_back_to_control_type(self) -> None:
        sel = BestSelector(control_type="Pane")
        assert sel.label() == "Pane"

    def test_label_prefers_name_over_control_type(self) -> None:
        sel = BestSelector(control_type="Window", name="Main")
        assert sel.label() == "Main"

    def test_to_dict_full(self) -> None:
        sel = _sample_selector()
        d = sel.to_dict()
        assert d == {
            "control_type": "Button",
            "name": "Submit",
            "class_name": "ButtonClass",
            "automation_id": "btnSubmit",
        }

    def test_to_dict_omits_none_values(self) -> None:
        sel = BestSelector(control_type="Edit")
        d = sel.to_dict()
        assert d == {"control_type": "Edit"}
        assert "name" not in d
        assert "class_name" not in d
        assert "automation_id" not in d

    def test_to_dict_always_includes_control_type(self) -> None:
        sel = BestSelector(control_type="Pane", name="x")
        assert "control_type" in sel.to_dict()

    def test_has_any_with_all_fields(self) -> None:
        assert _sample_selector().has_any() is True

    def test_has_any_with_only_control_type(self) -> None:
        sel = BestSelector(control_type="Button")
        assert sel.has_any() is True

    def test_has_any_empty_string_control_type(self) -> None:
        sel = BestSelector(control_type="")
        assert sel.has_any() is False

    def test_has_any_with_name_only(self) -> None:
        sel = BestSelector(control_type="", name="OK")
        assert sel.has_any() is True

    def test_has_any_with_automation_id_only(self) -> None:
        sel = BestSelector(control_type="", automation_id="id1")
        assert sel.has_any() is True

    def test_has_any_with_class_name_only(self) -> None:
        sel = BestSelector(control_type="", class_name="Cls")
        assert sel.has_any() is True


# ===================================================================
# PathNode
# ===================================================================


class TestPathNode:
    """Tests for :class:`PathNode` — creation and serialisation."""

    def test_creation_full(self) -> None:
        node = PathNode(
            control_type="Button",
            class_name="BtnCls",
            name="OK",
            automation_id="btnOk",
        )
        assert node.control_type == "Button"
        assert node.class_name == "BtnCls"
        assert node.name == "OK"
        assert node.automation_id == "btnOk"

    def test_creation_minimal(self) -> None:
        node = PathNode(control_type="Pane")
        assert node.control_type == "Pane"
        assert node.class_name is None
        assert node.name is None
        assert node.automation_id is None

    def test_frozen(self) -> None:
        node = PathNode(control_type="Button")
        with pytest.raises(AttributeError):
            node.control_type = "Edit"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = PathNode(control_type="Button", name="OK")
        b = PathNode(control_type="Button", name="OK")
        assert a == b

    def test_inequality(self) -> None:
        a = PathNode(control_type="Button", name="OK")
        b = PathNode(control_type="Button", name="Cancel")
        assert a != b

    def test_creation_with_extended_fields(self) -> None:
        node = PathNode(
            control_type="Button",
            name="OK",
            framework_id="WPF",
            is_enabled="True",
            process_id="1234",
            bounding_rectangle="0,0,100,50",
        )
        assert node.framework_id == "WPF"
        assert node.is_enabled == "True"
        assert node.process_id == "1234"
        assert node.bounding_rectangle == "0,0,100,50"
        # Extended fields default to None
        assert node.aria_role is None
        assert node.help_text is None

    def test_as_dict_manual(self) -> None:
        """PathNode is frozen; verify all fields are accessible for dict conversion."""
        node = PathNode(
            control_type="Edit",
            class_name="EditCls",
            name="Field",
            automation_id="ed1",
        )
        d = {
            "control_type": node.control_type,
            "class_name": node.class_name,
            "name": node.name,
            "automation_id": node.automation_id,
        }
        assert d == {
            "control_type": "Edit",
            "class_name": "EditCls",
            "name": "Field",
            "automation_id": "ed1",
        }


class TestPathToDicts:
    """Tests for :func:`path_to_dicts` — PathNode list to dict list conversion."""

    def test_full_path(self) -> None:
        path = [
            PathNode(control_type="Window", name="Main"),
            PathNode(
                control_type="Button",
                name="OK",
                class_name="BtnCls",
                automation_id="btnOk",
            ),
        ]
        result = path_to_dicts(path)
        assert len(result) == 2
        assert result[0] == {"control_type": "Window", "name": "Main"}
        assert result[1] == {
            "control_type": "Button",
            "name": "OK",
            "class_name": "BtnCls",
            "automation_id": "btnOk",
        }

    def test_empty_path(self) -> None:
        assert path_to_dicts([]) == []

    def test_omits_none_values(self) -> None:
        path = [PathNode(control_type="Pane")]
        result = path_to_dicts(path)
        assert result == [{"control_type": "Pane"}]
        assert "name" not in result[0]
        assert "class_name" not in result[0]
        assert "automation_id" not in result[0]
        assert "framework_id" not in result[0]

    def test_includes_extended_fields(self) -> None:
        path = [
            PathNode(
                control_type="Button",
                name="OK",
                framework_id="WPF",
                is_enabled="True",
                bounding_rectangle="0,0,100,50",
            ),
        ]
        result = path_to_dicts(path)
        assert result[0]["framework_id"] == "WPF"
        assert result[0]["is_enabled"] == "True"
        assert result[0]["bounding_rectangle"] == "0,0,100,50"


# ===================================================================
# CaptureRecord
# ===================================================================


class TestCaptureRecord:
    """Tests for :class:`CaptureRecord` defaults and construction."""

    def test_default_id_is_hex(self) -> None:
        rec = CaptureRecord()
        assert len(rec.id) == 32
        int(rec.id, 16)  # must not raise

    def test_default_timestamp_is_iso(self) -> None:
        rec = CaptureRecord()
        assert "T" in rec.timestamp

    def test_default_description_is_none(self) -> None:
        rec = CaptureRecord()
        assert rec.description is None

    def test_custom_fields(self) -> None:
        sel = BestSelector(control_type="Button")
        node = PathNode(control_type="Window")
        rec = CaptureRecord(
            id="abc123",
            timestamp="2025-01-01T00:00:00+00:00",
            description="test",
            full_path=[node],
            best_selector=sel,
        )
        assert rec.id == "abc123"
        assert rec.description == "test"
        assert len(rec.full_path) == 1
        assert rec.best_selector is sel


# ===================================================================
# ToolType (StrEnum)
# ===================================================================


class TestToolType:
    """Tests for :class:`ToolType` — enum values and string conversion."""

    def test_all_values(self) -> None:
        assert ToolType.CLICK == "click"
        assert ToolType.INPUT_TEXT == "input_text"
        assert ToolType.SET_TEXT == "set_text"
        assert ToolType.WAIT == "wait"

    def test_from_string_valid(self) -> None:
        assert ToolType("click") is ToolType.CLICK
        assert ToolType("input_text") is ToolType.INPUT_TEXT
        assert ToolType("set_text") is ToolType.SET_TEXT
        assert ToolType("wait") is ToolType.WAIT

    def test_from_string_invalid(self) -> None:
        with pytest.raises(ValueError):
            ToolType("nonexistent")

    def test_needs_text(self) -> None:
        assert ToolType.INPUT_TEXT.needs_text is True
        assert ToolType.SET_TEXT.needs_text is True
        assert ToolType.CLICK.needs_text is False
        assert ToolType.WAIT.needs_text is False

    def test_needs_duration(self) -> None:
        assert ToolType.WAIT.needs_duration is True
        assert ToolType.CLICK.needs_duration is False

    def test_needs_selector(self) -> None:
        assert ToolType.CLICK.needs_selector is True
        assert ToolType.INPUT_TEXT.needs_selector is True
        assert ToolType.SET_TEXT.needs_selector is True
        assert ToolType.WAIT.needs_selector is False


# ===================================================================
# build_inline_selector
# ===================================================================


class TestBuildInlineSelector:
    """Tests for selector priority in :func:`build_inline_selector`."""

    def test_full_selector(self) -> None:
        sel = _sample_selector()
        cfg = build_inline_selector(sel)
        assert cfg["name"] == "Submit"
        assert cfg["automation_id"] == "btnSubmit"
        assert cfg["control_type"] == "Button"
        assert cfg["class_name"] == "ButtonClass"

    def test_without_automation_id(self) -> None:
        sel = BestSelector(
            control_type="Button",
            name="Submit",
            class_name="BtnCls",
            automation_id=None,
        )
        cfg = build_inline_selector(sel)
        assert cfg["name"] == "Submit"
        assert cfg["control_type"] == "Button"
        assert "automation_id" not in cfg

    def test_custom_control_type_excluded(self) -> None:
        sel = BestSelector(
            control_type="Custom",
            name="MyElement",
            automation_id=None,
        )
        cfg = build_inline_selector(sel)
        assert cfg["name"] == "MyElement"
        assert "control_type" not in cfg

    def test_empty_control_type_excluded(self) -> None:
        sel = BestSelector(control_type="")
        cfg = build_inline_selector(sel)
        assert "control_type" not in cfg

    def test_minimal_selector(self) -> None:
        sel = BestSelector(control_type="Pane")
        cfg = build_inline_selector(sel)
        assert cfg == {"control_type": "Pane"}

    def test_with_text(self) -> None:
        sel = _sample_selector()
        cfg = build_inline_selector(sel, text="Hello")
        assert cfg["text"] == "Hello"
        assert cfg["name"] == "Submit"

    def test_empty_text_omitted(self) -> None:
        sel = _sample_selector()
        cfg = build_inline_selector(sel)
        assert "text" not in cfg


# ===================================================================
# generate_nodes
# ===================================================================


class TestGenerateNodes:
    """Tests for :func:`generate_nodes` — one node per tool type."""

    def test_click_produces_one_node(self) -> None:
        sel = _sample_selector()
        params = GenerateParams()
        nodes = generate_nodes(sel, ToolType.CLICK, params)
        assert len(nodes) == 1
        assert nodes[0].tool == "windows.click"
        assert nodes[0].args["automation_id"] == "btnSubmit"
        assert nodes[0].args["name"] == "Submit"

    def test_input_text_produces_one_node(self) -> None:
        sel = _sample_selector()
        params = GenerateParams(text="Hello")
        nodes = generate_nodes(sel, ToolType.INPUT_TEXT, params)
        assert len(nodes) == 1
        assert nodes[0].tool == "windows.input_text"
        assert nodes[0].args["text"] == "Hello"
        assert nodes[0].args["automation_id"] == "btnSubmit"

    def test_set_text_produces_one_node(self) -> None:
        sel = _sample_selector()
        params = GenerateParams(text="test")
        nodes = generate_nodes(sel, ToolType.SET_TEXT, params)
        assert len(nodes) == 1
        assert nodes[0].tool == "windows.set_text"
        assert nodes[0].args["text"] == "test"

    def test_wait_produces_one_node(self) -> None:
        sel = _sample_selector()
        params = GenerateParams(duration_ms=2000)
        nodes = generate_nodes(sel, ToolType.WAIT, params)
        assert len(nodes) == 1
        assert nodes[0].tool == "windows.wait"
        assert nodes[0].args["timeout_ms"] == 2000

    def test_click_node_contains_all_selector_fields(self) -> None:
        sel = _sample_selector()
        params = GenerateParams()
        nodes = generate_nodes(sel, ToolType.CLICK, params)
        args = nodes[0].args
        assert args["automation_id"] == "btnSubmit"
        assert args["name"] == "Submit"
        assert args["control_type"] == "Button"
        assert args["class_name"] == "ButtonClass"


# ===================================================================
# generate_action_config (without output_key)
# ===================================================================


class TestGenerateActionConfig:
    """Tests for :func:`generate_action_config` — action config only."""

    def test_click_action_has_inline_selectors(self) -> None:
        sel = _sample_selector()
        params = GenerateParams()
        cfg = generate_action_config(sel, ToolType.CLICK, params)
        assert cfg["automation_id"] == "btnSubmit"
        assert cfg["name"] == "Submit"
        assert cfg["control_type"] == "Button"

    def test_input_text_action(self) -> None:
        sel = _sample_selector()
        params = GenerateParams(text="Hello")
        cfg = generate_action_config(sel, ToolType.INPUT_TEXT, params)
        assert cfg["text"] == "Hello"
        assert cfg["automation_id"] == "btnSubmit"

    def test_set_text_action(self) -> None:
        sel = _sample_selector()
        params = GenerateParams(text="data")
        cfg = generate_action_config(sel, ToolType.SET_TEXT, params)
        assert cfg["text"] == "data"
        assert cfg["automation_id"] == "btnSubmit"

    def test_wait_action(self) -> None:
        sel = _sample_selector()
        params = GenerateParams(duration_ms=500)
        cfg = generate_action_config(sel, ToolType.WAIT, params)
        assert cfg == {"timeout_ms": 500}

    def test_click_action_no_output_key(self) -> None:
        sel = BestSelector(control_type="Pane")
        params = GenerateParams()
        cfg = generate_action_config(sel, ToolType.CLICK, params)
        assert "output_key" not in cfg


# ===================================================================
# read_node (UIA element → PathNode)
# ===================================================================


class TestReadNode:
    """Tests for :func:`read_node` — UIA element property extraction."""

    def test_full_properties(self) -> None:
        el = _make_mock_element(
            control_type=50000,
            name="OK",
            class_name="BtnCls",
            automation_id="btnOk",
        )
        node = read_node(el)
        assert node.control_type == "50000"
        assert node.name == "OK"
        assert node.class_name == "BtnCls"
        assert node.automation_id == "btnOk"

    def test_empty_strings_become_none(self) -> None:
        el = _make_mock_element(control_type=50000, name="", class_name="", automation_id="")
        node = read_node(el)
        assert node.name is None
        assert node.class_name is None
        assert node.automation_id is None

    def test_zero_control_type_becomes_unknown(self) -> None:
        el = _make_mock_element(control_type=0)
        node = read_node(el)
        assert node.control_type == "Unknown"

    def test_get_control_type_exception(self) -> None:
        from unittest.mock import PropertyMock

        el = MagicMock()
        type(el).ControlType = PropertyMock(side_effect=Exception("COM error"))
        el.Name = "X"
        el.ClassName = "Y"
        el.AutomationId = "Z"
        node = read_node(el)
        assert node.control_type == "Unknown"


# ===================================================================
# best_selector_from_path
# ===================================================================


class TestBestSelectorFromPath:
    """Tests for :func:`best_selector_from_path` extraction logic."""

    def test_extracts_last_node(self) -> None:
        from smithcore.windows.tools.selector_capture.capture import (
            best_selector_from_path,
        )

        path = [
            PathNode(control_type="Window", name="Main"),
            PathNode(
                control_type="Button",
                name="OK",
                automation_id="btnOk",
                class_name="BtnCls",
            ),
        ]
        sel = best_selector_from_path(path)
        assert sel.control_type == "Button"
        assert sel.name == "OK"
        assert sel.automation_id == "btnOk"

    def test_empty_path_raises(self) -> None:
        from smithcore.windows.tools.selector_capture.capture import (
            best_selector_from_path,
        )

        with pytest.raises(ValueError, match="empty"):
            best_selector_from_path([])


# ===================================================================
# capture_at_point (with mocked UIA)
# ===================================================================


class TestCaptureAtPoint:
    """Tests for :func:`capture_at_point` with mocked ``uiautomation``."""

    def test_captures_element_at_coordinates(self) -> None:
        mock_root = MagicMock()
        mock_root.ControlType = 50031
        mock_root.Name = "Desktop"
        mock_root.ClassName = ""
        mock_root.AutomationId = ""

        mock_child = MagicMock()
        mock_child.ControlType = 50000
        mock_child.Name = "OK"
        mock_child.ClassName = "BtnCls"
        mock_child.AutomationId = "btnOk"
        mock_child.BoundingRectangle = MagicMock(
            left=100,
            top=200,
            right=300,
            bottom=400,
        )
        mock_child.GetFirstChildControl.return_value = None
        mock_child.GetNextSiblingControl.return_value = None
        mock_child.GetParentControl.return_value = mock_root

        mock_root.GetFirstChildControl.return_value = mock_child
        mock_root.GetNextSiblingControl.return_value = None
        mock_root.GetParentControl.return_value = None

        mock_auto = MagicMock()
        mock_auto.GetRootControl.return_value = mock_root

        with patch(
            "smithcore.windows.tools.selector_capture.capture.auto",
            mock_auto,
        ):
            path, selector = capture_at_point(200, 300)

        assert len(path) == 2
        assert path[0].control_type == "50031"
        assert path[1].control_type == "50000"
        assert path[1].name == "OK"
        assert selector.control_type == "50000"
        assert selector.name == "OK"

        # Verify full_path can be serialised
        dicts = path_to_dicts(path)
        assert len(dicts) == 2
        assert dicts[1]["automation_id"] == "btnOk"

    def test_root_none_raises(self) -> None:
        mock_auto = MagicMock()
        mock_auto.GetRootControl.return_value = None

        with (
            patch(
                "smithcore.windows.tools.selector_capture.capture.auto",
                mock_auto,
            ),
            pytest.raises(RuntimeError, match="root"),
        ):
            capture_at_point(100, 100)

    def test_single_node_path(self) -> None:
        """When root itself is the deepest element (no children)."""
        mock_root = MagicMock()
        mock_root.ControlType = 50031
        mock_root.Name = "Desktop"
        mock_root.ClassName = ""
        mock_root.AutomationId = ""
        mock_root.GetFirstChildControl.return_value = None
        mock_root.GetParentControl.return_value = None

        mock_auto = MagicMock()
        mock_auto.GetRootControl.return_value = mock_root

        with patch(
            "smithcore.windows.tools.selector_capture.capture.auto",
            mock_auto,
        ):
            path, selector = capture_at_point(0, 0)

        assert len(path) == 1
        assert selector.control_type == "50031"


class TestFlowNodeFullPath:
    """Tests for :class:`FlowNode` full_path field."""

    def test_default_full_path_is_empty(self) -> None:
        node = FlowNode(tool="windows.click", args={"name": "OK"})
        assert node.full_path == []

    def test_full_path_can_be_set(self) -> None:
        fp = [{"control_type": "Window", "name": "Main"}]
        node = FlowNode(tool="windows.click", args={}, full_path=fp)
        assert node.full_path == fp


# ===================================================================
# Recorder output unification (single/series/record → nodes)
# ===================================================================


class TestNodesForCapture:
    """Tests for :func:`_nodes_for_capture` — one capture, one node shape."""

    def test_ranked_config_used_with_full_path(self) -> None:
        from smithcore.windows.selector_rank import RankedSelector
        from smithcore.windows.tools.selector_capture.recorder import (
            _nodes_for_capture,
        )

        ranked = RankedSelector(
            config={"name": "OK", "control_type": "button"},
            strategy="name+type",
            score=70,
            confidence="medium",
            unique=True,
            match_count=1,
        )
        nodes = _nodes_for_capture(
            BestSelector(control_type="50000", name="OK"),
            [PathNode(control_type="Window", name="Main")],
            ToolType.CLICK,
            GenerateParams(),
            ranked,
        )
        assert len(nodes) == 1
        assert nodes[0].tool == "windows.click"
        assert nodes[0].args == {"name": "OK", "control_type": "button"}
        assert nodes[0].full_path == [{"control_type": "Window", "name": "Main"}]

    def test_fallback_unranked_translates_numeric_type(self) -> None:
        from smithcore.windows.tools.selector_capture.recorder import (
            _nodes_for_capture,
        )

        nodes = _nodes_for_capture(
            BestSelector(control_type="50000", name="OK"),
            [PathNode(control_type="Window")],
            ToolType.CLICK,
            GenerateParams(),
            None,
        )
        assert nodes[0].args == {"name": "OK", "control_type": "button"}
        assert nodes[0].full_path == [{"control_type": "Window"}]

    def test_empty_path_gives_empty_full_path(self) -> None:
        from smithcore.windows.selector_rank import RankedSelector
        from smithcore.windows.tools.selector_capture.recorder import (
            _nodes_for_capture,
        )

        ranked = RankedSelector(
            config={"name": "OK"},
            strategy="name",
            score=60,
            confidence="medium",
            unique=True,
            match_count=1,
        )
        nodes = _nodes_for_capture(
            BestSelector(control_type="", name="OK"),
            None,
            ToolType.CLICK,
            GenerateParams(),
            ranked,
        )
        assert nodes[0].full_path == []


class TestSeriesInputFlush:
    """Tests for :func:`_input_text_nodes` — keyboard flushes keep full_path."""

    def test_input_nodes_keep_full_path(self) -> None:
        from smithcore.windows.tools.selector_capture.recorder import (
            _input_text_nodes,
        )

        sel = BestSelector(
            control_type="Edit",
            name="Field",
            class_name="EditCls",
            automation_id="ed1",
        )
        fp = [{"control_type": "Window", "name": "Main"}]
        nodes = _input_text_nodes(sel, fp)
        assert len(nodes) == 1
        assert nodes[0].tool == "windows.input_text"
        assert nodes[0].full_path == fp
        assert nodes[0].args["automation_id"] == "ed1"
        # Series mode records the target, not the pressed keys.
        assert "text" not in nodes[0].args
