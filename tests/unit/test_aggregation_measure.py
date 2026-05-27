"""Tests for ``aggregation: MEASURE`` — engine-delegated aggregation.

OBML ``Measure.aggregation = "measure"`` makes the compiler emit a bare
``MEASURE("<measure_label>")`` call rather than wrapping a source column
in ``SUM(...)`` / ``COUNT(...)`` / etc. Only Databricks Metric Views
implement the function; every other dialect raises
``UnsupportedAggregationError``.

Snowflake Semantic Views use a different construct
(``SEMANTIC_VIEW(view DIMENSIONS d METRICS m)``) and are out of scope.

See issue #92.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import AggregationType, Measure, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MEASURE_VIEW_MODEL = """
version: 1.0

dataObjects:
  Sales Metric View:
    code: sales_metric_view
    database: warehouse
    schema: silver
    columns:
      Order Date:
        code: order_date
        abstractType: date
      Region:
        code: region
        abstractType: string

dimensions:
  Order Date:
    dataObject: Sales Metric View
    column: Order Date
    resultType: date
  Region:
    dataObject: Sales Metric View
    column: Region
    resultType: string

measures:
  Total Revenue:
    aggregation: measure
    resultType: float
    dataType: decimal(18, 2)

  Order Count:
    aggregation: MEASURE
    resultType: int
    dataType: bigint
"""


@pytest.fixture
def measure_view_model() -> SemanticModel:
    loader = TrackedLoader()
    raw, source_map = loader.load_string(_MEASURE_VIEW_MODEL)
    resolver = ReferenceResolver()
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, [e.message for e in result.errors]
    return model


# ----------------------------------------------------------------------
# Enum / alias normalization
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias",
    ["measure", "MEASURE", "Measure", "agg", "AGG", "aggregate", "AGGREGATE"],
)
def test_measure_alias_normalizes_to_canonical_enum(alias: str) -> None:
    """``agg`` / ``aggregate`` are accepted as aliases for ``measure`` so OBML
    reads naturally regardless of which vendor convention the user is used to.
    """
    m = Measure(label="X", aggregation=alias)
    assert m.aggregation == AggregationType.MEASURE


# ----------------------------------------------------------------------
# Model-level validation: MEASURE forbids columns / expression / filters / total
# ----------------------------------------------------------------------


def test_measure_aggregation_rejects_columns() -> None:
    """The engine resolves the aggregation by name; ``columns:`` would be
    silently ignored. Reject at model-load time.
    """
    with pytest.raises(ValueError, match="columns:"):
        Measure(
            label="Revenue",
            aggregation="measure",
            columns=[{"dataObject": "Sales", "column": "Amount"}],  # type: ignore[arg-type]
        )


def test_measure_aggregation_rejects_expression() -> None:
    with pytest.raises(ValueError, match="expression:"):
        Measure(
            label="Revenue",
            aggregation="measure",
            expression="{[Sales].[Amount]} * 1.1",
        )


def test_measure_aggregation_rejects_filters() -> None:
    with pytest.raises(ValueError, match="filters:"):
        Measure(
            label="Revenue",
            aggregation="measure",
            filters=[
                {
                    "column": {"dataObject": "Sales", "column": "Region"},
                    "operator": "equals",
                    "values": [{"dataType": "string", "valueString": "EMEA"}],
                }
            ],  # type: ignore[arg-type]
        )


def test_measure_aggregation_rejects_total() -> None:
    with pytest.raises(ValueError, match="total"):
        Measure(label="Revenue", aggregation="measure", total=True)


def test_measure_aggregation_allows_minimal_form() -> None:
    """Bare label + ``aggregation: measure`` must parse cleanly — that's
    the entire intended surface (the engine knows the rest).
    """
    m = Measure(label="Total Revenue", aggregation="measure")
    assert m.aggregation == AggregationType.MEASURE
    assert m.columns == []
    assert m.expression is None


# ----------------------------------------------------------------------
# Codegen: Databricks emits MEASURE("<label>"); other dialects reject
# ----------------------------------------------------------------------


def _compile(model: SemanticModel, dialect: str, measures: list[str]) -> str:
    query = QueryObject.model_validate({"select": {"dimensions": ["Region"], "measures": measures}})
    return CompilationPipeline().compile(query, model, dialect).sql


def test_databricks_emits_measure_function(measure_view_model: SemanticModel) -> None:
    """On Databricks, ``aggregation: measure`` compiles to a bare
    ``MEASURE(`Total Revenue`)`` projection — no column reference, no
    wrapping aggregate, no GROUP BY rewriting.
    """
    sql = _compile(measure_view_model, "databricks", ["Total Revenue"])
    assert "MEASURE(`Total Revenue`)" in sql, f"missing MEASURE() call in:\n{sql}"
    # Sanity: the original metric-view column does NOT appear (the engine
    # resolves the measure by name, not by reading a column).
    assert "SUM(" not in sql and "COUNT(" not in sql, (
        f"unexpected wrapping aggregate in MEASURE() output:\n{sql}"
    )


def test_databricks_handles_multiple_measures(measure_view_model: SemanticModel) -> None:
    sql = _compile(measure_view_model, "databricks", ["Total Revenue", "Order Count"])
    assert "MEASURE(`Total Revenue`)" in sql
    assert "MEASURE(`Order Count`)" in sql


@pytest.mark.parametrize(
    "dialect",
    ["snowflake", "bigquery", "duckdb", "clickhouse", "postgres", "mysql", "dremio"],
)
def test_other_dialects_reject_measure_aggregation(
    dialect: str, measure_view_model: SemanticModel
) -> None:
    """All seven non-Databricks dialects must raise
    ``UnsupportedAggregationError`` — including Snowflake, which uses
    the separate ``SEMANTIC_VIEW(...)`` table function instead of bare
    ``MEASURE()`` and would emit invalid SQL otherwise.
    """
    with pytest.raises(UnsupportedAggregationError) as excinfo:
        _compile(measure_view_model, dialect, ["Total Revenue"])
    assert excinfo.value.aggregation == "measure"
    assert excinfo.value.dialect == dialect


def test_measure_aggregation_listed_unsupported_on_all_but_databricks() -> None:
    """The dialect capability matrix must agree with codegen: every
    non-Databricks dialect lists ``measure`` in its
    ``unsupported_aggregations`` so ``GET /v1/dialects`` reflects
    actual support.
    """
    from orionbelt.dialect import DialectRegistry

    for name in DialectRegistry.available():
        caps = DialectRegistry.get(name).capabilities
        if name == "databricks":
            assert "measure" not in caps.unsupported_aggregations, (
                "Databricks must accept aggregation: measure"
            )
        else:
            assert "measure" in caps.unsupported_aggregations, (
                f"{name}: must list 'measure' as unsupported "
                "(only Databricks Metric Views accept it)"
            )
