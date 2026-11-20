"""A fixed order for a query's filters, for its result-cache key.

Two queries that differ only in the order of their filters, ``Client = 'A'``
then ``Product = 'B'`` against the other way round, return the same rows but
compile to different SQL. The result cache keys on the compiled SQL, so the
second missed the entry the first wrote: in the UI, clicking the same two
values as filters in the other order ran the query on the database again.

:func:`ordered_query` puts the filters joined by AND in one fixed order, and
the pipeline compiles that query a second time for the key
(``CompilationResult.key_sql``). What moves: the ``where`` and ``having``
lists, the filters of a ``logic: and`` group and those of an ``exists``
subquery. The filters of a ``logic: or`` group keep their order, as does
everything else, so two queries share a key only when they hold the same
filters. The SQL that runs is the query as written.

The query is reordered rather than its compiled SQL: the compiler writes some
conditions as text (the CTEs of a period-over-period metric) that no walk of
the SQL tree reaches, and the reordered query nests its groups as deep as the
one written, which the compiler already took.
"""

from __future__ import annotations

import json

from orionbelt.models.query import QueryFilter, QueryFilterGroup, QueryFilterItem, QueryObject
from orionbelt.models.semantic import FilterLogic


def _sort_key(item: QueryFilterItem) -> str:
    return json.dumps(item.model_dump(mode="json", by_alias=True), sort_keys=True, default=str)


def _ordered_filters[F: QueryFilterItem](items: list[F], *, commutative: bool) -> list[F]:
    """*items*, each in its fixed order, and sorted among themselves if *commutative*."""
    ordered = [_ordered_item(item) for item in items]
    return sorted(ordered, key=_sort_key) if commutative else ordered


def _ordered_item[F: QueryFilterItem](item: F) -> F:
    if isinstance(item, QueryFilterGroup):
        filters = _ordered_filters(item.filters, commutative=item.logic == FilterLogic.AND)
        return item.model_copy(update={"filters": filters})
    if isinstance(item, QueryFilter) and item.subquery is not None:
        # An EXISTS subquery's filters are joined by AND.
        inner = _ordered_filters(item.subquery.filter, commutative=True)
        return item.model_copy(
            update={"subquery": item.subquery.model_copy(update={"filter": inner})}
        )
    return item


def ordered_query(query: QueryObject) -> QueryObject:
    """*query* with every list of filters joined by AND in a fixed order."""
    return query.model_copy(
        update={
            "where": _ordered_filters(query.where, commutative=True),
            "having": _ordered_filters(query.having, commutative=True),
        }
    )
