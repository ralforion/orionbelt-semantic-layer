"""The result cache keys on a query's SQL with its AND-ed conditions in a fixed order.

Filters given in another order return the same rows; they must share a cache
entry. Anything else that tells two queries apart must still give two keys.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import (
    CTE,
    BinaryOp,
    ColumnRef,
    Expr,
    From,
    FunctionCall,
    Literal,
    OrderByItem,
    Select,
    SubqueryExpr,
    UnaryOp,
    UnionAll,
)
from orionbelt.compiler.condition_order import _canonical, key_sql
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML

DIALECTS = ["postgres", "duckdb", "snowflake", "mysql", "bigquery", "clickhouse", "databricks"]


@pytest.fixture
def sample_model() -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(SAMPLE_MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


def _eq(column: str, value: str) -> Expr:
    return BinaryOp(left=ColumnRef(name=column, table="t"), op="=", right=Literal.string(value))


A, B, C = _eq("a", "1"), _eq("b", "2"), _eq("c", "3")


def _and(*parts: Expr) -> Expr:
    result = parts[0]
    for part in parts[1:]:
        result = BinaryOp(left=result, op="AND", right=part)
    return result


def _select(where: Expr | None = None, having: Expr | None = None, **kw: object) -> Select:
    return Select(
        columns=[ColumnRef(name="a", table="t")],
        from_=From(source="t", alias="t"),
        where=where,
        having=having,
        **kw,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("dialect_name", DIALECTS)
def test_where_conditions_in_any_order_share_a_key(dialect_name: str) -> None:
    dialect = DialectRegistry.get(dialect_name)
    first = _select(where=_and(A, B, C))
    second = _select(where=_and(C, A, B))
    assert dialect.compile(first) != dialect.compile(second)
    assert key_sql(first, dialect) == key_sql(second, dialect)


def test_nesting_of_the_and_chain_does_not_matter() -> None:
    dialect = DialectRegistry.get("postgres")
    left = _select(where=BinaryOp(left=BinaryOp(left=A, op="AND", right=B), op="AND", right=C))
    right = _select(where=BinaryOp(left=C, op="AND", right=BinaryOp(left=B, op="AND", right=A)))
    assert key_sql(left, dialect) == key_sql(right, dialect)


def test_having_conditions_in_any_order_share_a_key() -> None:
    dialect = DialectRegistry.get("postgres")
    total = FunctionCall(name="SUM", args=[ColumnRef(name="x", table="t")])
    gt = BinaryOp(left=total, op=">", right=Literal.number(10))
    lt = BinaryOp(left=total, op="<", right=Literal.number(99))
    assert key_sql(_select(having=_and(gt, lt)), dialect) == key_sql(
        _select(having=_and(lt, gt)), dialect
    )


def test_or_keeps_its_order() -> None:
    """Only conjunctions are reordered."""
    dialect = DialectRegistry.get("postgres")
    a_or_b = _select(where=BinaryOp(left=A, op="OR", right=B))
    b_or_a = _select(where=BinaryOp(left=B, op="OR", right=A))
    assert key_sql(a_or_b, dialect) != key_sql(b_or_a, dialect)


def test_a_negated_conjunction_is_not_split() -> None:
    """NOT (a AND b) is one condition; its operands stay as they are."""
    dialect = DialectRegistry.get("postgres")
    query = _select(where=_and(C, UnaryOp(op="NOT", operand=_and(B, A))))
    assert "NOT (t.b = '2' AND t.a = '1')" in key_sql(query, dialect).replace('"', "")


def test_conditions_of_subqueries_ctes_and_union_legs_are_ordered() -> None:
    dialect = DialectRegistry.get("postgres")

    def build(order: tuple[Expr, ...]) -> Select:
        inner = _select(where=_and(*order))
        exists = FunctionCall(name="EXISTS", args=[SubqueryExpr(query=inner)])
        return Select(
            columns=[ColumnRef(name="a", table="u")],
            from_=From(source="u", alias="u"),
            ctes=[CTE(name="u", query=UnionAll(queries=[inner, inner]))],
            where=_and(exists, *order),
        )

    assert key_sql(build((A, B, C)), dialect) == key_sql(build((B, C, A)), dialect)


def test_other_differences_still_give_two_keys() -> None:
    dialect = DialectRegistry.get("postgres")
    base = _select(where=_and(A, B))
    assert key_sql(base, dialect) != key_sql(_select(where=_and(A, _eq("b", "9"))), dialect)
    assert key_sql(base, dialect) != key_sql(_select(where=_and(A, B), distinct=True), dialect)
    assert key_sql(base, dialect) != key_sql(_select(where=_and(A, B), limit=5), dialect)


def test_every_field_of_every_node_survives() -> None:
    """With nothing to reorder the canonical tree is the tree, field for field.

    A hand-kept list of fields (as ``ASTVisitor.visit_select`` has) drops what it
    does not name, a DISTINCT or a GROUPING SETS, and two queries would share a key.
    """
    node = Select(
        columns=[ColumnRef(name="a", table="t")],
        from_=From(source=_select(where=A), alias="s"),
        where=B,
        group_by=[ColumnRef(name="a", table="t")],
        having=C,
        order_by=[OrderByItem(expr=ColumnRef(name="a", table="t"), desc=True, nulls_last=True)],
        limit=10,
        offset=5,
        ctes=[CTE(name="x", query=UnionAll(queries=[_select(where=A)]))],
        distinct=True,
        grouping="ROLLUP",
    )
    assert _canonical(node, DialectRegistry.get("postgres")) == node


def _two_filter_queries() -> tuple[QueryObject, QueryObject]:
    select = {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}
    where = [
        {"field": "Customer Country", "op": "notequals", "value": "FR"},
        {"field": "Customer Country", "op": "notequals", "value": "DE"},
    ]
    having = [
        {"field": "Total Revenue", "op": "gt", "value": 10},
        {"field": "Order Count", "op": "gt", "value": 1},
    ]
    first = QueryObject.model_validate({"select": select, "where": where, "having": having})
    second = QueryObject.model_validate(
        {"select": select, "where": where[::-1], "having": having[::-1]}
    )
    return first, second


@pytest.mark.parametrize("dialect_name", DIALECTS)
def test_compiled_filters_in_another_order_share_a_key(
    sample_model: SemanticModel, dialect_name: str
) -> None:
    first, second = _two_filter_queries()
    one = CompilationPipeline().compile(first, sample_model, dialect_name)
    two = CompilationPipeline().compile(second, sample_model, dialect_name)
    assert one.sql != two.sql  # the SQL that runs keeps the order given
    assert one.key_sql == two.key_sql


def test_key_sql_is_the_sql_when_nothing_moves(sample_model: SemanticModel) -> None:
    query = QueryObject.model_validate(
        {"select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]}}
    )
    result = CompilationPipeline().compile(query, sample_model, "postgres")
    assert result.cache_key_sql is None
    assert result.key_sql == result.sql
