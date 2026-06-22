"""Compiler pass ordering and feature-compatibility matrix.

Covers ``compiler/passes.py``: the declared pass order, the totals
incompatibility metadata, and :func:`evaluate_compatibility` across the
combinations of grouping / totals / PoP / cumulative / window that the
pipeline previously handled with inline ``if`` blocks. The end-to-end SQL
equality is covered by the existing drift and compilation tests; these
tests lock the orchestration contract itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from orionbelt.compiler.passes import (
    PASS_CUMULATIVE,
    PASS_FILTER_CONTEXT,
    PASS_HAVING_CLEANUP,
    PASS_PERIOD_OVER_PERIOD,
    PASS_TOTALS,
    PASS_WINDOW,
    build_default_passes,
    evaluate_compatibility,
)
from orionbelt.compiler.resolution import ResolvedQuery

_GROUPING_MARKER = "GROUPING() flag columns"
_TOTALS_MARKER = "ignored when combined"


def _resolved(**attrs: object) -> ResolvedQuery:
    """Build a stub with just the attributes the passes read.

    Accepts ``grouping`` (str or None), ``having_only_measures`` (set), and
    the ``has_*`` feature flags (bool). Anything unset defaults to off.

    ``has_window=True`` also seeds a window measure so the window pass's
    predicate (``window_pass_applies``, which inspects ``measures`` /
    ``metric_components``) sees it.
    """
    grouping = attrs.get("grouping")
    has_window = bool(attrs.get("has_window", False))
    measures = [SimpleNamespace(is_window=True, component_measures=[])] if has_window else []
    ns = SimpleNamespace(
        grouping=SimpleNamespace(value=grouping) if grouping is not None else None,
        has_totals=attrs.get("has_totals", False),
        has_pop=attrs.get("has_pop", False),
        has_cumulative=attrs.get("has_cumulative", False),
        has_window=has_window,
        has_filter_context=attrs.get("has_filter_context", False),
        having_only_measures=attrs.get("having_only_measures") or set(),
        measures=measures,
        metric_components={},
    )
    return cast(ResolvedQuery, ns)


def test_pass_order_is_declared_once() -> None:
    names = [p.name for p in build_default_passes()]
    assert names == [
        PASS_FILTER_CONTEXT,
        PASS_PERIOD_OVER_PERIOD,
        PASS_TOTALS,
        PASS_CUMULATIVE,
        PASS_WINDOW,
        PASS_HAVING_CLEANUP,
    ]


def test_totals_incompatibility_metadata() -> None:
    totals = next(p for p in build_default_passes() if p.name == PASS_TOTALS)
    assert totals.incompatible_with == frozenset({PASS_PERIOD_OVER_PERIOD, PASS_CUMULATIVE})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"has_totals": True},
        {"has_pop": True},
        {"has_cumulative": True},
        {"has_window": True},
    ],
)
def test_grouping_with_any_aggregate_feature_warns(kwargs: dict[str, bool]) -> None:
    passes = build_default_passes()
    result = evaluate_compatibility(_resolved(grouping="rollup", **kwargs), passes)
    assert any(_GROUPING_MARKER in w.message for w in result.warnings)


def test_grouping_alone_does_not_warn() -> None:
    passes = build_default_passes()
    result = evaluate_compatibility(_resolved(grouping="rollup"), passes)
    assert result.warnings == []
    assert result.suppressed == frozenset()


@pytest.mark.parametrize("conflict", ["has_pop", "has_cumulative"])
def test_totals_suppressed_when_combined_with_pop_or_cumulative(conflict: str) -> None:
    passes = build_default_passes()
    result = evaluate_compatibility(_resolved(has_totals=True, **{conflict: True}), passes)
    assert result.suppressed == frozenset({PASS_TOTALS})
    assert any(_TOTALS_MARKER in w.message for w in result.warnings)


def test_totals_alone_is_not_suppressed() -> None:
    passes = build_default_passes()
    result = evaluate_compatibility(_resolved(has_totals=True), passes)
    assert result.suppressed == frozenset()
    assert result.warnings == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"has_pop": True},
        {"has_cumulative": True},
        {"has_window": True},
        {"has_filter_context": True},
    ],
)
def test_no_warnings_without_grouping_or_totals_conflict(kwargs: dict[str, bool]) -> None:
    passes = build_default_passes()
    result = evaluate_compatibility(_resolved(**kwargs), passes)
    assert result.warnings == []
    assert result.suppressed == frozenset()


def test_warning_order_grouping_before_totals() -> None:
    """When both rules fire, the grouping advisory precedes the totals notice."""
    passes = build_default_passes()
    result = evaluate_compatibility(
        _resolved(grouping="rollup", has_totals=True, has_pop=True), passes
    )
    assert len(result.warnings) == 2
    assert _GROUPING_MARKER in result.warnings[0].message
    assert _TOTALS_MARKER in result.warnings[1].message
    assert result.suppressed == frozenset({PASS_TOTALS})


@pytest.mark.parametrize(
    ("name", "flag"),
    [
        (PASS_FILTER_CONTEXT, "has_filter_context"),
        (PASS_PERIOD_OVER_PERIOD, "has_pop"),
        (PASS_TOTALS, "has_totals"),
        (PASS_CUMULATIVE, "has_cumulative"),
    ],
)
def test_applies_predicates_track_flags(name: str, flag: str) -> None:
    # The window pass uses a richer predicate (it also fires when a derived
    # metric transitively references a window metric); it is covered by the
    # window/trend integration tests rather than this flag-based stub.
    pass_ = next(p for p in build_default_passes() if p.name == name)
    assert pass_.applies(_resolved(**{flag: True})) is True
    assert pass_.applies(_resolved()) is False


def test_having_cleanup_applies_on_having_only_measures() -> None:
    cleanup = next(p for p in build_default_passes() if p.name == PASS_HAVING_CLEANUP)
    assert cleanup.applies(_resolved(having_only_measures={"revenue"})) is True
    assert cleanup.applies(_resolved()) is False
