"""An unnest reaches the SQL through the AST, not only the dialect method.

``Select.joins`` holds joins and unnests in one list, because the order between
them matters: an unnest names its parent, so it has to follow whatever put that
parent in scope. Keeping two lists would make the planner interleave them again
at render time, from information it no longer has.

Compile-only on purpose, so CI runs it. The per-dialect *fragments* are executed
against real engines in
``tests/integration/drift/vendor_exec/test_unnest_render_exec.py``, which CI
does not run.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import AliasedExpr, ColumnRef, FunctionCall, Unnest
from orionbelt.dialect.base import UnsupportedNestedAccessError
from orionbelt.dialect.registry import DialectRegistry

RENDERS = ["bigquery", "clickhouse", "databricks", "duckdb", "mysql", "postgres", "snowflake"]


def _ast(outer: bool = True):
    return (
        QueryBuilder()
        .select(AliasedExpr(expr=ColumnRef(name="Key", table="L"), alias="Label Key"))
        .select(
            AliasedExpr(
                expr=FunctionCall(name="SUM", args=[ColumnRef(name="cost", table="C")]),
                alias="Cost",
            )
        )
        .from_("charges", alias="C")
        .unnest(
            Unnest(
                parent_alias="C",
                column="x_Labels",
                alias="L",
                columns=(("Key", "VARCHAR(64)"),),
                outer=outer,
            )
        )
        .group_by(ColumnRef(name="Key", table="L"))
        .build()
    )


@pytest.mark.parametrize("dialect", RENDERS)
def test_the_unnest_reaches_the_sql(dialect: str) -> None:
    sql = DialectRegistry.get(dialect).compile_select(_ast())
    assert "x_Labels" in sql, sql


@pytest.mark.parametrize("dialect", RENDERS)
def test_it_lands_after_the_from_it_names(dialect: str) -> None:
    """Ordering is the reason the two share a list. A fragment naming ``C``
    before ``FROM ... AS C`` is a syntax error on every engine.
    """
    sql = DialectRegistry.get(dialect).compile_select(_ast())
    assert sql.index("FROM") < sql.index("x_Labels"), sql


@pytest.mark.parametrize("dialect", RENDERS)
def test_both_forms_differ(dialect: str) -> None:
    """Inner drops a parent whose array is empty; outer keeps it. If a dialect
    rendered them identically, one of the two would be silently wrong.
    """
    outer = DialectRegistry.get(dialect).compile_select(_ast(outer=True))
    inner = DialectRegistry.get(dialect).compile_select(_ast(outer=False))
    assert outer != inner, f"{dialect} renders inner and outer the same: {outer}"


def test_dremio_refuses_rather_than_emitting_something_that_will_not_parse() -> None:
    """``FLATTEN`` is a projection function, so Dremio's unnest needs a derived
    table rather than a FROM-clause fragment. That is a query restructure and
    belongs with the planner; until then the error names the ``code`` fallback.
    """
    with pytest.raises(UnsupportedNestedAccessError, match="no FROM-clause unnest"):
        DialectRegistry.get("dremio").compile_select(_ast())
