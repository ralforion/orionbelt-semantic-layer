"""A nested data object, compiled and executed against DuckDB.

The point of these is the *numbers*, not the SQL. Every claim the design makes
about unnesting is a claim about arithmetic - a parent measure double-counts a
charge that carries two labels, a nested measure must not collapse two identical
credits, an empty array still contributes its parent's cost - and a rendering
that looks right settles none of them.

The fixture is built so each of those cases is present exactly once:

===========  ======  ==========================  ====================
charge       cost    labels                      credits
===========  ======  ==========================  ====================
``c1``       100     ``team=prod``, ``env=prod`` two identical ``-5``
``c2``       100     ``team=prod``               one ``-1``
``c3``       50      *(empty)*                   *(empty)*
===========  ======  ==========================  ====================

``c1`` carries the same label *value* under two keys, which is what makes a
parent-side ``SUM`` wrong without deduplication; its two credits are
byte-identical, which is what makes the same deduplication wrong for a
nested-side ``SUM``. ``c3`` carries neither, which is what the outer form is
for - measured on a real GCP billing export, 61% of charges carry no labels.

The same arithmetic runs on the other six engines that unnest, from the same
compiled queries, in
``tests/integration/drift/vendor_exec/test_nested_plan_exec.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb package required for execution tests")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.models.query import QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

_SETUP_SQL = """
CREATE TABLE charges (
    id      VARCHAR,
    cost    DOUBLE,
    Labels  STRUCT("Key" VARCHAR, "Value" VARCHAR)[],
    Credits STRUCT("Type" VARCHAR, "Amount" DOUBLE)[]
);
INSERT INTO charges VALUES
    ('c1', 100, [{'Key':'team','Value':'prod'}, {'Key':'env','Value':'prod'}],
                [{'Type':'CUD','Amount':-5}, {'Type':'CUD','Amount':-5}]),
    ('c2', 100, [{'Key':'team','Value':'prod'}],
                [{'Type':'SUD','Amount':-1}]),
    ('c3',  50, [], []);
"""

MODEL_YAML = """
version: "1.0"
name: nested_charges
dataObjects:
  Charges:
    code: charges
    columns:
      Charge Id: {code: id, abstractType: string, primaryKey: true}
      Cost: {code: cost, abstractType: float}
  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    columns:
      Label Key: {code: Key, abstractType: string}
      Label Value: {code: Value, abstractType: string}
  Charge Credits:
    nestedIn: {dataObject: Charges, column: Credits}
    columns:
      Credit Type: {code: Type, abstractType: string}
      Credit Amount: {code: Amount, abstractType: float}
dimensions:
  Label Value: {dataObject: Charge Labels, column: Label Value}
  Credit Type: {dataObject: Charge Credits, column: Credit Type}
measures:
  Total Cost:
    columns: [{dataObject: Charges, column: Cost}]
    resultType: float
    aggregation: sum
  Total Credit:
    columns: [{dataObject: Charge Credits, column: Credit Amount}]
    resultType: float
    aggregation: sum
  Credit Count:
    columns: [{dataObject: Charge Credits, column: Credit Amount}]
    resultType: int
    aggregation: count
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def conn() -> Any:
    connection = duckdb.connect(":memory:")
    connection.execute(_SETUP_SQL)
    yield connection
    connection.close()


def _run(
    conn: Any,
    model: SemanticModel,
    dimensions: list[str],
    measures: list[str],
) -> list[tuple[Any, ...]]:
    """Compile the query and return its rows, ordered so they compare stably."""
    result = CompilationPipeline().compile(
        QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures)),
        model,
        "duckdb",
    )
    rows = conn.execute(result.sql).fetchall()
    return sorted(rows, key=lambda row: tuple("" if v is None else str(v) for v in row))


def test_a_nested_dimension_with_a_nested_measure(conn: Any, model: SemanticModel) -> None:
    """Credits by type: the naive unnest is already right.

    Each element appears exactly once, so nothing needs deduplicating - and the
    two identical ``-5`` credits on ``c1`` are two genuine credits that must
    both be counted. This is the case the design's "array-deduped" rewrite gets
    *wrong* (-6 instead of -11 in its own measurement), which is why the rule is
    directional rather than uniform.
    """
    rows = _run(conn, model, ["Credit Type"], ["Total Credit", "Credit Count"])
    assert rows == [(None, None, 0), ("CUD", -10.0, 2), ("SUD", -1.0, 1)]


