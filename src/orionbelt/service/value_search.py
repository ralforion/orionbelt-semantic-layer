"""Dimension-value search: the values a dimension holds, to ground a filter value.

An agent writing ``Country = 'Germany'`` cannot know whether the column holds
``Germany``, ``DE`` or ``GERMANY``, and a wrong guess is no error: the query
returns no rows. A value search answers from the data.

It is not a new way of reading the warehouse. Each step is a dimension-only
OBSL query, compiled and executed like any other, so every rule a query obeys
holds unchanged: static model filters (a tenant filter restricts the values),
role dimensions, computed columns, time grains, join paths, auth and the
result cache. Nothing reads a table the model does not expose, and a dimension
declaring ``valueSearch: false`` is not listed at all.

A string dimension with a search term is matched in two stages:

1. **Candidates**, in the database: ``ilike '%term%'``, ranked here as
   ``exact``, ``prefix`` or ``contains``, then alphabetically.
2. **Fuzzy**, only when that finds fewer than ``limit``: the whole domain,
   fetched with ``limit: fuzzy_domain_max + 1``. Fewer rows than that is the
   whole domain, ranked by ``service.fuzzy`` (``Germny`` finds ``Germany``); a
   full page means it is over the cap, and the response says the fuzzy stage
   did not run.

A date, time or number dimension returns its range instead; a boolean lists
its values.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from orionbelt.compiler.filters import _escape_like
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryFilterItem,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.models.semantic import DATE_BEARING_TYPES, DataType, Dimension, SemanticModel
from orionbelt.service.fuzzy import fuzzy_search

#: The candidates the database returns for ranking, at least, per search.
CANDIDATE_MIN = 100

_RANGE_TYPES = DATE_BEARING_TYPES | {DataType.TIME, DataType.TIME_TZ, DataType.INT}
_RANGE_TYPES |= {DataType.FLOAT, DataType.DECIMAL}


@dataclass(frozen=True)
class ValueSearchConfig:
    """Server limits for the value search (``VALUE_SEARCH_*`` settings)."""

    enabled: bool = True
    max_limit: int = 200
    fuzzy_domain_max: int = 10000
    timeout_ms: int = 30000


class ValueSearchError(Exception):
    """A value search the server refuses, with a typed code and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class QueryRun:
    """One executed query: the first column's values and the SQL that read them."""

    values: list[Any]
    sql: str
    warnings: list[str] = field(default_factory=list)


#: Compiles and executes a query; the API runs it through the result cache.
Runner = Callable[[QueryObject], Awaitable[QueryRun]]


@dataclass(frozen=True)
class ValueMatch:
    """A value and why it was returned: ``exact``, ``prefix``, ``contains`` or
    ``fuzzy`` for a search, ``listed`` without one."""

    value: Any
    match: str
    score: float | None = None


@dataclass
class ValueSearchResult:
    """What a value search found."""

    dimension: str
    values: list[ValueMatch] = field(default_factory=list)
    truncated: bool = False
    minimum: Any = None
    maximum: Any = None
    sql: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _dimension(model: SemanticModel, name: str) -> Dimension:
    dim = model.dimensions.get(name)
    if dim is None:
        raise ValueSearchError("UNKNOWN_DIMENSION", f"Unknown dimension '{name}'")
    if not dim.value_search:
        raise ValueSearchError(
            "VALUE_SEARCH_DISABLED",
            f"Dimension '{name}' declares valueSearch: false; its values are not listed",
            status=403,
        )
    return dim


def _query(
    dimension: str,
    where: list[QueryFilterItem],
    limit: int,
    *,
    descending: bool = False,
) -> QueryObject:
    direction = SortDirection.DESC if descending else SortDirection.ASC
    return QueryObject(
        select=QuerySelect(dimensions=[dimension]),
        where=where,
        order_by=[QueryOrderBy(field=dimension, direction=direction)],
        limit=limit,
    )


def _match(value: str, term: str) -> str | None:
    folded, wanted = value.casefold(), term.casefold()
    if folded == wanted:
        return "exact"
    if folded.startswith(wanted):
        return "prefix"
    if wanted in folded:
        return "contains"
    return None


_RANK = {"exact": 0, "prefix": 1, "contains": 2}


class _Search:
    """One value search, collecting the SQL and warnings of the queries it runs."""

    def __init__(self, dimension: str, run: Runner) -> None:
        self.result = ValueSearchResult(dimension=dimension)
        self._run = run

    async def run(self, query: QueryObject) -> list[Any]:
        executed = await self._run(query)
        self.result.sql.append(executed.sql)
        self.result.warnings.extend(w for w in executed.warnings if w not in self.result.warnings)
        return executed.values


