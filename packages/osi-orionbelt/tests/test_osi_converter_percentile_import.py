"""Exact percentile metrics from another tool import as OBML percentile measures.

An Ossie metric written as ``PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY x)``,
``PERCENTILE_DISC(...)``, the two-argument ``PERCENTILE_CONT(x, p)`` or
Spark's exact ``percentile(x, p)`` becomes a measure with that aggregation and
``percentile: p``, alone or inside a formula. What OBML cannot model the same
way - a descending or multi-key order, a fraction that is not a literal in
(0, 1) with at most 9 places - is preserved verbatim rather than converted to
another measure. An approximate percentile imports as the exact one it
estimates, with a warning.
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


def _metric(name: str, expression: str, dialect: str = "ANSI_SQL") -> dict[str, Any]:
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": dialect, "expression": expression}]},
    }


def _import(
    expression: str, dialect: str = "ANSI_SQL", before: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """The OBML of a model whose metric ``Probe`` is *expression*, after the
    metrics *before*."""
    osi = {
        "version": "0.2.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "Orders",
                "source": "A.P.ORDERS",
                "fields": [_field("amount"), _field("cost")],
            }
        ],
        "metrics": [*(before or []), _metric("Probe", expression, dialect)],
    }
    return conv.OSItoOBML(osi).convert()


def _column_measure(aggregation: str, percentile: float) -> dict[str, Any]:
    return {
        "columns": [{"dataObject": "Orders", "column": "amount"}],
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


def _converter(expression: str, dialect: str) -> conv.OSItoOBML:
    osi = {
        "version": "0.2.0.dev0",
        "name": "sales",
        "datasets": [
            {
                "name": "Orders",
                "source": "A.P.ORDERS",
                "fields": [_field("amount"), _field("cost")],
            }
        ],
        "metrics": [_metric("Probe", expression, dialect)],
    }
    return conv.OSItoOBML(osi)


@pytest.mark.parametrize(
    ("expression", "dialect", "aggregation"),
    [
        # Snowflake's t-digest estimate interpolates between values.
        ("APPROX_PERCENTILE(Orders.amount, 0.9)", "SNOWFLAKE", "percentile_cont"),
        # Databricks returns a value of the column; the accuracy only tunes the
        # estimate, which the exact aggregate does not need.
        ("approx_percentile(Orders.amount, 0.9)", "DATABRICKS", "percentile_disc"),
        ("approx_percentile(Orders.amount, 0.9, 1000)", "DATABRICKS", "percentile_disc"),
        ("percentile_approx(Orders.amount, 0.9)", "DATABRICKS", "percentile_disc"),
    ],
)
def test_an_approximate_percentile_imports_as_the_exact_one(
    expression: str, dialect: str, aggregation: str
) -> None:
    """The exact value is what the approximation estimates: the measure gets
    more accurate, not less, and the import says so."""
    converter = _converter(expression, dialect)
    obml = converter.convert()
    assert obml["measures"]["Probe"] == _column_measure(aggregation, 0.9)
    approximate = [w for w in converter.warnings if w.startswith("APPROXIMATE:")]
    assert len(approximate) == 1
    assert "'Probe'" in approximate[0] and aggregation in approximate[0]


def test_an_approximate_percentile_inside_a_formula() -> None:
    converter = _converter("APPROX_PERCENTILE(Orders.amount, 0.9) - SUM(Orders.cost)", "SNOWFLAKE")
    obml = converter.convert()
    aggregations = sorted(m["aggregation"] for m in obml["measures"].values())
    assert aggregations == ["percentile_cont", "sum"]
    assert "Probe" in obml["metrics"]
    assert any(w.startswith("APPROXIMATE:") for w in converter.warnings)


@pytest.mark.parametrize(
    ("expression", "dialect"),
    [
        # Several fractions at once: no one measure.
        ("approx_percentile(Orders.amount, array(0.25, 0.5))", "DATABRICKS"),
        # ANSI has no approximate percentile; it does not parse as one.
        ("APPROX_PERCENTILE(Orders.amount, 0.9)", "ANSI_SQL"),
    ],
)
def test_an_approximate_percentile_obml_cannot_model_is_preserved(
    expression: str, dialect: str
) -> None:
    converter = _converter(expression, dialect)
    obml = converter.convert()
    assert "Probe" not in obml.get("measures", {})
    assert not any(w.startswith("APPROXIMATE:") for w in converter.warnings)


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
        "PERCENTILE_DISC(0.9) WITHIN GROUP (ORDER BY Orders.amount DESC)",
        "PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY Orders.amount, Orders.cost)",
        "PERCENTILE_CONT(1.5) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(0) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(0.0000000001) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(Orders.cost) WITHIN GROUP (ORDER BY Orders.amount)",
        "percentile(Orders.amount, 0.3, Orders.cost)",
        "percentile(Orders.amount, 0.3, Orders.cost) - SUM(Orders.cost)",
        "quantile_cont(Orders.amount, 0.3, Orders.cost)",
        "percentile(DISTINCT Orders.amount, 0.3)",
        "PERCENTILE_CONT(0.3, 1) WITHIN GROUP (ORDER BY Orders.amount)",
        "percentile(Orders.amount, 0.3) FILTER (WHERE Orders.cost > 0)",
        "percentile(Orders.amount, 0.3) OVER ()",
        "PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY Orders.amount) FILTER (WHERE Orders.cost > 0)",
        "PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY Orders.amount) OVER ()",
        "PERCENTILE_CONT(CAST(0.9 AS DECIMAL)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST(0.25 AS DECIMAL(3, 1))) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST(0.9 AS DECIMAL(5))) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST(0.9 AS INT)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST(Orders.cost AS DOUBLE)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_DISC(CAST(0.1 AS FLOAT)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_DISC(CAST(0.1 AS REAL)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST(CAST(0.1 AS FLOAT) AS DOUBLE)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST('NaN' AS DOUBLE)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST('Infinity' AS DOUBLE)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST(1e40 AS FLOAT)) WITHIN GROUP (ORDER BY Orders.amount)",
        "PERCENTILE_CONT(CAST(-1e40 AS FLOAT)) WITHIN GROUP (ORDER BY Orders.amount)",
        "SUM(Orders.amount) FILTER (WHERE Orders.cost > 0)",
        "SUM(Orders.amount) OVER () - SUM(Orders.cost)",
    ],
    ids=[
        "disc desc",
        "two keys",
        "above 1",
        "zero",
        "ten places",
        "column fraction",
        "frequency",
        "frequency in a formula",
        "duckdb third argument",
        "distinct",
        "within group two arguments",
        "filter",
        "over",
        "within group filter",
        "within group over",
        "cast to bare decimal",
        "cast to a narrower scale",
        "cast to decimal without scale",
        "cast to int",
        "cast of a column",
        "float rounds 0.1",
        "real rounds 0.1",
        "float under double",
        "nan",
        "infinity",
        "float overflow",
        "float negative overflow",
        "sum filter",
        "sum over in a formula",
    ],
)
def test_preserved_when_not_modelable(expression: str) -> None:
    """Each would otherwise import as a different measure: a weighted
    percentile as an unweighted one (sqlglot drops the frequency argument), a
    DISTINCT one as an invalid expression, and any aggregate with a FILTER or
    OVER as a formula OBML does not parse."""
    dialect = "DATABRICKS" if expression.startswith("percentile(") else "ANSI_SQL"
    obml = _import(expression, dialect)
    assert "Probe" not in obml.get("measures", {})
    assert "Probe" not in obml.get("metrics", {})


def test_nested_call_in_the_argument_still_imports() -> None:
    """Commas inside the argument's own parentheses are not percentile arguments."""
    obml = _import("percentile(COALESCE(Orders.amount, 0), 0.3)", "DATABRICKS")
    assert obml["measures"]["Probe"]["percentile"] == 0.3


