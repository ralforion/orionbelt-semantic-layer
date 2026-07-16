"""Aggregation values are validated against the controlled OBSL surface.

Pre-v2.7.5 bug (review finding 3): ``Measure.aggregation`` was a plain
``str``, so a typo like ``ssum`` validated clean and the SQL emitter
interpolated the bogus name directly — producing ``SSUM(...)`` SQL that
no engine would execute. Worse, since model authoring is semi-trusted,
this was a mild SQL-construction surface.

Fix: ``Measure.aggregation`` is now an ``AggregationType`` enum so
Pydantic rejects unknown values at parse time. Uppercase / mixed case
remain accepted via a normalizing field validator (BI tools and LLMs
often spell SQL keywords in uppercase).
"""

from __future__ import annotations

import pytest

from orionbelt.models.semantic import AggregationType, Measure
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver


def _yaml(agg: str) -> str:
    return f"""
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Amount:
        code: AMT
        abstractType: float
        numClass: additive
measures:
  M:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: {agg}
    resultType: float
"""


def _resolve(agg: str) -> tuple[bool, list[str]]:
    raw, sm = TrackedLoader().load_string(_yaml(agg))
    _model, vr = ReferenceResolver().resolve(raw, sm)
    return vr.valid, [e.code for e in vr.errors]


class TestAggregationEnum:
    def test_known_lowercase_accepted(self) -> None:
        for agg in ("sum", "count", "count_distinct", "avg", "min", "max"):
            valid, codes = _resolve(agg)
            assert valid, f"{agg!r} should be valid; got {codes}"

    def test_known_uppercase_accepted(self) -> None:
        # BI tools / LLMs commonly use uppercase SQL keywords
        for agg in ("SUM", "Count", "COUNT_DISTINCT", "Avg", "MIN", "MAX"):
            valid, codes = _resolve(agg)
            assert valid, f"{agg!r} should be valid; got {codes}"

    def test_typo_rejected(self) -> None:
        valid, codes = _resolve("ssum")
        assert not valid
        assert "MEASURE_PARSE_ERROR" in codes

    def test_unknown_aggregation_rejected(self) -> None:
        # ``agg`` and ``aggregate`` are reserved aliases for ``measure``
        # (v2.7.7+) so they are NOT in this set — they resolve to a valid
        # enum but the model validator rejects them for a different
        # reason (forbidden ``columns:`` for engine-delegated measures);
        # covered by tests/unit/test_aggregation_measure.py.
        for agg in ("totalize", "compute", "ssum", ""):
            valid, _ = _resolve(agg)
            assert not valid, f"{agg!r} should NOT validate as an aggregation"

    def test_aggregation_field_is_enum_on_loaded_measure(self) -> None:
        raw, sm = TrackedLoader().load_string(_yaml("SUM"))
        model, vr = ReferenceResolver().resolve(raw, sm)
        assert vr.valid
        m = model.measures["M"]
        assert isinstance(m.aggregation, AggregationType)
        assert m.aggregation == AggregationType.SUM


class TestMeasureDirectConstruction:
    """Pydantic-direct model construction also enforces the enum."""

    def test_direct_construction_rejects_unknown(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Measure(
                name="M",
                aggregation="ssum",  # type: ignore[arg-type]
                result_type="float",
            )

    def test_direct_construction_accepts_enum(self) -> None:
        m = Measure(
            name="M",
            aggregation=AggregationType.SUM,
            result_type="float",
        )
        assert m.aggregation == AggregationType.SUM

    def test_direct_construction_normalizes_case(self) -> None:
        m = Measure(name="M", aggregation="Sum", result_type="float")  # type: ignore[arg-type]
        assert m.aggregation == AggregationType.SUM
