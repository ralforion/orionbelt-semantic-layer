"""Exact percentile metrics from another tool import as OBML percentile measures.

An Ossie metric written as ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY x)``,
``PERCENTILE_DISC(...)``, the two-argument ``PERCENTILE_CONT(x, p)`` or
Spark's exact ``percentile(x, p)`` becomes a measure with that aggregation and
``percentile: p``, alone or inside a formula. What OBML cannot model the same
way - a descending or multi-key order, a fraction that is not a literal in
(0, 1) with at most 9 places, an approximate percentile - is preserved
verbatim rather than converted to another measure.
"""

from __future__ import annotations

from typing import Any

import pytest

import osi_orionbelt.converter as conv


def _field(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": name}]},
    }


def _import(expression: str, dialect: str = "ANSI_SQL") -> dict[str, Any]:
    osi = {
        "version": "0.2.0.dev0",
        "semantic_model": [
            {
                "name": "sales",
                "datasets": [
                    {
                        "name": "Orders",
                        "source": "A.P.ORDERS",
                        "fields": [_field("amount"), _field("cost")],
                    }
                ],
                "metrics": [
                    {
                        "name": "Probe",
                        "expression": {
                            "dialects": [{"dialect": dialect, "expression": expression}]
                        },
                    }
                ],
            }
        ],
    }
    return conv.OSItoOBML(osi).convert()


def _column_measure(aggregation: str, percentile: float) -> dict[str, Any]:
    return {
        "columns": [{"dataObject": "Orders", "column": "amount"}],
        "resultType": "float" if aggregation == "percentile_cont" else "int",
        "aggregation": aggregation,
        "percentile": percentile,
    }


@pytest.mark.parametrize(
    ("expression", "dialect", "aggregation", "percentile"),
    [
        (
            "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_cont",
            0.9,
        ),
        (
            "PERCENTILE_DISC(0.25) WITHIN GROUP (ORDER BY Orders.amount ASC)",
            "ANSI_SQL",
            "percentile_disc",
            0.25,
        ),
        (
            "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY Orders.amount)",
            "SNOWFLAKE",
            "percentile_cont",
            0.5,
        ),
        ("percentile(Orders.amount, 0.95)", "DATABRICKS", "percentile_cont", 0.95),
        ("PERCENTILE_CONT(Orders.amount, 0.75)", "ANSI_SQL", "percentile_cont", 0.75),
        (
            "PERCENTILE_DISC(0.000000001) WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_disc",
            1e-9,
        ),
    ],
)
def test_imported_as_a_percentile_measure(
    expression: str, dialect: str, aggregation: str, percentile: float
) -> None:
    obml = _import(expression, dialect)
    assert obml["measures"]["Probe"] == _column_measure(aggregation, percentile)
    assert "Probe" not in obml.get("metrics", {})


def test_over_an_expression() -> None:
    obml = _import("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY Orders.amount - Orders.cost)")
    measure = obml["measures"]["Probe"]
    assert measure["aggregation"] == "percentile_cont"
    assert measure["percentile"] == 0.9
    assert measure["expression"] == "{[Orders].[amount]} - {[Orders].[cost]}"


def test_inside_a_formula() -> None:
    """Two percentiles of one column in one formula are two measures, told
    apart by their fractions."""
    obml = _import(
        "PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY Orders.amount)"
        " - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY Orders.amount)"
    )
    percentiles = {
        name: (m["aggregation"], m["percentile"])
        for name, m in obml["measures"].items()
        if m["aggregation"].startswith("percentile")
    }
    assert sorted(percentiles.values()) == [("percentile_cont", 0.25), ("percentile_cont", 0.75)]
    formula = obml["metrics"]["Probe"]["expression"]
    assert all("{[" + name + "]}" in formula for name in percentiles)


@pytest.mark.parametrize(
    "expression",
    [
        "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY Orders.amount DESC)",
        "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY Orders.amount, Orders.cost)",
        "PERCENTILE_CONT(1.5) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(0) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(0.0000000001) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(Orders.cost) WITHIN GROUP (ORDER BY Orders.amount)",
        "APPROX_PERCENTILE(Orders.amount, 0.9)",
    ],
    ids=["desc", "two keys", "above 1", "zero", "ten places", "column fraction", "approx"],
)
def test_preserved_when_not_modelable(expression: str) -> None:
    dialect = "SNOWFLAKE" if expression.startswith("APPROX") else "ANSI_SQL"
    obml = _import(expression, dialect)
    assert "Probe" not in obml.get("measures", {})
    assert "Probe" not in obml.get("metrics", {})