def test_a_nested_dimension_with_a_parent_measure(conn: Any, model: SemanticModel) -> None:
    """Spend by label value: ``c1`` is one charge however many labels it carries.

    The naive unnest gives ``prod`` 300, because ``c1``'s cost is repeated under
    both of its labels. Deduplicating on the parent's key before aggregating
    gives 200, which is the sum of two distinct charges.
    """
    rows = _run(conn, model, ["Label Value"], ["Total Cost"])
    assert rows == [(None, 50.0), ("prod", 200.0)]


def test_the_empty_array_keeps_its_parent(conn: Any, model: SemanticModel) -> None:
    """``c3`` carries no labels and still contributes its 50 to the total.

    The inner form of every unnest drops it. That is why ``outer`` is the
    default and has no model surface: on a real billing export the inner form
    silently loses 95% of the spend.
    """
    rows = _run(conn, model, ["Label Value"], ["Total Cost"])
    assert sum(cost for _, cost in rows) == 250.0
    assert (None, 50.0) in rows


def test_the_mixed_case_needs_two_row_sets(conn: Any, model: SemanticModel) -> None:
    """A nested measure beside a parent measure, grouped by the nested dimension.

    No single flat rewrite answers both - measured in the design plan, the naive
    unnest inflates the parent measure and any ``DISTINCT`` that fixes it
    collapses the nested one. The two are computed over different row sets and
    meet at the query grain, which is the shape ``grain_dedup`` already had.
    """
    rows = _run(conn, model, ["Credit Type"], ["Total Credit", "Total Cost"])
    assert rows == [(None, None, 50.0), ("CUD", -10.0, 100.0), ("SUD", -1.0, 100.0)]


def test_two_nested_dimensions_with_a_parent_measure(conn: Any, model: SemanticModel) -> None:
    """Both arrays unnested at once, with the measure on the parent.

    The cross product of two child grains is real - ``c1`` produces four rows -
    but the parent's identity is unaffected by it, so deduplicating on that key
    still yields one ``c1`` per group. Per-group values are exact; the groups
    overlap, which is what the fan-trap warning says.
    """
    rows = _run(conn, model, ["Label Value", "Credit Type"], ["Total Cost"])
    assert rows == [(None, None, 50.0), ("prod", "CUD", 100.0), ("prod", "SUD", 100.0)]


def test_the_parents_own_row_count_is_deduplicated_too(conn: Any, model: SemanticModel) -> None:
    """``Charges Count`` is a row count of the parent, so the unnest inflates it.

    It is synthesized rather than declared, which is what makes it the easiest
    one to get wrong: nothing in the model says it exists. Two charges carry a
    ``prod`` label, so the answer is 2 however many labels each of them has.
    """
    rows = _run(conn, model, ["Label Value"], ["Charges Count"])
    assert rows == [(None, 1), ("prod", 2)]


def test_the_nested_object_has_no_synthesized_count(model: SemanticModel) -> None:
    """The outer unnest pads an empty array with one all-NULL row, which a
    ``COUNT(*)`` cannot tell from an element. A row count over a nested object
    is something the model declares over a column, not something synthesis can
    promise.
    """
    assert "Charges Count" in model.effective_measures
    assert "Charge Labels Count" not in model.effective_measures


def test_the_parent_measure_alone_is_untouched(conn: Any, model: SemanticModel) -> None:
    """No nested dimension, no unnest, no deduplication - and the true total."""
    result = CompilationPipeline().compile(
        QueryObject(select=QuerySelect(dimensions=[], measures=["Total Cost"])),
        model,
        "duckdb",
    )
    assert "UNNEST" not in result.sql.upper()
    assert conn.execute(result.sql).fetchall() == [(250.0,)]


# ---------------------------------------------------------------------------
# Every road a nested object can take into a FROM clause, executed.
#
# Four rounds of review found the same defect four times, in four different
# places, and every one of them compiled: a plan named a table it had not
# joined, or a column no union leg supplied. A string assertion passes on all
# of them. So the shapes are enumerated here and the compiled SQL is *handed to
# the engine* - the only check that can tell "it compiled" from "it binds".
#
# A shape is correct when it either binds and runs, or is refused with a
# structured error. What it must never do is compile to SQL the engine rejects.
# ---------------------------------------------------------------------------