def test_fraction_tells_measures_apart_when_reusing_named_ones() -> None:
    """A named P50 first: the IQR formula's P75 and P25 are not it, and each
    operand keeps its own fraction."""
    p50 = _metric("P50", "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY Orders.amount)")
    obml = _import(
        "PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY Orders.amount)"
        " - PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY Orders.amount)",
        before=[p50],
    )
    formula = obml["metrics"]["Probe"]["expression"]
    assert "{[P50]}" not in formula
    fractions = sorted(
        m["percentile"] for name, m in obml["measures"].items() if "{[" + name + "]}" in formula
    )
    assert fractions == [0.25, 0.75]


def test_an_equal_named_percentile_is_reused() -> None:
    p75 = _metric("P75", "PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY Orders.amount)")
    obml = _import(
        "PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY Orders.amount) - SUM(Orders.cost)",
        before=[p75],
    )
    assert "{[P75]}" in obml["metrics"]["Probe"]["expression"]


@pytest.mark.parametrize(
    ("expression", "aggregation", "percentile"),
    [
        ("PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY Orders.amount DESC)", "percentile_cont", 0.1),
        (
            "PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY Orders.amount DESC NULLS LAST)",
            "percentile_cont",
            0.75,
        ),
        (
            "PERCENTILE_CONT(0.000000001) WITHIN GROUP (ORDER BY Orders.amount DESC)",
            "percentile_cont",
            0.999999999,
        ),
    ],
)
def test_descending_continuous_is_the_ascending_one_at_one_minus_p(
    expression: str, aggregation: str, percentile: float
) -> None:
    """``p * (n - 1)`` from the top is ``(1 - p) * (n - 1)`` from the bottom."""
    assert _import(expression)["measures"]["Probe"] == _column_measure(aggregation, percentile)


