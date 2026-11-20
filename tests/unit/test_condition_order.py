"""The result cache keys on a query's SQL with its AND-ed conditions in a fixed order.

Filters given in another order return the same rows; they must share a cache
entry. Anything else that tells two queries apart must still give two keys,
the joins above all: the order of the filters can pick a join path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
COMMERCE_MODEL = Path(__file__).parents[2] / "examples" / "orionbelt_1_commerce.yaml"


def _load(text: str) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return model


@pytest.fixture
def sample_model() -> SemanticModel:
    return _load(SAMPLE_MODEL_YAML)


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


def test_a_negated_conjunction_is_ordered_inside_and_not_split() -> None:
    """NOT (a AND b) is one condition, whose own operands are ordered."""
    dialect = DialectRegistry.get("postgres")
    first = _select(where=_and(C, UnaryOp(op="NOT", operand=_and(B, A))))
    second = _select(where=_and(UnaryOp(op="NOT", operand=_and(A, B)), C))
    assert key_sql(first, dialect) == key_sql(second, dialect)
    assert "NOT (t.a = '1' AND t.b = '2')" in key_sql(first, dialect).replace('"', "")


def test_a_long_conjunction_is_keyed_as_a_shallow_tree() -> None:
    """Flattened and sorted, a chain is rebuilt as deep as the log of its length."""
    dialect = DialectRegistry.get("postgres")
    parts = [_eq("a", str(i)) for i in range(2000)]
    assert key_sql(_select(where=_and(*parts)), dialect) == key_sql(
        _select(where=_and(*parts[::-1])), dialect
    )


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


def _assert_shared_key(model: SemanticModel, first: QueryObject, second: QueryObject) -> None:
    one, two = (CompilationPipeline().compile(q, model, "postgres") for q in (first, second))
    assert one.sql != two.sql
    assert one.key_sql == two.key_sql


def test_grouped_filters_in_another_order_compile_and_share_a_key(
    sample_model: SemanticModel,
) -> None:
    """Four AND groups of 128 filters each: one flattened chain of 512 was past
    the renderer's recursion limit, and the query failed to compile."""

    def query(step: int) -> QueryObject:
        groups = [
            {
                "logic": "and",
                "filters": [
                    {"field": "Customer Country", "op": "notequals", "value": f"{g}-{i}"}
                    for i in range(128)
                ][::step],
            }
            for g in range(4)
        ]
        return QueryObject.model_validate(
            {
                "select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]},
                "where": groups,
            }
        )

    _assert_shared_key(sample_model, query(1), query(-1))


def test_period_over_period_filters_in_another_order_share_a_key() -> None:
    """The PoP CTEs carry the filters as text the compiler writes itself, which
    the key cannot reorder: it writes them in the key's order."""
    model = _load(COMMERCE_MODEL.read_text())
    select = {
        "dimensions": ["Sales Month", "Client Name", "Product Name"],
        "measures": ["Sales YoY Growth"],
    }
    where: list[dict[str, Any]] = [
        {"field": "Client Name", "op": "equals", "value": "Kai Garcia"},
        {"field": "Product Name", "op": "equals", "value": "Laptop"},
    ]
    first, second = (
        QueryObject.model_validate({"select": select, "where": w}) for w in (where, where[::-1])
    )
    one, two = (CompilationPipeline().compile(q, model, "postgres") for q in (first, second))
    assert one.key_sql == two.key_sql


def _two_routes_model() -> SemanticModel:
    """Fact -> A -> Dates, and Fact -> B -> C -> Dates on a named secondary join."""
    shapes = {
        "Fact": (["A", "B"], {"Amount": ("amount", "float")}),
        "A": (["Dates"], {"Flag": ("flag", "int")}),
        "B": (["C"], {}),
        "C": (["Dates"], {"Flag": ("flag", "int")}),
        "Dates": ([], {"Name": ("name", "string")}),
    }
    objects: dict[str, Any] = {}
    for name, (targets, extra) in shapes.items():
        columns: dict[str, Any] = {"ID": {"code": "id", "abstractType": "int", "primaryKey": True}}
        columns.update(
            {f"{t}ID": {"code": f"{t.lower()}_id", "abstractType": "int"} for t in targets}
        )
        columns.update({c: {"code": code, "abstractType": t} for c, (code, t) in extra.items()})
        joins = [
            {"joinType": "many-to-one", "joinTo": t, "columnsFrom": [f"{t}ID"], "columnsTo": ["ID"]}
            for t in targets
        ]
        objects[name] = {"code": name.lower(), "columns": columns, "joins": joins}
    objects["C"]["joins"][0].update(secondary=True, pathName="c_dates")
    raw = {
        "version": "1.0",
        "dataObjects": objects,
        "dimensions": {
            "A Flag": {"dataObject": "A", "column": "Flag", "resultType": "int"},
            "C Flag": {"dataObject": "C", "column": "Flag", "resultType": "int"},
            "Date Name": {"dataObject": "Dates", "column": "Name", "resultType": "string"},
        },
        "measures": {
            "Revenue": {
                "columns": [{"dataObject": "Fact", "column": "Amount"}],
                "aggregation": "sum",
                "resultType": "float",
            }
        },
    }
    model, result = ReferenceResolver().resolve(raw)
    assert result.valid, result.errors
    return model


def test_filters_whose_order_picks_another_join_path_keep_two_keys() -> None:
    """With C joined first, Dates is reached through C rather than A: other rows.

    A key built from the query with its filters reordered gave both one entry,
    and the second query was served the first one's rows.
    """
    a = {"field": "A Flag", "op": "equals", "value": 1}
    c = {"field": "C Flag", "op": "equals", "value": 1}
    dates = {"field": "Date Name", "op": "equals", "value": "target"}
    paths = [{"source": "C", "target": "Dates", "pathName": "c_dates"}]
    model = _two_routes_model()
    one, two = (
        CompilationPipeline().compile(
            QueryObject.model_validate(
                {"select": {"measures": ["Revenue"]}, "where": w, "usePathNames": paths}
            ),
            model,
            "duckdb",
        )
        for w in ([dates, c, a], [c, dates, a])
    )
    assert one.key_sql != two.key_sql
