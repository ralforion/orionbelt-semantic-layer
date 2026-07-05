"""Auto-synthesized row-count measures (name == label, e.g. "Sales Count").

Covers the settled decisions in ``design/PLAN_synthesized_count_measures.md``:
synthesis presence (D1), anchored ``COUNT(*)`` + integer typing (D2), name-only
reference (D3), declared-wins override (D4), per-object / model opt-out and name/
label precedence + pattern validation (D5), and the invariant that a ``dataObject``
is never a queryable FROM target.
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
    """Every countable object yields its count measure in effective measures."""
    assert "Sales Count" in model.effective_measures
    assert "Region Count" in model.effective_measures
    # Declared measures are untouched.
    assert "Total Amount" in model.effective_measures
    # Synthesized counts are NOT persisted on the raw measures dict.
    assert "Sales Count" not in model.measures


def test_synth_is_anchored_count(model: SemanticModel) -> None:
    m = model.effective_measures["Sales Count"]
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
    query = QueryObject(select=QuerySelect(measures=["Sales Count"]))
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
    query = QueryObject(select=QuerySelect(dimensions=["Region Name"], measures=["Sales Count"]))
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
        label="Sales Count",
        columns=[DataColumnRef(view="Sales", column="Sale ID")],
        aggregation=AggregationType.COUNT_DISTINCT,
        result_type=DataType.INT,
    )
    overridden = model.model_copy(update={"measures": {**model.measures, "Sales Count": declared}})
    # The declared one wins; synthesis steps aside.
    assert overridden.effective_measures["Sales Count"] is declared
    sql = pipeline.compile(
        QueryObject(select=QuerySelect(measures=["Sales Count"])), overridden, "duckdb"
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
    assert "Sales Count" not in m.effective_measures


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
    assert model.effective_measures["Sales Count"].label == "Sales Count"


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
    # Name == label, so the pattern sets both the key and the label.
    assert "# Sales" in m.effective_measures
    assert m.effective_measures["# Sales"].label == "# Sales"


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
    # Per-object override beats the model pattern (and is the name too).
    assert "Sales headcount" in m.effective_measures
    assert m.effective_measures["Sales headcount"].label == "Sales headcount"


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
    # Name/label interpolate the display label ("Sales"), not the key.
    assert "Sales Count" in m.effective_measures
    assert m.effective_measures["Sales Count"].label == "Sales Count"


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


# --------------------------------------------------------------------------- #
# Resolver parses the knobs from YAML (not just Python construction)
# --------------------------------------------------------------------------- #


def test_resolver_honors_countable_false() -> None:
    m = _resolve(
        """
version: 1.0
dataObjects:
  Sales:
    code: SALES
    database: W
    schema: P
    countable: false
    columns:
      Amount: {code: AMT, abstractType: float}
"""
    )
    assert m.data_objects["Sales"].countable is False
    assert "Sales Count" not in m.effective_measures


def test_resolver_honors_expose_counts_and_pattern() -> None:
    m = _resolve(
        """
version: 1.0
exposeCounts: false
countLabelPattern: "# {object}"
dataObjects:
  Sales:
    code: SALES
    database: W
    schema: P
    columns:
      Amount: {code: AMT, abstractType: float}
"""
    )
    assert m.expose_counts is False
    assert m.count_label_pattern == "# {object}"
    assert list(m.effective_measures) == []


def test_resolver_honors_count_label() -> None:
    m = _resolve(
        """
version: 1.0
dataObjects:
  Sales:
    label: Sales
    code: SALES
    database: W
    schema: P
    countLabel: Deals
    columns:
      Amount: {code: AMT, abstractType: float}
"""
    )
    # countLabel sets both the name and the label.
    assert "Deals" in m.effective_measures
    assert m.effective_measures["Deals"].label == "Deals"


# --------------------------------------------------------------------------- #
# Metrics can reference synthesized counts; fanout guard applies
# --------------------------------------------------------------------------- #


def test_derived_metric_references_synthesized_count() -> None:
    m = _resolve(
        """
version: 1.0
dataObjects:
  Sales:
    code: SALES
    database: W
    schema: P
    columns:
      Amount: {code: AMT, abstractType: float}
measures:
  Revenue: {aggregation: sum, columns: [{dataObject: Sales, column: Amount}]}
metrics:
  Double Count: {type: derived, expression: '{[Sales Count]} * 2'}
"""
    )
    assert "Double Count" in m.metrics


def test_synthesized_count_subject_to_fanout_guard(pipeline: CompilationPipeline) -> None:
    """A synthesized count is not exempt from fan-trap prevention. A
    many-to-many join is forward-traversable but multiplies rows, so counting
    the anchored object while grouping by the far side raises FanoutError -
    exactly as a declared count would. Regression for the detect_fanout()
    lookup that previously saw only declared measures."""
    from orionbelt.compiler.fanout import FanoutError

    m = _resolve(
        """
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: W
    schema: P
    columns:
      Order ID: {code: ORDER_ID, abstractType: string, primaryKey: true}
      Tag ID: {code: TAG_ID, abstractType: string}
    joins:
      - joinType: many-to-many
        joinTo: Tags
        columnsFrom: [Tag ID]
        columnsTo: [Tag ID]
  Tags:
    code: TAGS
    database: W
    schema: P
    columns:
      Tag ID: {code: TAG_ID, abstractType: string, primaryKey: true}
      Tag Name: {code: TAG_NAME, abstractType: string}
dimensions:
  Tag Name: {dataObject: Tags, column: Tag Name, resultType: string}
