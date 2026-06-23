"""Metamorphic / invariant tests for the compiler (Phase 7.2).

These assert *relationships* that must hold across query transformations,
rather than pinning exact SQL. They guard the compiler's structural
contract against regressions (especially after the Phase 5 resolution/CFL
decomposition):

- reordering selected measures changes neither joins nor required objects;
- adding a dimension only adds its join path + grouping, never drops a
  measure or changes the others;
- a WHERE filter on a dimension stays in WHERE (never becomes HAVING);
- a measure referenced only by HAVING is auto-included for aggregation but
  does not leak into the final projection;
- every registered dialect either compiles a corpus query or raises a
  stable, typed unsupported-feature error.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import QueryResolver, ResolvedQuery
from orionbelt.dialect.base import UnsupportedAggregationError, UnsupportedGroupingError
from orionbelt.dialect.registry import DialectRegistry, UnsupportedDialectError
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel


def _resolve(query: QueryObject, model: SemanticModel, dialect: str = "postgres") -> ResolvedQuery:
    d = DialectRegistry.get(dialect)
    return QueryResolver().resolve(
        query,
        model,
        qualify_table=lambda obj: d.format_table_ref(obj.database, obj.schema_name, obj.code),
    )


def _join_pairs(resolved: ResolvedQuery) -> set[tuple[str, str]]:
    return {(s.from_object, s.to_object) for s in resolved.join_steps}


# ── reordering measures is invariant for joins / required objects ───────────


def test_measure_order_does_not_change_joins_or_objects(sales_model: SemanticModel) -> None:
    q1 = QueryObject(
        select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue", "Order Count"])
    )
    q2 = QueryObject(
        select=QuerySelect(dimensions=["Customer Country"], measures=["Order Count", "Revenue"])
    )
    r1, r2 = _resolve(q1, sales_model), _resolve(q2, sales_model)
    assert set(r1.required_objects) == set(r2.required_objects)
    assert _join_pairs(r1) == _join_pairs(r2)
    assert {m.name for m in r1.measures} == {m.name for m in r2.measures}


# ── adding a dimension only adds its join path + grouping ───────────────────


def test_adding_dimension_is_additive(sales_model: SemanticModel) -> None:
    base = QueryObject(select=QuerySelect(measures=["Revenue"]))
    with_dim = QueryObject(
        select=QuerySelect(dimensions=["Product Category"], measures=["Revenue"])
    )
    rb, rw = _resolve(base, sales_model), _resolve(with_dim, sales_model)

    # Measures are untouched by adding a dimension.
    assert [m.name for m in rb.measures] == [m.name for m in rw.measures]
    # The dimension is added; the base had none.
    assert [d.name for d in rb.dimensions] == []
    assert "Product Category" in [d.name for d in rw.dimensions]
    # Required objects only grow (the dimension's join path is added, nothing removed).
    assert set(rb.required_objects) <= set(rw.required_objects)
    # The base joins are a subset of the with-dimension joins.
    assert _join_pairs(rb) <= _join_pairs(rw)


# ── WHERE on a dimension never becomes HAVING ───────────────────────────────


def test_where_on_dimension_stays_in_where(sales_model: SemanticModel) -> None:
    q = QueryObject(
        select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
        where=[QueryFilter(field="Customer Country", op="equals", value="Germany")],
    )
    r = _resolve(q, sales_model)
    assert len(r.where_filters) == 1
    assert len(r.having_filters) == 0


# ── HAVING-only measure: auto-included, not projected ───────────────────────


def test_having_only_measure_not_in_projection(sales_model: SemanticModel) -> None:
    q = QueryObject(
        select=QuerySelect(dimensions=["Customer Country"], measures=["Revenue"]),
        having=[QueryFilter(field="Order Count", op=">", value=0)],
    )
    r = _resolve(q, sales_model)
    # "Order Count" is referenced only by HAVING (not user-selected), so it is
    # auto-included for aggregation and tracked as having-only.
    assert "Order Count" not in q.select.measures
    assert "Order Count" in r.having_only_measures

    # In the compiled SQL it must NOT appear in the final projection.
    result = CompilationPipeline().compile(q, sales_model, "postgres")
    parsed = sqlglot.parse_one(result.sql, read="postgres")
    assert isinstance(parsed, exp.Query)
    projected = set(parsed.named_selects)
    assert "Order Count" not in projected
    assert "Revenue" in projected
    assert "Customer Country" in projected


# ── every dialect compiles the corpus or raises a typed error ───────────────

_TYPED_UNSUPPORTED = (
    UnsupportedAggregationError,
    UnsupportedGroupingError,
    UnsupportedDialectError,
    FanoutError,
)


def test_all_dialects_compile_or_raise_typed(sales_model: SemanticModel) -> None:
    corpus = QueryObject(
        select=QuerySelect(
            dimensions=["Customer Country", "Product Category"],
            measures=["Revenue", "Order Count"],
        )
    )
    dialects = DialectRegistry.available()
    assert dialects, "no dialects registered"
    for name in dialects:
        try:
            result = CompilationPipeline().compile(corpus, sales_model, name)
        except _TYPED_UNSUPPORTED:
            continue  # acceptable: a stable, typed unsupported-feature error
        assert result.sql.strip(), f"{name}: empty SQL"
