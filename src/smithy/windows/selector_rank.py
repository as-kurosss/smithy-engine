"""Selector ranking — pick the best selector for a captured element.

Desktop equivalent of Playwright's codegen selector engine: given a
captured element, generate candidate selectors in priority order
(automation ID → name + type → class + type), score each for stability,
verify uniqueness against the live desktop (strict mode — exactly one
match), and return the winner with a confidence level.

Honesty rule: when nothing is unique and stable, the result carries
``confidence=\"low\"`` with warnings instead of a made-up stable
selector — fix it with an anchor, don't trust the output.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from smithy.windows.selector import (
    _CONTROL_TYPE_MAP,
    ElementSelector,
    parse_control_type,
)

Confidence = Literal["high", "medium", "low"]

# Score thresholds for confidence levels (applied to unique selectors).
_HIGH_SCORE = 90
_MEDIUM_SCORE = 50

# Base scores per strategy (priority order = order in candidate_configs).
_BASE_SCORES = {
    "automation_id": 100,
    "automation_id+type": 90,
    "name+type": 70,
    "name": 60,
    "class+type": 40,
    "class": 30,
    "all_fields": 10,
}

_DYNAMIC_DIGITS = re.compile(r"\d{4,}")
_DATE_LIKE = re.compile(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}")
_HEX_RUN = re.compile(r"[0-9a-fA-F]{6,}")
_LONG_NAME = 60


@dataclass(frozen=True, slots=True)
class RankedSelector:
    """The winning selector plus how much to trust it."""

    config: dict[str, str] = field(default_factory=dict)
    strategy: str = "all_fields"
    score: int = 0
    confidence: Confidence = "low"
    unique: bool = False
    match_count: int = 0
    warnings: tuple[str, ...] = ()


def control_type_display(raw: str | None) -> str | None:
    """Translate a captured control type into a config-ready name.

    Real captures carry the numeric UIA id (``\"50000\"``) while tool
    configs need the name (``\"Button\"``) — passing the number through
    makes the runtime reject the selector. Returns ``None`` when *raw*
    is missing or untranslatable (callers drop the field then).
    """
    if not raw:
        return None
    if raw in _ID_TO_NAME:
        return _ID_TO_NAME[raw]
    if parse_control_type(raw) is not None:
        return raw
    return None


def candidate_configs(
    *,
    name: str | None,
    automation_id: str | None,
    control_type: str | None,
    class_name: str | None,
) -> list[tuple[str, dict[str, str]]]:
    """Build candidate selector configs in priority order.

    Minimal first (fewer fields = fewer ways to break across runs);
    ``all_fields`` last as the maximally-specific fallback.
    """
    candidates: list[tuple[str, dict[str, str]]] = []
    if automation_id:
        candidates.append(("automation_id", {"automation_id": automation_id}))
        if control_type:
            candidates.append(
                (
                    "automation_id+type",
                    {"automation_id": automation_id, "control_type": control_type},
                )
            )
    if name:
        if control_type:
            candidates.append(("name+type", {"name": name, "control_type": control_type}))
        candidates.append(("name", {"name": name}))
    if class_name:
        if control_type:
            candidates.append(
                ("class+type", {"class_name": class_name, "control_type": control_type})
            )
        candidates.append(("class", {"class_name": class_name}))
    full: dict[str, str] = {}
    if automation_id:
        full["automation_id"] = automation_id
    if name:
        full["name"] = name
    if control_type:
        full["control_type"] = control_type
    if class_name:
        full["class_name"] = class_name
    if full and all(config != full for _, config in candidates):
        candidates.append(("all_fields", full))
    return candidates


def stability_penalty(config: dict[str, str]) -> tuple[int, tuple[str, ...]]:
    """Static (no-UIA) stability scoring for a candidate config.

    Returns:
        ``(penalty, warnings)`` — penalty subtracted from the base score.
    """
    penalty = 0
    warnings: list[str] = []
    name = config.get("name")
    if name is not None:
        if _DYNAMIC_DIGITS.search(name) or _DATE_LIKE.search(name):
            penalty += 30
            warnings.append(f"name {name!r} looks dynamic (digits/date) — may change per run")
        if len(name) > _LONG_NAME:
            penalty += 10
            warnings.append(f"name {name!r} is long — brittle if the text shifts")
        if "*" in name or "?" in name:
            penalty += 20
            warnings.append(
                f"name {name!r} contains wildcard chars — matched as a glob, not literally"
            )
    automation_id = config.get("automation_id")
    if automation_id is not None and _HEX_RUN.search(automation_id):
        penalty += 25
        warnings.append(
            f"automation_id {automation_id!r} contains a hex run — possibly generated per build"
        )
    class_name = config.get("class_name")
    if class_name is not None and _HEX_RUN.search(class_name):
        penalty += 15
        warnings.append(
            f"class_name {class_name!r} contains a hex run — possibly generated per build"
        )
    return penalty, tuple(warnings)


def rank_candidates(
    candidates: list[tuple[str, dict[str, str]]],
    *,
    count_matches: Callable[[dict[str, str]], int],
) -> RankedSelector:
    """Score candidates and return the winner.

    Each candidate is checked for uniqueness via *count_matches* (matches
    capped at 2 — 0 = gone, 1 = unique, 2 = ambiguous). Non-unique
    candidates are heavily penalised but still comparable, so an
    ambiguous world still yields a deterministic answer with ``low``
    confidence instead of nothing.
    """
    best: RankedSelector | None = None
    for strategy, config in candidates:
        base = _BASE_SCORES.get(strategy, 0)
        penalty, stability_warnings = stability_penalty(config)
        try:
            matches = count_matches(config)
        except Exception:
            matches = 0
        unique = matches == 1
        warnings = list(stability_warnings)
        if not unique:
            penalty += 50
            warnings.append(f"selector matches {matches} element(s) — needs exactly 1")
        score = max(0, base - penalty)
        if unique and score >= _HIGH_SCORE:
            confidence: Confidence = "high"
        elif unique and score >= _MEDIUM_SCORE:
            confidence = "medium"
        else:
            confidence = "low"
            if unique and not warnings:
                warnings.append("unique but fragile — prefer automation_id")
        ranked = RankedSelector(
            config=dict(config),
            strategy=strategy,
            score=score,
            confidence=confidence,
            unique=unique,
            match_count=matches,
            warnings=tuple(warnings),
        )
        if best is None or ranked.score > best.score:
            best = ranked
    if best is None:
        return RankedSelector(warnings=("no identifying fields — element cannot be targeted",))
    return best


def rank_best_selector(
    selector: Any,
    *,
    count_matches: Callable[[dict[str, str]], int] | None = None,
) -> RankedSelector:
    """Rank selector candidates for a captured element.

    Args:
        selector: A :class:`BestSelector` (or :class:`PathNode` — only the
            target fields are read) with ``name`` / ``automation_id`` /
            ``control_type`` / ``class_name`` attributes.
        count_matches: Uniqueness probe ``config -> matches (cap 2)``.
            Defaults to the live desktop via :class:`ElementSelector`.

    Returns:
        The winning :class:`RankedSelector`.
    """
    raw_type = getattr(selector, "control_type", None)
    control_type = control_type_display(raw_type if isinstance(raw_type, str) else None)
    type_warnings: list[str] = []
    if raw_type and control_type is None:
        type_warnings.append(
            f"control_type {raw_type!r} is not a known UIA type — dropped from candidates"
        )
    candidates = candidate_configs(
        name=getattr(selector, "name", None),
        automation_id=getattr(selector, "automation_id", None),
        control_type=control_type,
        class_name=getattr(selector, "class_name", None),
    )
    probe = count_matches if count_matches is not None else _uia_count_matches
    ranked = rank_candidates(candidates, count_matches=probe)
    if type_warnings:
        ranked = RankedSelector(
            config=ranked.config,
            strategy=ranked.strategy,
            score=ranked.score,
            confidence=ranked.confidence,
            unique=ranked.unique,
            match_count=ranked.match_count,
            warnings=tuple(type_warnings) + ranked.warnings,
        )
    return ranked


def _uia_count_matches(config: dict[str, str]) -> int:
    """Count live desktop matches for *config* (cap 2)."""
    selector = ElementSelector.from_config(dict(config))
    if selector is None:
        return 0
    return selector.count_from_desktop(limit=2)


def _build_id_to_name() -> dict[str, str]:
    """Reverse UIA id → name map (first name wins, so ``edit`` beats alias ``text``)."""
    reverse: dict[str, str] = {}
    for type_name, type_id in _CONTROL_TYPE_MAP.items():
        reverse.setdefault(str(type_id), type_name)
    return reverse


_ID_TO_NAME = _build_id_to_name()