"""
    )
    with pytest.raises(FanoutError):
        pipeline.compile(
            QueryObject(select=QuerySelect(dimensions=["Tag Name"], measures=["Orders Count"])),
            m,
            "duckdb",
        )


# --------------------------------------------------------------------------- #
# Namespace: a metric/dimension may not shadow a synthesized count
# --------------------------------------------------------------------------- #


def test_metric_named_like_count_is_rejected() -> None:
    from orionbelt.parser.validator import SemanticValidator

    m = _resolve(
        """
version: 1.0
dataObjects:
  Sales:
    code: SALES
    database: W
    schema: P
    columns:
      Amount: {code: AMT, abstractType: float}
measures:
  Revenue: {aggregation: sum, columns: [{dataObject: Sales, column: Amount}]}
metrics:
  Sales Count: {type: derived, expression: '{[Revenue]} * 2'}
"""
    )
    errors = SemanticValidator().validate(m)
    assert any(e.code == "DUPLICATE_IDENTIFIER" for e in errors)


def test_semantic_ql_resolves_synthesized_count(model: SemanticModel) -> None:
    from orionbelt.compiler.sql_translator import translate_sql_to_query

    query = translate_sql_to_query('SELECT "Region Name", "Sales Count" FROM m', model)
    assert "Sales Count" in query.select.measures


def test_declared_count_override_not_flagged_as_duplicate() -> None:
    from orionbelt.parser.validator import SemanticValidator

    m = _resolve(
        """
version: 1.0
dataObjects:
  Sales:
    code: SALES
    database: W
    schema: P
    columns:
      Sale ID: {code: SALE_ID, abstractType: string, primaryKey: true}
measures:
  Sales Count:
    aggregation: count_distinct
    columns: [{dataObject: Sales, column: Sale ID}]
"""
    )
    errors = SemanticValidator().validate(m)
    assert not any(e.code == "DUPLICATE_IDENTIFIER" for e in errors)


# --------------------------------------------------------------------------- #
# Invalid knob values become structured errors, not raw exceptions
# --------------------------------------------------------------------------- #


def _resolve_result(yaml_text: str):
    raw, source_map = TrackedLoader().load_string(yaml_text)
    return ReferenceResolver().resolve(raw, source_map)


def test_resolver_list_pattern_is_structured_error() -> None:
    _, result = _resolve_result(
        """
version: 1.0
countLabelPattern: [not, a, string]
dataObjects:
  Sales: {code: S, database: W, schema: P, columns: {A: {code: A, abstractType: int}}}
"""
    )
    assert not result.valid
    assert any(e.code == "INVALID_COUNT_LABEL_PATTERN" for e in result.errors)


def test_resolver_bad_pattern_token_is_structured_error() -> None:
    _, result = _resolve_result(
        """
version: 1.0
countLabelPattern: "{name} Count"
dataObjects:
  Sales: {code: S, database: W, schema: P, columns: {A: {code: A, abstractType: int}}}
"""
    )
    assert not result.valid
    assert any(e.code == "INVALID_COUNT_LABEL_PATTERN" for e in result.errors)


def test_resolver_bad_expose_counts_is_structured_error() -> None:
    _, result = _resolve_result(
        """
version: 1.0
exposeCounts: [nope]
dataObjects:
  Sales: {code: S, database: W, schema: P, columns: {A: {code: A, abstractType: int}}}
"""
    )
    assert not result.valid
    assert any(e.code == "INVALID_EXPOSE_COUNTS" for e in result.errors)


# --------------------------------------------------------------------------- #
# Semantic QL: wrap validation treats synthesized counts like declared ones
# --------------------------------------------------------------------------- #


def test_semantic_ql_wrap_on_synth_count_uses_effective(model: SemanticModel) -> None:
    from orionbelt.compiler.sql_translator import translate_sql_to_query

    # COUNT wrap on a count-declared synthesized measure is the matching wrap
    # (validated via effective_measures) and resolves to the bare count.
    query = translate_sql_to_query('SELECT COUNT("Sales Count") FROM m', model)
    assert "Sales Count" in query.select.measures


# --------------------------------------------------------------------------- #
# CFL: multi-fact synthesized counts get the integer CAST
# --------------------------------------------------------------------------- #

_CFL_YAML = """
version: 1.0
dataObjects:
  Customers:
    code: C
    database: W
    schema: P
    columns:
      CID: {code: CID, abstractType: string, primaryKey: true}
      Country: {code: CO, abstractType: string}
  Orders:
    code: O
    database: W
    schema: P
    columns:
      OID: {code: OID, abstractType: string}
      OCID: {code: CID, abstractType: string}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [OCID], columnsTo: [CID]}
  Returns:
    code: RT
    database: W
    schema: P
    columns:
      RID: {code: RID, abstractType: string}
      RCID: {code: CID, abstractType: string}
      Refund: {code: REF, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Customers, columnsFrom: [RCID], columnsTo: [CID]}
dimensions:
  Country: {dataObject: Customers, column: Country, resultType: string}
measures:
  Total Refunds: {aggregation: sum, columns: [{dataObject: Returns, column: Refund}]}
"""


def test_cfl_synthesized_count_gets_integer_cast(pipeline: CompilationPipeline) -> None:
    m = _resolve(_CFL_YAML)
    # Orders Count (synthesized) + Total Refunds (Returns) => CFL (independent facts).
    query = QueryObject(
        select=QuerySelect(dimensions=["Country"], measures=["Orders Count", "Total Refunds"])
    )
    sql = pipeline.compile(query, m, "duckdb").sql
    assert "UNION ALL" in sql  # confirms the CFL path
    assert 'CAST(COUNT("composite_01"."Orders Count") AS INTEGER)' in sql