@pytest.mark.parametrize(
    ("expression", "dialect", "aggregation", "percentile"),
    [
        (
            "PERCENTILE_CONT(CAST(0.9 AS DOUBLE)) WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_cont",
            0.9,
        ),
        (
            "PERCENTILE_DISC(CAST(0.25 AS REAL)) WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_disc",
            0.25,
        ),
        (
            "PERCENTILE_DISC(CAST(0.25 AS FLOAT)) WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_disc",
            0.25,
        ),
        (
            "PERCENTILE_CONT(CAST(0.25 AS DECIMAL(5, 2))) WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_cont",
            0.25,
        ),
        (
            "PERCENTILE_CONT(0.9::DOUBLE) WITHIN GROUP (ORDER BY Orders.amount)",
            "SNOWFLAKE",
            "percentile_cont",
            0.9,
        ),
        ("percentile(Orders.amount, CAST(0.95 AS DOUBLE))", "DATABRICKS", "percentile_cont", 0.95),
        (
            "PERCENTILE_CONT(CAST('0.9' AS DOUBLE)) WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_cont",
            0.9,
        ),
        (
            "PERCENTILE_CONT(CAST(CAST(0.9 AS DECIMAL(3, 1)) AS DOUBLE))"
            " WITHIN GROUP (ORDER BY Orders.amount)",
            "ANSI_SQL",
            "percentile_cont",
            0.9,
        ),
    ],
)
def test_fraction_through_a_cast_that_keeps_it(
    expression: str, dialect: str, aggregation: str, percentile: float
) -> None:
    obml = _import(expression, dialect)
    assert obml["measures"]["Probe"] == _column_measure(aggregation, percentile)


@pytest.mark.parametrize("fraction", ["CAST('NaN' AS DOUBLE)", "CAST(1e40 AS FLOAT)"])
def test_an_unusable_fraction_leaves_the_rest_of_the_model_importing(fraction: str) -> None:
    """A NaN fraction, or one past single precision's range, preserves its
    metric; each used to raise and abort the whole import."""
    total = _metric("Total", "SUM(Orders.amount)")
    obml = _import(
        f"PERCENTILE_CONT({fraction}) WITHIN GROUP (ORDER BY Orders.amount)",
        before=[total],
    )
    assert obml["measures"]["Total"]["aggregation"] == "sum"
    assert "Probe" not in obml.get("measures", {})