async def search_values(
    model: SemanticModel,
    dimension: str,
    *,
    run: Runner,
    config: ValueSearchConfig,
    search: str | None = None,
    where: list[QueryFilterItem] | None = None,
    limit: int = 20,
) -> ValueSearchResult:
    """Search the values of *dimension* under the query filters *where*."""
    if not 1 <= limit <= config.max_limit:
        raise ValueSearchError(
            "INVALID_LIMIT", f"limit must be between 1 and {config.max_limit}, got {limit}"
        )
    dim = _dimension(model, dimension)
    filters = list(where or [])
    term = (search or "").strip()
    state = _Search(dimension, run)

    if dim.time_grain is not None or dim.result_type in _RANGE_TYPES:
        if term:
            raise ValueSearchError(
                "VALUE_SEARCH_NOT_SUPPORTED",
                f"Dimension '{dimension}' is a {dim.result_type.value}: it has a range, "
                "not values to search; leave out 'search' to get its minimum and maximum",
            )
        await _range(state, dimension, filters)
    elif not term:
        await _listing(state, dimension, filters, limit)
    else:
        if dim.result_type is not DataType.STRING:
            raise ValueSearchError(
                "VALUE_SEARCH_NOT_SUPPORTED",
                f"Dimension '{dimension}' is a {dim.result_type.value}; only a string "
                "dimension is searched",
            )
        await _matches(state, dimension, filters, term, limit, config)
    return state.result


async def _range(state: _Search, dimension: str, where: list[QueryFilterItem]) -> None:
    """The smallest and largest value, NULLs left out."""
    present = [*where, QueryFilter(field=dimension, op=FilterOperator.SET)]
    lowest = await state.run(_query(dimension, present, 1))
    highest = await state.run(_query(dimension, present, 1, descending=True))
    state.result.minimum = lowest[0] if lowest else None
    state.result.maximum = highest[0] if highest else None


async def _listing(
    state: _Search, dimension: str, where: list[QueryFilterItem], limit: int
) -> None:
    """The first *limit* values in order; NULL is a value too."""
    values = await state.run(_query(dimension, where, limit + 1))
    state.result.truncated = len(values) > limit
    state.result.values = [ValueMatch(value=v, match="listed") for v in values[:limit]]


async def _matches(
    state: _Search,
    dimension: str,
    where: list[QueryFilterItem],
    term: str,
    limit: int,
    config: ValueSearchConfig,
) -> None:
    """Database candidates for *term*, then a fuzzy pass over a small domain."""
    cap = min(config.fuzzy_domain_max, max(CANDIDATE_MIN, 10 * limit))
    contains = QueryFilter(
        field=dimension, op=FilterOperator.ILIKE, value=f"%{_escape_like(term)}%"
    )
    candidates = await state.run(_query(dimension, [*where, contains], cap + 1))
    if len(candidates) > cap:
        state.result.truncated = True
        state.result.warnings.append(
            f"More than {cap} values of '{dimension}' contain '{term}'; "
            "only the first are ranked. Refine the search."
        )
    ranked = sorted(
        (
            (_RANK[kind], str(value).casefold(), value, kind)
            for value in candidates[:cap]
            if value is not None and (kind := _match(str(value), term)) is not None
        ),
        key=lambda item: (item[0], item[1]),
    )
    found = [ValueMatch(value=value, match=kind) for _, _, value, kind in ranked]
    if len(found) < limit:
        found += await _fuzzy(state, dimension, where, term, limit - len(found), config, found)
    state.result.truncated = state.result.truncated or len(found) > limit
    state.result.values = found[:limit]


async def _fuzzy(
    state: _Search,
    dimension: str,
    where: list[QueryFilterItem],
    term: str,
    wanted: int,
    config: ValueSearchConfig,
    found: list[ValueMatch],
) -> list[ValueMatch]:
    """Near misses of *term* in the whole domain, when it is within the cap."""
    domain = await state.run(_query(dimension, where, config.fuzzy_domain_max + 1))
    if len(domain) > config.fuzzy_domain_max:
        state.result.warnings.append(
            f"'{dimension}' has more than {config.fuzzy_domain_max} values: no fuzzy "
            "matching, only values containing the search term."
        )
        return []
    seen = {str(m.value) for m in found}
    by_text = {str(v): v for v in domain if v is not None and str(v) not in seen}
    matches = fuzzy_search(term, [(text, "value", []) for text in by_text], max_results=wanted)
    return [ValueMatch(value=by_text[m.name], match="fuzzy", score=m.score) for m in matches]
