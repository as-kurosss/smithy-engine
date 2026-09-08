"""Tests for smithy.windows.selector_rank + strict mode + ranked generation."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smithy.core.errors import ElementNotFound, InvalidInput
from smithy.windows.selector import ElementSelector
from smithy.windows.selector_rank import (
    RankedSelector,
    candidate_configs,
    control_type_display,
    rank_best_selector,
    rank_candidates,
    stability_penalty,
)
from smithy.windows.tools.selector_capture.capture import BestSelector


def _always(n: int) -> Callable[[dict[str, str]], int]:
    return lambda _config: n


class TestControlTypeDisplay:
    def test_numeric_id_translates(self) -> None:
        assert control_type_display("50000") == "button"
        assert control_type_display("50032") == "window"
        assert control_type_display("50031") == "splitbutton"

    def test_alias_id_prefers_first_name(self) -> None:
        assert control_type_display("50004") == "edit"

    def test_valid_name_passes_through(self) -> None:
        assert control_type_display("Button") == "Button"

    def test_missing_returns_none(self) -> None:
        assert control_type_display(None) is None
        assert control_type_display("") is None

    def test_unknown_returns_none(self) -> None:
        assert control_type_display("NoSuchType") is None
        assert control_type_display("99999") is None


class TestCandidateConfigs:
    def test_priority_order_all_fields(self) -> None:
        candidates = candidate_configs(
            name="OK", automation_id="btnOk", control_type="button", class_name="BtnCls"
        )
        strategies = [strategy for strategy, _ in candidates]
        assert strategies == [
            "automation_id",
            "automation_id+type",
            "name+type",
            "name",
            "class+type",
            "class",
            "all_fields",
        ]
        assert candidates[0][1] == {"automation_id": "btnOk"}

    def test_minimal_when_only_name(self) -> None:
        assert candidate_configs(
            name="OK", automation_id=None, control_type=None, class_name=None
        ) == [("name", {"name": "OK"})]

    def test_empty_fields_give_no_candidates(self) -> None:
        assert (
            candidate_configs(name=None, automation_id=None, control_type=None, class_name=None)
            == []
        )

    def test_full_config_not_duplicated(self) -> None:
        candidates = candidate_configs(
            name=None, automation_id="x", control_type=None, class_name=None
        )
        assert candidates == [("automation_id", {"automation_id": "x"})]


class TestStabilityPenalty:
    def test_clean_config_no_penalty(self) -> None:
        penalty, warnings = stability_penalty({"automation_id": "btnOk"})
        assert penalty == 0
        assert warnings == ()

    def test_dynamic_digits_flagged(self) -> None:
        penalty, warnings = stability_penalty({"name": "Invoice 12345"})
        assert penalty >= 30
        assert any("dynamic" in w for w in warnings)

    def test_date_flagged(self) -> None:
        penalty, warnings = stability_penalty({"name": "Report 04.09.2026"})
        assert penalty >= 30
        assert any("dynamic" in w for w in warnings)

    def test_wildcard_chars_flagged(self) -> None:
        penalty, warnings = stability_penalty({"name": "Save *"})
        assert penalty >= 20
        assert any("wildcard" in w for w in warnings)

    def test_hex_automation_id_flagged(self) -> None:
        penalty, warnings = stability_penalty({"automation_id": "btn_a1b2c3d4"})
        assert penalty >= 25
        assert any("hex" in w for w in warnings)

    def test_long_name_flagged(self) -> None:
        penalty, warnings = stability_penalty({"name": "x" * 61})
        assert penalty >= 10
        assert any("long" in w for w in warnings)


class TestRankCandidates:
    def test_unique_automation_id_wins_high(self) -> None:
        ranked = rank_candidates(
            [("automation_id", {"automation_id": "btnOk"})],
            count_matches=_always(1),
        )
        assert ranked.strategy == "automation_id"
        assert ranked.confidence == "high"
        assert ranked.unique is True
        assert ranked.config == {"automation_id": "btnOk"}

    def test_unique_name_type_is_medium(self) -> None:
        ranked = rank_candidates(
            [("name+type", {"name": "OK", "control_type": "button"})],
            count_matches=_always(1),
        )
        assert ranked.confidence == "medium"

    def test_ambiguous_preferred_falls_through(self) -> None:
        def counter(config: dict[str, str]) -> int:
            return 2 if "automation_id" in config else 1

        ranked = rank_candidates(
            [
                ("automation_id", {"automation_id": "dup"}),
                ("name+type", {"name": "OK", "control_type": "button"}),
            ],
            count_matches=counter,
        )
        assert ranked.strategy == "name+type"
        assert ranked.unique is True

    def test_all_ambiguous_gives_low(self) -> None:
        ranked = rank_candidates(
            [
                ("automation_id", {"automation_id": "dup"}),
                ("name", {"name": "OK"}),
            ],
            count_matches=_always(2),
        )
        assert ranked.confidence == "low"
        assert ranked.unique is False
        assert ranked.match_count == 2
        assert ranked.warnings

    def test_empty_candidates_explain(self) -> None:
        ranked = rank_candidates([], count_matches=_always(1))
        assert ranked.confidence == "low"
        assert any("cannot be targeted" in w for w in ranked.warnings)

    def test_counter_error_means_gone(self) -> None:
        def boom(_config: dict[str, str]) -> int:
            raise RuntimeError("uia hiccup")

        ranked = rank_candidates([("name", {"name": "OK"})], count_matches=boom)
        assert ranked.unique is False
        assert ranked.match_count == 0

    def test_result_is_frozen_value(self) -> None:
        ranked = rank_candidates([("name", {"name": "OK"})], count_matches=_always(1))
        assert isinstance(ranked, RankedSelector)
        assert ranked.config == {"name": "OK"}


class TestRankBestSelector:
    def test_numeric_control_type_translated(self) -> None:
        sel = BestSelector(control_type="50000", name="OK", automation_id="btnOk")
        ranked = rank_best_selector(sel, count_matches=_always(1))
        assert ranked.strategy == "automation_id"
        assert ranked.config == {"automation_id": "btnOk"}

    def test_unknown_type_warns_and_drops(self) -> None:
        sel = BestSelector(control_type="Weird", name="OK")
        ranked = rank_best_selector(sel, count_matches=_always(1))
        assert "control_type" not in ranked.config
        assert any("not a known UIA type" in w for w in ranked.warnings)

    def test_dynamic_name_lowers_confidence(self) -> None:
        sel = BestSelector(control_type="Button", name="Invoice 98765")
        ranked = rank_best_selector(sel, count_matches=_always(1))
        assert ranked.confidence in ("medium", "low")
        assert any("dynamic" in w for w in ranked.warnings)

    def test_no_fields_low_with_warning(self) -> None:
        ranked = rank_best_selector(BestSelector(control_type="Unknown"), count_matches=_always(1))
        assert ranked.confidence == "low"


class _FakeNode:
    """Minimal UIA-like node for count_from_desktop tests."""

    def __init__(
        self,
        name: str = "",
        automation_id: str = "",
        control_type: int = 0,
        class_name: str = "",
        pid: int = 0,
    ) -> None:
        self.Name = name
        self.AutomationId = automation_id
        self.ControlType = control_type
        self.ClassName = class_name
        self.ProcessId = pid
        self._children: list[_FakeNode] = []
        self._sibling: _FakeNode | None = None

    def add(self, child: _FakeNode) -> _FakeNode:
        self._children.append(child)
        return child

    def GetFirstChildControl(self) -> Any:  # noqa: N802 — mirrors UIA API names
        return self._children[0] if self._children else None

    def GetNextSiblingControl(self) -> Any:  # noqa: N802 — mirrors UIA API names
        return self._sibling


def _tree_with_siblings() -> _FakeNode:
    """Root with two OK buttons (one nested) + wire sibling links."""
    root = _FakeNode(name="Desktop")
    first = root.add(_FakeNode(name="OK", control_type=50000))
    second = _FakeNode(name="Cancel", control_type=50000)
    root._children.append(second)
    nested = _FakeNode(name="OK", control_type=50000)
    second._children.append(nested)

    first._sibling = second
    return root


class TestCountFromDesktop:
    def _selector(self) -> ElementSelector:
        return ElementSelector().with_name("OK").with_control_type("Button")

    def _run(self, selector: ElementSelector, root: _FakeNode, limit: int) -> int:
        fake = ModuleType("uiautomation")
        fake.GetRootControl = lambda: root  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"uiautomation": fake}):
            return selector.count_from_desktop(limit=limit)

    def test_counts_capped_at_limit(self) -> None:
        assert self._run(self._selector(), _tree_with_siblings(), limit=2) == 2

    def test_limit_one_stops_early(self) -> None:
        assert self._run(self._selector(), _tree_with_siblings(), limit=1) == 1

    def test_zero_when_missing(self) -> None:
        selector = ElementSelector().with_name("Nope")
        assert self._run(selector, _tree_with_siblings(), limit=2) == 0

    def test_flaky_nodes_skipped(self) -> None:
        root = _FakeNode(name="Desktop")

        class Flaky(_FakeNode):
            def __getattribute__(self, attr: str) -> Any:
                if attr == "Name":
                    raise RuntimeError("com error")
                return super().__getattribute__(attr)

        flaky = Flaky(name="Flaky")
        ok = _FakeNode(name="OK", control_type=50000)
        flaky._sibling = ok
        root._children.append(flaky)
        root._children.append(ok)
        assert self._run(self._selector(), root, limit=2) == 1


class TestStrictResolve:
    @pytest.mark.asyncio
    async def test_ambiguous_raises_invalid_input(self) -> None:
        from smithy.windows.tools._resolve import resolve_element

        with (
            patch.object(ElementSelector, "count_from_desktop", return_value=2),
            pytest.raises(InvalidInput, match="Ambiguous"),
        ):
            await resolve_element({"name": "OK"}, strict=True)

    @pytest.mark.asyncio
    async def test_missing_raises_not_found(self) -> None:
        from smithy.windows.tools._resolve import resolve_element

        with (
            patch.object(ElementSelector, "count_from_desktop", return_value=0),
            pytest.raises(ElementNotFound),
        ):
            await resolve_element({"name": "OK"}, strict=True)

    @pytest.mark.asyncio
    async def test_unique_resolves_normally(self) -> None:
        from smithy.windows.tools._resolve import resolve_element

        element = MagicMock()
        with (
            patch.object(ElementSelector, "count_from_desktop", return_value=1),
            patch.object(ElementSelector, "find_from_desktop", return_value=element),
        ):
            assert await resolve_element({"name": "OK"}, strict=True) is element

    @pytest.mark.asyncio
    async def test_non_strict_skips_count(self) -> None:
        from smithy.windows.tools._resolve import resolve_element

        element = MagicMock()
        with (
            patch.object(ElementSelector, "count_from_desktop", new=MagicMock()) as count,
            patch.object(ElementSelector, "find_from_desktop", return_value=element),
        ):
            assert await resolve_element({"name": "OK"}) is element
        count.assert_not_called()


class TestRankedGeneration:
    def test_click_uses_ranked_config(self) -> None:
        from smithy.windows.tools.selector_capture.generate import (
            GenerateParams,
            ToolType,
            generate_nodes_from_config,
        )

        nodes = generate_nodes_from_config(
            {"automation_id": "btnOk"}, ToolType.CLICK, GenerateParams()
        )
        assert len(nodes) == 1
        assert nodes[0].tool == "windows.click"
        assert nodes[0].args == {"automation_id": "btnOk"}

    def test_input_text_adds_text(self) -> None:
        from smithy.windows.tools.selector_capture.generate import (
            GenerateParams,
            ToolType,
            generate_nodes_from_config,
        )

        nodes = generate_nodes_from_config(
            {"automation_id": "btnOk"},
            ToolType.INPUT_TEXT,
            GenerateParams(text="hi"),
        )
        assert nodes[0].args == {"automation_id": "btnOk", "text": "hi"}

    def test_wait_ignores_selector(self) -> None:
        from smithy.windows.tools.selector_capture.generate import (
            GenerateParams,
            ToolType,
            generate_nodes_from_config,
        )

        nodes = generate_nodes_from_config(
            {"automation_id": "btnOk"},
            ToolType.WAIT,
            GenerateParams(duration_ms=500),
        )
        assert nodes[0].args == {"timeout_ms": 500}

    def test_inline_selector_translates_numeric_type(self) -> None:
        from smithy.windows.tools.selector_capture.generate import build_inline_selector

        cfg = build_inline_selector(BestSelector(control_type="50000", name="OK"))
        assert cfg["control_type"] == "button"
        assert cfg["name"] == "OK"

    def test_inline_selector_drops_unknown_type(self) -> None:
        from smithy.windows.tools.selector_capture.generate import build_inline_selector

        cfg = build_inline_selector(BestSelector(control_type="99999", name="OK"))
        assert "control_type" not in cfg
        assert cfg["name"] == "OK"


class TestRankCapturedFallback:
    def test_rank_failure_returns_none(self) -> None:
        from smithy.windows.tools.selector_capture.recorder import _rank_captured

        sel = BestSelector(control_type="Button", name="OK")
        with patch(
            "smithy.windows.selector_rank.rank_best_selector",
            side_effect=RuntimeError("uia down"),
        ):
            assert _rank_captured(sel) is None
