"""The outer query names only tables its own FROM provides (#358).

Syntax is not the only way a compiled statement can be wrong. An expression
left behind in a query that wraps it parses clean on every dialect and fails at
the database, and the failure names a data object from the *model*, so it reads
as a join problem rather than a projection-scope one. This is the check that
separates the two, and it runs on every compile.
"""

from __future__ import annotations

import pathlib

import pytest

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    ColumnRef,
    From,
    Join,
    JoinType,
    OrderByItem,
    Select,
    Unnest,
)
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.scope_check import out_of_scope_tables
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.models.warnings import WarningCode
from orionbelt.parser import ReferenceResolver, TrackedLoader


def _wrapped(order_by_table: str | None) -> Select:
    """A CTE-wrapped query, ordered by a column of *order_by_table*."""
    order_expr = ColumnRef(name="region", table=order_by_table)
    return Select(
        columns=[AliasedExpr(expr=ColumnRef(name="Region"), alias="Region")],
        from_=From(source="base", alias="base"),
        order_by=[OrderByItem(expr=order_expr)],
        ctes=[CTE(name="base", query=Select(from_=From(source="customer", alias="Customer")))],
    )


def test_a_table_only_the_cte_has_is_reported() -> None:
    """The shape of #358: the base table is in scope inside ``base``, not outside it."""
    assert out_of_scope_tables(_wrapped("Customer")) == {"Customer"}


def test_the_cte_itself_is_in_scope() -> None:
    assert out_of_scope_tables(_wrapped("base")) == set()


def test_an_unqualified_reference_is_in_scope() -> None:
    """A bare column is whatever the FROM exposes under that name."""
    assert out_of_scope_tables(_wrapped(None)) == set()


def test_a_joined_alias_is_in_scope() -> None:
    select = Select(
        columns=[AliasedExpr(expr=ColumnRef(name="name", table="D"), alias="Name")],
        from_=From(source="facts", alias="F"),
        joins=[
            Join(
                join_type=JoinType.LEFT,
                source="dim",
                alias="D",
                on=ColumnRef(name="id", table="D"),
            )
        ],
    )
    assert out_of_scope_tables(select) == set()


def test_an_unnest_puts_both_its_names_in_scope() -> None:
    """An unnest introduces the element alias and correlates it to its parent."""
    select = Select(
        columns=[
            AliasedExpr(expr=ColumnRef(name="sku", table="L"), alias="SKU"),
            AliasedExpr(expr=ColumnRef(name="id", table="Orders"), alias="ID"),
        ],
        from_=From(source="orders", alias="Orders"),
        joins=[Unnest(parent_alias="Orders", column="lines", alias="L")],
    )
    assert out_of_scope_tables(select) == set()


# ── on real compilations ────────────────────────────────────────────────────

MODEL_YAML = """
version: 1.0
name: scope_check

dataObjects:
  Customer:
    code: customer
    columns:
      Country: {code: country, abstractType: string}
      Amount:  {code: amount, abstractType: float, numClass: additive}
      Region:
        expression: "CASE WHEN {Country} IN ('DE', 'FR') THEN 'EU' ELSE 'Other' END"
        abstractType: string

dimensions:
  Region: {dataObject: Customer, column: Region, resultType: string}

measures:
  Total Amount:
    columns: [{dataObject: Customer, column: Amount}]
    resultType: float
    aggregation: sum
  Grand Total Amount:
    columns: [{dataObject: Customer, column: Amount}]
    resultType: float
    aggregation: sum
    total: true
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML)
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def test_a_wrapped_query_compiles_without_the_warning(model: SemanticModel) -> None:
    """The query from #358, which used to emit exactly what this check reports."""
    query = QueryObject.model_validate(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Total Amount", "Grand Total Amount"],
            },
            "orderBy": [{"field": "Region", "direction": "asc"}],
        }
    )
    result = CompilationPipeline().compile(query, model, "duckdb")
    assert [w for w in result.warnings if w.code == WarningCode.OUT_OF_SCOPE_TABLE] == []


@pytest.mark.parametrize(
    "path", sorted(pathlib.Path("examples").rglob("*.obml.yml")), ids=lambda p: p.stem
)
def test_the_bundled_models_compile_in_scope(path: pathlib.Path) -> None:
    """A warning has to be silent on models that are fine, or it teaches people to ignore it.

    One query per model, over the first four dimensions and four measures it
    declares. A model that cannot answer that particular combination is skipped
    rather than asserted about: what is being measured here is the warning's
    noise, and only a query that compiles has a compiled statement to check.
    """
    raw, source_map = TrackedLoader().load(path)
    model, result = ReferenceResolver().resolve(raw, source_map)
    if result.errors:
        pytest.skip(f"{path} does not resolve")
    query = QueryObject(
        select=QuerySelect(
            dimensions=list(model.dimensions)[:4],
            measures=list(model.measures)[:4],
        )
    )
    try:
        compiled = CompilationPipeline().compile(query, model, "duckdb")
    except Exception as exc:  # noqa: BLE001 - a query this model cannot answer is not this test's business
        pytest.skip(f"{path} rejects the probe query: {type(exc).__name__}: {exc}")
    stray = [w for w in compiled.warnings if w.code == WarningCode.OUT_OF_SCOPE_TABLE]
    assert not stray, [w.message for w in stray]