_SHAPES_SQL = """
CREATE TABLE charges2 (
    id VARCHAR, cost DOUBLE,
    Labels STRUCT("Value" VARCHAR, "OwnerId" VARCHAR)[]
);
CREATE TABLE budgets (amount DOUBLE, budget_name VARCHAR);
CREATE TABLE owners (owner_id VARCHAR, owner_name VARCHAR, owner_weight DOUBLE);
INSERT INTO charges2 VALUES ('c1', 100, [{'Value':'prod','OwnerId':'o1'}]);
INSERT INTO budgets VALUES (500, 'b1');
INSERT INTO owners VALUES ('o1', 'Ada', 2.0);
"""

SHAPES_MODEL_YAML = """
version: "1.0"
name: nested_shapes
dataObjects:
  Charges:
    code: charges2
    columns:
      Charge Id: {code: id, abstractType: string, primaryKey: true}
      Cost: {code: cost, abstractType: float}
  Budgets:
    code: budgets
    columns:
      Amount: {code: amount, abstractType: float}
      Budget Name: {code: budget_name, abstractType: string}
  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    columns:
      Label Value: {code: Value, abstractType: string}
      Owner Id: {code: OwnerId, abstractType: string}
    joins:
      - joinTo: Owners
        columnsFrom: [Owner Id]
        columnsTo: [Owner Id]
        joinType: many-to-one
  Owners:
    code: owners
    columns:
      Owner Id: {code: owner_id, abstractType: string, primaryKey: true}
      Owner Name: {code: owner_name, abstractType: string}
      Owner Weight: {code: owner_weight, abstractType: float}
dimensions:
  Label Value: {dataObject: Charge Labels, column: Label Value}
  Owner Name: {dataObject: Owners, column: Owner Name}
  Budget Name: {dataObject: Budgets, column: Budget Name}
measures:
  Total Cost:
    columns: [{dataObject: Charges, column: Cost}]
    resultType: float
    aggregation: sum
  Budget Amount:
    columns: [{dataObject: Budgets, column: Amount}]
    resultType: float
    aggregation: sum
  Weighted Cost:
    expression: "{[Charges].[Cost]} * {[Owners].[Owner Weight]}"
    resultType: float
    aggregation: sum
"""

#: ``(label, dimensions, measures)``. Each either binds or is refused.
SHAPES: list[tuple[str, list[str], list[str]]] = [
    ("nested dim, parent measure", ["Label Value"], ["Total Cost"]),
    ("dim behind a nested edge, star", ["Owner Name"], ["Total Cost"]),
    ("dim behind a nested edge, union", ["Owner Name"], ["Total Cost", "Budget Amount"]),
    ("nested dim in a union", ["Label Value"], ["Total Cost", "Budget Amount"]),
    ("expression across a nested edge, star", ["Label Value"], ["Weighted Cost"]),
    ("expression across a nested edge, union", ["Budget Name"], ["Weighted Cost", "Budget Amount"]),
    ("unrelated union, model untouched", ["Budget Name"], ["Total Cost", "Budget Amount"]),
]


@pytest.fixture(scope="module")
def shapes_model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(SHAPES_MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def shapes_conn() -> Any:
    connection = duckdb.connect(":memory:")
    connection.execute(_SHAPES_SQL)
    yield connection
    connection.close()


@pytest.mark.parametrize(("label", "dimensions", "measures"), SHAPES, ids=[s[0] for s in SHAPES])
def test_every_shape_either_runs_or_refuses(
    shapes_conn: Any,
    shapes_model: SemanticModel,
    label: str,
    dimensions: list[str],
    measures: list[str],
) -> None:
    from orionbelt.compiler.fanout import FanoutError
    from orionbelt.compiler.resolution import ResolutionError
    from orionbelt.dialect.base import UnsupportedNestedAccessError

    try:
        result = CompilationPipeline().compile(
            QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures)),
            shapes_model,
            "duckdb",
        )
    except (ResolutionError, FanoutError, UnsupportedNestedAccessError):
        return  # a structured refusal is a correct outcome
    # Compiled, so it has to bind. This is the assertion four rounds of string
    # checks could not make.
    shapes_conn.execute(result.sql).fetchall()
