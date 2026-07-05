"""Auto-synthesized row-count measures (``<object>.count``).

Covers the settled decisions in ``design/PLAN_synthesized_count_measures.md``:
synthesis presence (D1), anchored ``COUNT(*)`` + integer typing (D2), name-only
reference (D3), declared-wins override (D4), per-object / model opt-out and label
precedence + pattern validation (D5), and the invariant that a ``dataObject`` is
never a queryable FROM target.
"""

from __future__ import annotations

import warnings

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import (
    AggregationType,
    DataColumnRef,
    DataObject,
    DataObjectColumn,
    DataType,
    Measure,
    SemanticModel,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_MODEL_YAML = """\
version: 1.0

dataObjects:
  Sales:
    label: Sales
    code: SALES
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Sale ID:
        code: SALE_ID
        abstractType: string
        primaryKey: true
      Region:
        code: REGION
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Region
        columnsFrom: [Region]
        columnsTo: [Region Code]

  Region:
    label: Region
    code: REGIONS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Region Code:
        code: REGION
        abstractType: string
        primaryKey: true
      Region Name:
        code: REGION_NAME
        abstractType: string

dimensions:
  Region Name:
    dataObject: Region
    column: Region Name
    resultType: string

measures:
  Total Amount:
    columns:
      - dataObject: Sales
        column: Amount
    resultType: float
    aggregation: sum
"""


def _resolve(yaml_text: str) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture
def model() -> SemanticModel:
    return _resolve(_MODEL_YAML)


@pytest.fixture
def pipeline() -> CompilationPipeline:
    return CompilationPipeline()


# --------------------------------------------------------------------------- #
# D1 — synthesis presence
# --------------------------------------------------------------------------- #


def test_synthesis_presence(model: SemanticModel) -> None:
    """Every countable object yields ``<object>.count`` in effective measures."""
    assert "Sales.count" in model.effective_measures
    assert "Region.count" in model.effective_measures
    # Declared measures are untouched.
    assert "Total Amount" in model.effective_measures
    # Synthesized counts are NOT persisted on the raw measures dict.
    assert "Sales.count" not in model.measures


def test_synth_is_anchored_count(model: SemanticModel) -> None:
    m = model.effective_measures["Sales.count"]
    assert m.aggregation == AggregationType.COUNT
    assert m.result_type == DataType.INT
    assert m.data_type == "integer"
    # Column-less anchor ref points at the object.
    assert [c.view for c in m.columns] == ["Sales"]
    assert all(c.column is None for c in m.columns)


# --------------------------------------------------------------------------- #
# D2 — anchored COUNT(*), integer formatting (no DECIMAL cast)
# --------------------------------------------------------------------------- #


def test_count_alone_emits_count_star(model: SemanticModel, pipeline: CompilationPipeline) -> None:
    query = QueryObject(select=QuerySelect(measures=["Sales.count"]))
    sql = pipeline.compile(query, model, "duckdb").sql
    assert "COUNT(1)" in sql
    assert '"SALES"' in sql
    # Integer typing → no decimal cast on the count.
    assert "DECIMAL" not in sql.upper()
    assert "CAST(COUNT(1) AS INTEGER)" in sql


def test_count_by_many_to_one_dim_no_fanout(
    model: SemanticModel, pipeline: CompilationPipeline
) -> None:
    """A many-to-one join does not fan the fact, so the count stays true."""
    query = QueryObject(select=QuerySelect(dimensions=["Region Name"], measures=["Sales.count"]))
    sql = pipeline.compile(query, model, "duckdb").sql
    assert "COUNT(1)" in sql
    assert "LEFT JOIN" in sql
    # Anchored on the fact table, not the dimension table.
    assert 'FROM "PUBLIC"."SALES"' in sql


# --------------------------------------------------------------------------- #
# D4 — declared measure overrides synthesis
# --------------------------------------------------------------------------- #


def test_declared_override_wins(model: SemanticModel, pipeline: CompilationPipeline) -> None:
    declared = Measure(
        label="Sales.count",
        columns=[DataColumnRef(view="Sales", column="Sale ID")],
        aggregation=AggregationType.COUNT_DISTINCT,
        result_type=DataType.INT,
    )
    overridden = model.model_copy(update={"measures": {**model.measures, "Sales.count": declared}})
    # The declared one wins; synthesis steps aside.
    assert overridden.effective_measures["Sales.count"] is declared
    sql = pipeline.compile(
        QueryObject(select=QuerySelect(measures=["Sales.count"])), overridden, "duckdb"
    ).sql
    assert "COUNT(DISTINCT" in sql
    assert "COUNT(1)" not in sql


# --------------------------------------------------------------------------- #
# D5 — opt-out (per object + model) and label precedence / validation
# --------------------------------------------------------------------------- #


def test_per_object_opt_out() -> None:
    obj = DataObject(
        label="Sales",
        code="SALES",
        database="W",
        schema="P",
        countable=False,
        columns={
            "Amount": DataObjectColumn(label="Amount", code="AMT", abstract_type=DataType.FLOAT)
        },
    )
    m = SemanticModel(data_objects={"Sales": obj})
    assert "Sales.count" not in m.effective_measures


def test_model_expose_counts_false() -> None:
    obj = DataObject(
        label="Sales",
        code="SALES",
        database="W",
        schema="P",
        columns={
            "Amount": DataObjectColumn(label="Amount", code="AMT", abstract_type=DataType.FLOAT)
        },
    )
    m = SemanticModel(data_objects={"Sales": obj}, expose_counts=False)
    assert m.effective_measures == m.measures  # no synthesized counts


def test_label_default_pattern(model: SemanticModel) -> None:
    assert model.effective_measures["Sales.count"].label == "Sales Count"


def test_label_model_pattern() -> None:
    obj = DataObject(
        label="Sales",
        code="SALES",
        database="W",
        schema="P",
        columns={
            "Amount": DataObjectColumn(label="Amount", code="AMT", abstract_type=DataType.FLOAT)
        },
    )
    m = SemanticModel(data_objects={"Sales": obj}, count_label_pattern="# {object}")
    assert m.effective_measures["Sales.count"].label == "# Sales"


def test_label_per_object_override() -> None:
    obj = DataObject(
        label="Sales",
        code="SALES",
        database="W",
        schema="P",
        count_label="Sales headcount",
        columns={
            "Amount": DataObjectColumn(label="Amount", code="AMT", abstract_type=DataType.FLOAT)
        },
    )
    m = SemanticModel(data_objects={"Sales": obj}, count_label_pattern="# {object}")
    # Per-object override beats the model pattern.
    assert m.effective_measures["Sales.count"].label == "Sales headcount"


def test_label_interpolates_display_label_not_key() -> None:
    """``{object}`` fills from the object's display label, not the reference key."""
    obj = DataObject(
        label="Sales",
        code="FACT_SALES",
        database="W",
        schema="P",
        columns={
            "Amount": DataObjectColumn(label="Amount", code="AMT", abstract_type=DataType.FLOAT)
        },
    )
    m = SemanticModel(data_objects={"fact_sales": obj})
    assert m.effective_measures["fact_sales.count"].label == "Sales Count"


def test_pattern_validation_rejects_other_tokens() -> None:
    with pytest.raises(ValueError, match="countLabelPattern"):
        SemanticModel(count_label_pattern="{name} bad")


def test_count_label_ignored_when_not_countable_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        DataObject(
            label="Sales",
            code="SALES",
            database="W",
            schema="P",
            countable=False,
            count_label="ignored",
            columns={},
        )
    assert any("countLabel" in str(w.message) for w in caught)


# --------------------------------------------------------------------------- #
# Invariant — a dataObject is never a FROM-able / queryable artifact
# --------------------------------------------------------------------------- #


def test_dataobject_not_queryable_as_measure(
    model: SemanticModel, pipeline: CompilationPipeline
) -> None:
    """Referencing the bare object name as a measure is rejected; only the
    synthesized ``<object>.count`` (an anchored measure) is valid."""
    with pytest.raises(Exception):  # noqa: B017 - resolution raises on unknown measure
        pipeline.compile(QueryObject(select=QuerySelect(measures=["Sales"])), model, "duckdb")
