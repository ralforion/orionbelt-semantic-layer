"""Federated queries: one query over data objects in several data sources.

A data object names the data source its table is read from (``dataSource``,
else ``settings.defaultDataSource``). When a query reaches objects of more
than one source, no single database can run it. It is split instead:

* An **island** is a group of the query's objects that share a source and are
  joined among themselves. The island holding the query's base object and all
  of its measures is the **fact island**; every other island is a
  **dimension island**, reached from the fact island by one many-to-one join,
  a **federation edge**.
* Each island becomes a **leg**: an ordinary query compiled by the existing
  pipeline, in the island's dialect, against a model holding only that
  island. The fact leg groups by the query's dimensions on its island plus the
  edge keys, and pre-aggregates the measures; a dimension leg returns the
  distinct keys with the dimensions the query reads there.
* The **combine** step is the original query, compiled by the same pipeline
  for DuckDB against a model whose data objects are the legs: the fact leg
  joins each dimension leg on its key, and each measure is re-aggregated from
  the leg's partial result (``SUM`` of sums and counts, ``MIN`` of minimums).
* **Rows.** When a measure does not combine from partial results (a median,
  a correlation), the fact leg groups by every column the measures read as
  well and counts each group's rows instead, and the combine step computes
  every measure from that many copies of each (:func:`_row_measures`).
  Derived metrics, filters reading several islands or true for NULL, HAVING,
  ORDER BY and LIMIT run there, over the combined rows; a filter on one island
  that rejects NULL runs in its leg.
* **Key injection.** Where a dimension leg runs such a filter, the plan can
  compile the fact leg again reading only the keys that leg returned
  (``FederatedPlan.inject``); the executor decides from how many it got.

Only what splits exactly is planned. Everything else is refused with a named
``FEDERATION_*`` error rather than answered with a different number; see
``design/PLAN_federation.md`` §5.5 for which shapes come later.

This module plans and compiles; ``service/federated_executor.py`` runs the
plan.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from orionbelt.ast.nodes import ColumnRef, Expr
from orionbelt.compiler.filters import _resolve_subquery_filter_field
from orionbelt.compiler.grain_dedup import MULTIPLICITY_SAFE_AGGREGATIONS, dedup_identity
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.compiler.resolution import (
    ResolutionError,
    ResolvedQuery,
    columns_in_own_zone,
    static_filters_after_join,
)
from orionbelt.compiler.type_resolver import (
    combined_averages,
    combined_variances,
    exact_input_scale,
    partial_squares,
    partial_sums,
    resolve_measure_cast_type,
)
from orionbelt.dialect.base import Dialect
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.errors import SemanticError
from orionbelt.models.expressions import find_qualified_refs, repoint_references
from orionbelt.models.query import (
    DimensionRef,
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    Subquery,
)
from orionbelt.models.semantic import (
    AggregationType,
    Cardinality,
    DataColumnRef,
    DataObject,
    DataObjectColumn,
    DataObjectJoin,
    DataType,
    Dimension,
    FilterEnforcement,
    FilterLogic,
    Measure,
    MeasureFilterGroup,
    MeasureFilterItem,
    MetricType,
    ModelFilter,
    ModelSettings,
    SemanticModel,
)
from orionbelt.models.types import DecimalType

COMBINE_DIALECT = "duckdb"

# The alias a leg's own query is read under when the leg rewrites some of its
# columns (:meth:`Dialect.equality_key`).
_LEG_ALIAS = "__leg"

# Aggregations whose result over the union of groups is computed from the
# per-group results, and how. ``avg`` combines from two of them, a sum and a
# count (:func:`_leg_measures`), so it is not a single re-aggregation here.
_REAGGREGATE: dict[AggregationType, AggregationType] = {
    AggregationType.SUM: AggregationType.SUM,
    AggregationType.COUNT: AggregationType.SUM,
    AggregationType.MIN: AggregationType.MIN,
    AggregationType.MAX: AggregationType.MAX,
    AggregationType.ANY_VALUE: AggregationType.ANY_VALUE,
}

# Aggregations computed from each group's count, sum and sum of squares
# (:func:`_leg_measures`), when the values are integers or declared decimals.
_VARIANCES = frozenset(
    {
        AggregationType.VARIANCE,
        AggregationType.VAR_POP,
        AggregationType.STDDEV,
        AggregationType.STDDEV_POP,
    }
)

# The most places a variance's values may have: the combine step reads their
# squares, at twice the places, as whole numbers of a 38-digit decimal.
_VARIANCE_MOST_PLACES = 19

# Aggregations that compare the values they read, when computed from a fact
# leg's rows (:func:`_compared_columns`): by order, or by equality alone.
_ORDERING = frozenset(
    {
        AggregationType.MIN,
        AggregationType.MAX,
        AggregationType.MEDIAN,
        AggregationType.PERCENTILE_CONT,
        AggregationType.PERCENTILE_DISC,
        AggregationType.FIRST,
        AggregationType.LAST,
        AggregationType.LISTAGG,
    }
)
_COUNTING = frozenset({AggregationType.COUNT_DISTINCT, AggregationType.MODE})

# The relation the combine step reads a fact leg returning rows as: the leg,
# each row repeated as often as it counts (:func:`_with_rows`); and the prefix
# of each relation of an object's rows once each, the parent of measures that
# read it through the fact rows (:meth:`_Planner.parents`).
_ROWS_RELATION = "leg_0_rows"
_ONCE_RELATION = "leg_0_once"

# The filter operators true where their field is NULL, and those false there.
# Every other one compares the field, which is unknown (NULL) for NULL.
# A missing subject row matches no row of an ``exists`` subquery: ``exists`` is
# false there, ``nonexists`` true.
_TRUE_FOR_NULL = frozenset(
    {FilterOperator.IS_NULL, FilterOperator.NOT_SET, FilterOperator.BLANK, FilterOperator.NONEXISTS}
)
_FALSE_FOR_NULL = frozenset(
    {
        FilterOperator.IS_NOT_NULL,
        FilterOperator.SET,
        FilterOperator.NOT_BLANK,
        FilterOperator.EXISTS,
    }
)

# The key types the fact leg may read a list of (:func:`_injectable_key_type`).
_INJECTABLE_KEYS = frozenset({DataType.INT, DataType.STRING})

# The decimal scale a decimal ``avg``'s partial sums are normalised to before
# the combine step divides them: the widest any engine returns one at
# (BigQuery's NUMERIC has 9 places), so a sum from every engine probed
# converts exactly, and a sum with more places is refused rather than rounded.
_AVERAGE_SCALE = 9


@dataclass(frozen=True)
class FederatedLeg:
    """One query a data source runs for a federated query.

    Attributes:
        name: The relation the combine step reads it as (``leg_0``).
        data_source: The data source it runs on, as the model names it;
            ``None`` for a dialect's own source.
        dialect: The dialect its SQL is written in.
        sql: The leg's query.
        data_objects: The model's data objects it reads.
        columns: Each column it returns, with the OBML type the combine step
            reads it as.
        physical_tables: The ``DATABASE.SCHEMA.CODE`` tables it reads, which
            decide how long its result may be cached.
        exact_sums: The columns of partial sums an ``avg`` is divided from,
            each with the decimal scale it is read at, so the combine step
            divides exactly.
        integer_sums: The columns of partial sums and sums of squares a
            ``variance`` or ``stddev`` combines from, each with its decimal
            scale: the combine step reads them as whole numbers of
            ``10^-scale`` units.
        rows: The column saying how many of its data source's rows each row
            stands for, when the leg returns rows rather than partial results
            (:func:`_row_measures`); the combine query repeats each row that
            many times.
    """

    name: str
    data_source: str | None
    dialect: str
    sql: str
    data_objects: tuple[str, ...]
    columns: tuple[tuple[str, str], ...] = ()
    physical_tables: tuple[str, ...] = ()
    exact_sums: tuple[tuple[str, int], ...] = ()
    integer_sums: tuple[tuple[str, int], ...] = ()
    rows: str | None = None


@dataclass(frozen=True)
class KeyInjection:
    """A dimension leg whose keys may filter the fact leg.

    The leg runs a filter of the query, and the combine step keeps only the
    fact rows it matched: the fact leg may read only the keys it returned.

    Attributes:
        leg: The dimension leg (``leg_1``).
        column: The key column, of the same name in that leg and the fact leg.
        abstract_type: The key's OBML type, ``int`` or ``string``.
    """

    leg: str
    column: str
    abstract_type: str


@dataclass(frozen=True)
class KeyInjector:
    """Compiles the fact leg again, reading only the keys given for each column.

    It holds copies of the leg's query and of its island's model, and nothing
    of the query or model planned: a query changed after planning does not
    change it, and a plan in the compilation cache does not keep a model
    unloaded since alive.

    Attributes:
        leg: The fact leg as planned, which the injected one replaces.
        query: The query it compiles from.
        model: The model of its island it compiles against.
        columns: The columns it returns.
        squares: The sums of squares it reads, by name, each with its scale.
        own_zone: The ``(data object, column)`` pairs it reads in their own
            time (:func:`columns_in_own_zone`).
        rekeyed: Its columns compared as their engine compares them.
        kept: The copies of rekeyed columns a combine filter reads.
        compile_leg: As :func:`plan_federated` takes it.
    """

    leg: FederatedLeg
    query: QueryObject
    model: SemanticModel
    columns: tuple[DataObjectColumn, ...]
    squares: dict[str, int]
    own_zone: frozenset[tuple[str, str]]
    rekeyed: dict[str, tuple[str, bool]]
    kept: dict[str, str]
    compile_leg: Callable[[QueryObject, SemanticModel, str], str] = dataclasses.field(
        compare=False, repr=False
    )

    def __call__(self, keys: dict[str, list[Any]]) -> FederatedLeg:
        """The fact leg reading only *keys*; an empty list for a column reads no rows."""
        if all(keys.values()):
            where = [
                *self.query.where,
                *(
                    QueryFilter(field=column, op=FilterOperator.IN_LIST, value=values)
                    for column, values in keys.items()
                ),
            ]
            query = self.query.model_copy(update={"where": where})
        else:
            # No key at all matches no fact row: the leg's columns, without rows.
            query = self.query.model_copy(update={"limit": 0})
        dialect = DialectRegistry.get(self.leg.dialect)
        with (
            partial_sums(),
            partial_squares(dialect, self.squares),
            columns_in_own_zone(self.own_zone),
        ):
            sql = self.compile_leg(query, self.model, dialect.name)
        if self.rekeyed:
            sql = _rewrite_leg(dialect, sql, list(self.columns), self.rekeyed, self.kept)
        return dataclasses.replace(self.leg, sql=sql)


@dataclass(frozen=True)
class FederatedPlan:
    """The legs of a federated query and the DuckDB query combining them.

    Attributes:
        injections: The dimension legs whose keys may filter the fact leg
            (``legs[0]``), which then runs after them.
        inject: The fact leg reading only the keys given for each injection's
            column; an empty list for any of them reads no rows.
    """

    legs: tuple[FederatedLeg, ...]
    combine_sql: str
    combine_dialect: str = COMBINE_DIALECT
    injections: tuple[KeyInjection, ...] = ()
    # In the repr, which the compilation cache sizes an entry by.
    inject: Callable[[dict[str, list[Any]]], FederatedLeg] | None = dataclasses.field(
        default=None, compare=False
    )


def object_sources(model: SemanticModel) -> dict[str, str | None]:
    """Each data object's data source, compared as the registry compares names.

    The object's ``dataSource``, else ``settings.defaultDataSource``; ``None``
    for neither. An object read through ``nestedIn`` takes its parent's.
    """
    from orionbelt.service.datasource_selection import source_key

    default = model.settings.default_data_source if model.settings else None
    sources: dict[str, str | None] = {}
    for name in model.data_objects:
        root = model.data_objects[model.unnest_root(name)]
        source = root.data_source or default
        sources[name] = source_key(source) if source else None
    return sources


def source_name(model: SemanticModel, obj: str) -> str | None:
    """The data source of *obj* as the model writes it."""
    root = model.data_objects[model.unnest_root(obj)]
    default = model.settings.default_data_source if model.settings else None
    return root.data_source or default


def query_objects(resolved: ResolvedQuery, query: QueryObject, model: SemanticModel) -> set[str]:
    """Every data object a resolved query reads, ``exists`` subqueries included."""
    objects = _outer_objects(resolved)
    for item in _exists_filters([*query.where, *query.having]):
        objects |= _exists_objects(item, model)
    objects.discard("")
    return objects


def _outer_objects(resolved: ResolvedQuery) -> set[str]:
    """The data objects a resolved query reads outside its ``exists`` subqueries."""
    objects = set(resolved.required_objects) | {resolved.base_object}
    for step in resolved.join_steps:
        objects |= {step.from_object, step.to_object}
    objects |= {d.object_name for d in resolved.dimensions}
    objects |= set(resolved.measure_source_objects)
    objects.discard("")
    return objects


def is_federated(resolved: ResolvedQuery, query: QueryObject, model: SemanticModel) -> bool:
    """Whether a query reads data objects of more than one data source."""
    sources = object_sources(model)
    read = query_objects(resolved, query, model)
    return len({sources.get(o) for o in read if o in sources}) > 1


def query_source(resolved: ResolvedQuery, query: QueryObject, model: SemanticModel) -> str | None:
    """The data source a query on one source reads, for a model read from several.

    As the model writes it; ``None`` when the model reads one source, or when
    the query reads the objects that name none.
    """
    sources = object_sources(model)
    if len(set(sources.values())) < 2:
        return None
    read = sorted(o for o in query_objects(resolved, query, model) if o in sources)
    return source_name(model, read[0]) if read else None


def plan_federated(
    query: QueryObject,
    model: SemanticModel,
    resolved: ResolvedQuery,
    *,
    dialect_name: str,
    compile_leg: Callable[[QueryObject, SemanticModel, str], str],
    source_dialect: Callable[[str | None], str | None],
    leg_row_limit: int | None = None,
) -> FederatedPlan:
    """Split a query reading several data sources into legs and a combine step.

    Args:
        query: The query as asked.
        model: The model, roles expanded, that *resolved* came from.
        resolved: The query's resolution against *model*.
        dialect_name: The dialect a leg is written in when its data source is
            not configured here, so its dialect is unknown.
        compile_leg: Compiles a query against a model for a dialect, returning
            SQL; the pipeline's own compile, for single-source queries.
        source_dialect: The dialect of a configured data source, or ``None``.
        leg_row_limit: The most rows a leg may return. Each leg asks for one
            more, so the executor can tell a leg that reached the limit from
            one that fits it and refuse the first rather than combine a part.

    Raises:
        ResolutionError: A ``FEDERATION_*`` error for a query that does not
            split exactly.
    """
    planner = _Planner(query, model, resolved, dialect_name, source_dialect)
    leg_limit = leg_row_limit + 1 if leg_row_limit is not None else None
    planner.check_query_shape()
    islands = planner.islands()
    fact = planner.fact_island(islands)
    edges = planner.federation_edges(islands, fact)
    rows_for = planner.check_measures(islands[fact])
    leg_filters, combine_filters, absent = planner.place_filters(islands, fact)

    fact_dims, edge_dims, regrained = planner.dimensions_by_island(
        islands, fact, edges, combine_filters
    )
    # Partial results count a row of an object the joins repeat once per
    # value the fact leg groups by beyond the query, and other objects'
    # measures beside a count of distinct values once per value it counts;
    # from rows, the combine step counts each once itself.
    rows_for = (
        rows_for
        or planner.repeats(edges, fact_dims, regrained.get(fact, []))
        or planner.counts_beside_others()
    )
    if rows_for:
        planner.check_rows(rows_for[0])
    from_rows = bool(rows_for)
    legs: list[FederatedLeg] = []
    combine_objects: dict[str, DataObject] = {}
    combine_dimensions: dict[str, Dimension] = {}
    combine_filters_static: list[ModelFilter] = []

    # Every column a leg gets that the model does not declare takes a name no
    # dimension, measure or metric has, nor another such column: a leg or the
    # combine model reading two columns of one name returned one for both.
    # Compared without case, as DuckDB compares identifiers, quoted or not.
    taken = {
        name.casefold() for name in (*model.effective_measures, *model.dimensions, *model.metrics)
    }
    # A combine filter on a dimension the query selects at a time grain reads
    # it at its own grain, from a column of its own: by island, each column's
    # name with the dimension's.
    own_grain = {
        island: {_unused(f"__og{island}_{n}", taken): name for n, name in enumerate(names)}
        for island, names in regrained.items()
    }
    # The edge keys, one name per key column, the same in both legs it joins.
    key_names = [
        [_unused(f"__fk{e}_{i}", taken) for i in range(len(edge.from_columns))]
        for e, edge in enumerate(edges)
    ]

    # The fact leg: the island's dimensions, the edge keys and the measures.
    fact_keys = {
        key_names[e][i]: (edge.from_object, column)
        for e, edge in enumerate(edges)
        for i, column in enumerate(edge.from_columns)
    }
    measures = planner.atomic_measures()
    # The leg column counting the rows each of its rows stands for, and the
    # leg column of each column a measure reads, when the leg returns rows.
    row_count: str | None = None
    measure_columns: dict[tuple[str, str, bool], str] = {}
    # The measures of each object read through the fact rows, and its identity.
    parents: dict[str, list[str]] = {}
    identities: dict[str, list[str]] = {}
    if from_rows:
        # A key read in its own time is a copy of its column, under the key's
        # name, which no column of its data object may have.
        taken |= {
            c.casefold()
            for island in islands
            for o in island
            for c in model.data_objects[o].columns
        }
        parents = planner.parents()
        identities = {
            parent: dedup_identity(parent, names, resolved, model)
            for parent, names in parents.items()
        }
        leg_measures, row_count, distinct, measure_columns = _row_measures(
            model, measures, taken, resolved.base_object, identities
        )
        averages: dict[str, tuple[str, str, int | None]] = {}
        variances: dict[str, tuple[str, str, str, int]] = {}
    else:
        leg_measures, averages, distinct, variances = _leg_measures(model, measures, taken)
    # The values a count of distinct values counts: the fact leg groups by them
    # too, so the combine step counts them across every group they reach.
    fact_values = {column: (obj, col) for column, obj, col in distinct.values()}
    # A column a measure reads of another data source comes from that
    # source's dimension leg, beside its keys; the rest from the fact leg.
    island_legs = {edge.island: f"leg_{e}" for e, edge in enumerate(edges, start=1)}
    elsewhere = {
        read: island_legs[i]
        for read in measure_columns
        for i, island in enumerate(islands)
        if read[0] in island and i != fact
    }
    row_values = {
        column: (obj, col)
        for (obj, col, own), column in measure_columns.items()
        if (obj, col, own) not in elsewhere
    }
    own_zone = {
        column
        for (obj, col, own), column in measure_columns.items()
        if own and (obj, col, own) not in elsewhere
    }
    in_own_zone = frozenset((row_values[column][0], column) for column in own_zone)
    fact_own_grain = own_grain.get(fact, {})
    fact_model = planner.island_model(
        islands[fact],
        {**fact_keys, **fact_values, **row_values},
        leg_measures,
        own_zone=own_zone,
        own_grain=fact_own_grain,
    )
    fact_query = QueryObject(
        select=QuerySelect(
            dimensions=[*fact_dims, *fact_own_grain, *fact_keys, *fact_values, *row_values],
            measures=sorted(leg_measures),
        ),
        where=leg_filters.get(fact, []),
        limit=leg_limit,
    )
    fact_columns = [
        *[_dimension_column(model, DimensionRef.parse(d).name) for d in fact_dims],
        *[_dimension_column(model, name, column) for column, name in fact_own_grain.items()],
        *[
            _key_column(model, name, obj, col)
            for name, (obj, col) in {**fact_keys, **fact_values}.items()
        ],
        *[_value_column(model, name, obj, col) for name, (obj, col) in row_values.items()],
        *[
            DataObjectColumn(name=n, code=n, abstract_type=m.result_type)
            for n, m in sorted(leg_measures.items())
        ],
    ]
    # Values the combine step groups by or counts are compared there as their
    # own engine compares them (:meth:`Dialect.equality_key`): the selected
    # dimensions without a grain, and the values a count of distinct values
    # counts. A rewritten dimension a combine filter reads keeps its value
    # too, under a name of its own, so the filter compares what it compared.
    grouped = {
        DimensionRef.parse(d).name
        for d in query.select.dimensions
        if isinstance(d, str) and not DimensionRef.parse(d).grain
    }
    filtered = {f for item in combine_filters for f in _filter_fields(item)}
    kept = {name: _unused(f"__eqv{n}", taken) for n, name in enumerate(sorted(grouped & filtered))}
    renamed = {name: column for found in own_grain.values() for column, name in found.items()}
    fact_columns, fact_rekeyed, fact_kept = _rekeyed(
        planner.leg_dialect(islands[fact]),
        fact_columns,
        {
            **{DimensionRef.parse(d).name: False for d in fact_dims},
            **dict.fromkeys(fact_values, True),
        },
        grouped,
        kept,
    )
    renamed.update({source: hidden for hidden, source in fact_kept.items()})
    squares = {squared: scale for _, _, squared, scale in variances.values()}
    with (
        partial_sums(),
        partial_squares(planner.leg_dialect(islands[fact]), squares),
        columns_in_own_zone(in_own_zone),
    ):
        legs.append(
            planner.leg(
                "leg_0",
                islands[fact],
                fact_query,
                fact_model,
                fact_columns,
                compile_leg,
                exact_sums=tuple(
                    (sums, scale) for sums, _, scale in averages.values() if scale is not None
                ),
                integer_sums=tuple(
                    part
                    for _, sums, squared, scale in variances.values()
                    for part in ((sums, scale), (squared, 2 * scale))
                ),
                rows=row_count,
                rekeyed=fact_rekeyed,
                kept=fact_kept,
            )
        )
    injections: list[KeyInjection] = []
    fact_joins: list[DataObjectJoin] = []
    # Each object measures read through the fact rows, from rows, is its own
    # relation in the combine model: its rows once each (_with_rows), joined
    # from the fact rows many-to-one, holding what its measures read, and the
    # dimensions and keys the query reads of it and of the objects it joins
    # many-to-one, as one database reads them there. The combine step's own
    # grain_dedup then counts each row once per group where the joins repeat
    # it, and puts the rows no fact row reaches in the groups they belong to,
    # as one database does.
    once = {parent: f"{_ONCE_RELATION}{i}" for i, parent in enumerate(sorted(parents))}
    once_of = {name: once[parent] for parent, names in parents.items() for name in names}
    once_joins: dict[str, list[DataObjectJoin]] = {}
    on_once = {
        column: once[parent]
        for column, parent in planner.read_once(
            parents, fact_dims, edges, key_names, fact_own_grain
        ).items()
    }
    on_once |= {
        hidden: on_once[source] for hidden, source in fact_kept.items() if source in on_once
    }
    for d in fact_dims:
        name = DimensionRef.parse(d).name
        combine_dimensions[name] = _combine_dimension(model, name, on_once.get(name, "leg_0"))
    for hidden, source in fact_kept.items():
        view = on_once.get(hidden, "leg_0")
        combine_dimensions[hidden] = _combine_dimension(model, source, view, hidden)
    for column, name in fact_own_grain.items():
        view = on_once.get(column, "leg_0")
        combine_dimensions[column] = _combine_dimension(model, name, view, column)

    # One leg per dimension island: its keys and the dimensions read there.
    for e, edge in enumerate(edges, start=1):
        leg_name = f"leg_{e}"
        keys = {
            key_names[e - 1][i]: (edge.to_object, column)
            for i, column in enumerate(edge.to_columns)
        }
        dims = edge_dims[e - 1]
        dim_own_grain = own_grain.get(edge.island, {})
        island = islands[edge.island]
        # A static model filter on this island runs in the combine step, on
        # the column this leg returns for it, as it runs on a left-joined
        # object in one database: there it drops the fact rows whose object
        # fails it, and keeps an unmatched one when the predicate holds for
        # NULL. Applied in the leg instead, a fact row whose object it
        # removed would come back NULL-joined, and pass ``IS NULL``. An
        # exists subquery the leg runs still applies it in its body.
        after_join = island - planner.in_subqueries
        static = {
            _unused(f"__sf{e - 1}_{n}", taken): (f.data_object, f.column, f)
            for n, f in enumerate(model.filters)
            if f.data_object in after_join
        }
        static_columns = {name: (obj, col) for name, (obj, col, _) in static.items()}
        # The columns measures read here, as the fact leg returns its own.
        read_here = {
            column: (obj, col)
            for (obj, col, own), column in measure_columns.items()
            if elsewhere.get((obj, col, own)) == leg_name
        }
        read_own_zone = {
            column
            for (obj, col, own), column in measure_columns.items()
            if own and column in read_here
        }
        dim_model = planner.island_model(
            island,
            {**keys, **static_columns, **read_here},
            {},
            own_zone=read_own_zone,
            own_grain=dim_own_grain,
        )
        dim_query = QueryObject(
            select=QuerySelect(
                dimensions=[*dims, *dim_own_grain, *keys, *static_columns, *read_here]
            ),
            where=leg_filters.get(edge.island, []),
            limit=leg_limit,
        )
        dim_columns = [
            *[_dimension_column(model, DimensionRef.parse(d).name) for d in dims],
            *[_dimension_column(model, name, column) for column, name in dim_own_grain.items()],
            *[
                _key_column(model, name, obj, col)
                for name, (obj, col) in {**keys, **static_columns}.items()
            ],
            *[_value_column(model, name, obj, col) for name, (obj, col) in read_here.items()],
        ]
        dim_columns, dim_rekeyed, dim_kept = _rekeyed(
            planner.leg_dialect(island),
            dim_columns,
            {DimensionRef.parse(d).name: False for d in dims},
            grouped,
            kept,
        )
        renamed.update({source: hidden for hidden, source in dim_kept.items()})
        with (
            columns_in_own_zone(frozenset((read_here[c][0], c) for c in read_own_zone)),
            static_filters_after_join(after_join),
        ):
            legs.append(
                planner.leg(
                    leg_name,
                    island,
                    dim_query,
                    dim_model,
                    dim_columns,
                    compile_leg,
                    rekeyed=dim_rekeyed,
                    kept=dim_kept,
                )
            )
        combine_objects[leg_name] = DataObject(
            name=leg_name,
            code=leg_name,
            database="",
            schema_name="",
            columns={c.name: c for c in dim_columns},
            countable=False,
        )
        combine_filters_static.extend(
            f.model_copy(update={"data_object": leg_name, "column": name})
            for name, (_, _, f) in static.items()
        )
        if edge.island in leg_filters:
            # The leg dropped the rows its filters reject: the combine step
            # keeps the fact rows it matched, which joins it also when the
            # query reads nothing else there.
            combine_filters_static.append(
                ModelFilter(
                    data_object=leg_name,
                    column=next(iter(keys)),
                    operator=FilterOperator.IS_NOT_NULL,
                    enforce=FilterEnforcement.ALWAYS,
                )
            )
            # So the fact leg may read only the keys this leg returns.
            key_type = _injectable_key_type(model, edge)
            if key_type is not None:
                injections.append(KeyInjection(leg_name, next(iter(keys)), key_type))
        for d in dims:
            name = DimensionRef.parse(d).name
            combine_dimensions[name] = _combine_dimension(model, name, leg_name)
        for hidden, source in dim_kept.items():
            combine_dimensions[hidden] = _combine_dimension(model, source, leg_name, hidden)
        for column, name in dim_own_grain.items():
            combine_dimensions[column] = _combine_dimension(model, name, leg_name, column)
        owner = on_once.get(next(iter(keys)))
        (once_joins.setdefault(owner, []) if owner else fact_joins).append(
            DataObjectJoin(
                join_type=Cardinality.MANY_TO_ONE,
                join_to=leg_name,
                columns_from=list(keys),
                columns_to=list(keys),
                required=edge.required,
            )
        )

    # One leg per nonexists on a dimension island: the island's keys the
    # subquery holds for, which the fact rows' keys must not be among.
    edge_of = {edge.island: e for e, edge in enumerate(edges)}
    for n, missing in enumerate(absent, start=len(legs)):
        leg_name = f"leg_{n}"
        e = edge_of[missing.island]
        keys = {
            key_names[e][i]: (edges[e].to_object, column)
            for i, column in enumerate(edges[e].to_columns)
        }
        key_columns = [_key_column(model, name, obj, col) for name, (obj, col) in keys.items()]
        holds = missing.item.model_copy(update={"op": FilterOperator.EXISTS})
        island = islands[missing.island]
        with static_filters_after_join(island - planner.in_subqueries):
            legs.append(
                planner.leg(
                    leg_name,
                    island,
                    QueryObject(
                        select=QuerySelect(dimensions=[*keys]), where=[holds], limit=leg_limit
                    ),
                    planner.island_model(island, keys, {}),
                    key_columns,
                    compile_leg,
                )
            )
        combine_objects[leg_name] = DataObject(
            name=leg_name,
            code=leg_name,
            database="",
            schema_name="",
            columns={c.name: c for c in key_columns},
            countable=False,
        )
        combine_filters_static.append(
            ModelFilter(
                data_object=leg_name,
                column=next(iter(keys)),
                operator=FilterOperator.IS_NULL,
                enforce=FilterEnforcement.ALWAYS,
            )
        )
        owner = on_once.get(next(iter(keys)))
        (once_joins.setdefault(owner, []) if owner else fact_joins).append(
            DataObjectJoin(
                join_type=Cardinality.MANY_TO_ONE,
                join_to=leg_name,
                columns_from=list(keys),
                columns_to=list(keys),
            )
        )

    once_columns: dict[str, tuple[list[str], list[str]]] = {}
    effective = model.effective_measures
    for parent, names in sorted(parents.items()):
        name = once[parent]
        identity_columns = [
            measure_columns[(parent, column, False)] for column in identities[parent]
        ]
        read = set(identity_columns)
        for measure in names:
            if measure in distinct:
                read.add(distinct[measure][0])
            else:
                read |= {measure_columns[r] for r in _read_columns(effective[measure])}
        values = {
            leg: _value_column(model, leg, *row_values[leg]).model_copy(
                update={"primary_key": leg in identity_columns}
            )
            for leg in sorted(read & row_values.keys())
        }
        held = {
            c.name: c
            for c in fact_columns
            if on_once.get(c.name) == name or c.name in read - values.keys()
        }
        once_columns[name] = ([*values, *held], identity_columns)
        combine_objects[name] = DataObject(
            name=name,
            code=name,
            database="",
            schema_name="",
            columns={**values, **held},
            joins=once_joins.get(name, []),
            countable=False,
        )
        fact_joins.append(
            DataObjectJoin(
                join_type=Cardinality.MANY_TO_ONE,
                join_to=name,
                columns_from=identity_columns,
                columns_to=identity_columns,
            )
        )

    combine_objects["leg_0"] = DataObject(
        name="leg_0",
        # The leg with each row repeated as often as it counts (_with_rows).
        code=_ROWS_RELATION if row_count else "leg_0",
        database="",
        schema_name="",
        columns={c.name: c for c in fact_columns if c.name != row_count},
        joins=fact_joins,
        countable=False,
    )
    # The fact leg column each combined measure reads, where not its own name.
    leg_columns = {
        **{m: sums for m, (sums, _, _) in averages.items()},
        **{m: values for m, (values, _, _) in distinct.items()},
        **{m: sums for m, (_, sums, _, _) in variances.items()},
    }
    combine_model = SemanticModel(
        name=model.name,
        # What types and buckets the combined values; not the query's time
        # zone, which the legs read their timestamps in already.
        settings=ModelSettings(default_dialect=COMBINE_DIALECT).model_copy(
            update=(
                {
                    "week_start": model.settings.week_start,
                    "default_numeric_data_type": model.settings.default_numeric_data_type,
                }
                if model.settings
                else {}
            )
        ),
        data_objects=combine_objects,
        dimensions=combine_dimensions,
        measures={
            m: (
                _row_measure(model, m, measure_columns, once_of.get(m, "leg_0"), elsewhere)
                if from_rows and m not in distinct
                else _combine_measure(model, m, leg_columns.get(m), once_of.get(m, "leg_0"))
            )
            for m in measures
        },
        metrics={n: m for n, m in model.metrics.items() if m.type == MetricType.DERIVED},
        filters=combine_filters_static,
        expose_counts=False,
    )
    combine_query = query.model_copy(
        update={
            "select": QuerySelect(
                dimensions=[DimensionRef.parse(str(d)).name for d in query.select.dimensions],
                measures=list(query.select.measures),
            ),
            "where": [_renamed(item, renamed) for item in combine_filters],
            "order_by": [
                QueryOrderBy(
                    field=DimensionRef.parse(o.field).name, direction=o.direction, nulls=o.nulls
                )
                for o in query.order_by
            ],
            "use_path_names": [],
        }
    )
    combiner = DialectRegistry.get(COMBINE_DIALECT)
    with (
        combined_averages(combiner, {m: (c, scale) for m, (_, c, scale) in averages.items()}),
        combined_variances(
            combiner, {m: (c, sq, scale) for m, (c, _, sq, scale) in variances.items()}
        ),
    ):
        combine_sql = compile_leg(combine_query, combine_model, COMBINE_DIALECT)
    if row_count:
        combine_sql = _with_rows(combine_sql, combiner, "leg_0", row_count, once_columns)
    inject = None
    if injections:
        inject = KeyInjector(
            leg=legs[0],
            query=fact_query.model_copy(deep=True),
            model=fact_model.model_copy(deep=True),
            columns=tuple(fact_columns),
            squares=squares,
            own_zone=in_own_zone,
            rekeyed=fact_rekeyed,
            kept=fact_kept,
            compile_leg=compile_leg,
        )
    return FederatedPlan(
        legs=tuple(legs),
        combine_sql=combine_sql,
        injections=tuple(injections),
        inject=inject,
    )


@dataclass(frozen=True)
class _Edge:
    """A many-to-one join from the fact island into a dimension island."""

    from_object: str
    to_object: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    island: int
    required: bool


@dataclass(frozen=True)
class _Absent:
    """A ``nonexists`` filter on a dimension island, and the island.

    The combine step keeps the fact rows whose key is not among those of
    the island's rows the subquery holds for: a leg of its own returns those
    keys (``exists``), and a fact row whose key has no row in the island at
    all is kept, as one database keeps it.
    """

    island: int
    item: QueryFilter


class _Planner:
    def __init__(
        self,
        query: QueryObject,
        model: SemanticModel,
        resolved: ResolvedQuery,
        dialect_name: str,
        source_dialect: Callable[[str | None], str | None],
    ) -> None:
        self.query = query
        self.dialect_name = dialect_name
        self.source_dialect = source_dialect
        self.model = model
        self.resolved = resolved
        self.sources = object_sources(model)
        self.objects = query_objects(resolved, query, model)
        # The objects only an ``exists`` subquery reads: the outer query of a
        # leg does not join them, and their static filters apply in the body.
        self.in_subqueries = self.objects - _outer_objects(resolved)

    # -- refusals -----------------------------------------------------------

    def refuse(self, code: str, message: str, hint: str | None = None) -> ResolutionError:
        return ResolutionError(
            [
                SemanticError(
                    code=f"FEDERATION_{code}",
                    message=f"{message} (the query reads data objects of several data sources)",
                    hint=hint,
                )
            ]
        )

    def check_query_shape(self) -> None:
        q = self.query
        unsupported = {
            "raw mode (select.fields)": bool(q.select.fields),
            "coalesce dimensions": any(not isinstance(d, str) for d in q.select.dimensions),
            "dimensionsExclude": q.dimensions_exclude,
            "fill": q.fill,
            "asOf": q.as_of is not None,
            "usePathNames": bool(q.use_path_names),
        }
        for feature, used in unsupported.items():
            if used:
                raise self.refuse("UNSUPPORTED_QUERY", f"A federated query cannot use {feature}")
        names = [DimensionRef.parse(d).name for d in q.select.dimensions if isinstance(d, str)]
        if len(names) != len(set(names)):
            raise self.refuse(
                "UNSUPPORTED_QUERY",
                "A federated query cannot select one dimension at two grains",
            )
        for name in names:
            if self.model.dimensions[name].via:
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"Dimension '{name}' plays a role (via), which a federated query "
                    "does not support yet",
                )

    # -- islands and edges --------------------------------------------------

    def islands(self) -> list[set[str]]:
        """The query's objects grouped by source and joined within it.

        An ``exists`` subquery runs where its subject is read, so the objects
        it reads join its subject's island; they must be of its data source.
        """
        parent = {o: o for o in self.objects}

        def find(o: str) -> str:
            while parent[o] != o:
                parent[o] = parent[parent[o]]
                o = parent[o]
            return o

        for step in self.resolved.join_steps:
            a, b = step.from_object, step.to_object
            if a in parent and b in parent and self.sources.get(a) == self.sources.get(b):
                parent[find(a)] = find(b)
        for name in self.objects:
            nested = self.model.data_objects[name].nested_in
            if nested is not None and nested.data_object in parent:
                parent[find(name)] = find(nested.data_object)
        for item in _exists_filters(self.query.where):
            subject = _exists_subject(item, self.model) or ""
            read = _exists_objects(item, self.model)
            apart = sorted(o for o in read if self.sources.get(o) != self.sources.get(subject))
            if apart:
                raise self.refuse(
                    "UNSUPPORTED_FILTER",
                    f"The {item.op.value} filter on '{item.field}' reads {', '.join(apart)}, "
                    f"of another data source than '{subject}', which it correlates to",
                    hint="Keep an exists subquery on the data source of its subject.",
                )
            for o in read:
                parent[find(o)] = find(subject)
        groups: dict[str, set[str]] = {}
        for o in sorted(self.objects):
            groups.setdefault(find(o), set()).add(o)
        return list(groups.values())

    def fact_island(self, islands: list[set[str]]) -> int:
        """The island of the measures, which must also hold the base object.

        A measure belongs to the island its values come from, its ``columns``
        and ``expression``: the base's, when they come from it and from
        another island too. That other island is read through the joins from
        the base's, as a dimension is (:meth:`federation_edges`), and so are
        the columns its filters or ordering read there; the combine step
        computes such a measure from rows (:meth:`check_measures`).
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        base = island_of[self.resolved.base_object]
        effective = self.model.effective_measures
        owners: set[int] = set()
        for name in self.atomic_measures():
            homes = {island_of[o] for o in effective[name].source_objects if o in island_of}
            owners |= {base} if base in homes else homes
        if len(owners) > 1:
            listed = "; ".join(
                f"{self._island_source(islands[i])}: {', '.join(sorted(islands[i]))}"
                for i in sorted(owners)
            )
            raise self.refuse(
                "MULTI_FACT",
                f"The query's measures are read from more than one data source ({listed})",
                hint="Measures of one query must come from one data source for now.",
            )
        if owners and owners != {base}:
            (fact,) = owners
            raise self.refuse(
                "UNSUPPORTED_JOIN",
                f"The measures, read from {', '.join(sorted(islands[fact]))} "
                f"({self._island_source(islands[fact])}), are grouped by dimensions of "
                f"{', '.join(sorted(islands[base]))} ({self._island_source(islands[base])}), "
                "on the many side of their join",
                hint="Group them by dimensions of their own data source, or of one their "
                "rows join many-to-one.",
            )
        return base

    def federation_edges(self, islands: list[set[str]], fact: int) -> list[_Edge]:
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        edges: list[_Edge] = []
        reached: set[int] = set()
        for step in self.resolved.join_steps:
            a, b = step.from_object, step.to_object
            if island_of.get(a) == island_of.get(b):
                continue
            label = f"'{a}' -> '{b}'"
            if island_of.get(a) != fact:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"The join {label} connects two data sources away from the measures' "
                    "data source",
                    hint="A federated query joins its measures' data source directly to "
                    "each other one, for now.",
                )
            if step.reversed or step.cardinality != Cardinality.MANY_TO_ONE:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"The join {label} between data sources is not many-to-one from the "
                    "measures' side",
                )
            target = island_of[b]
            if target in reached:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"The data source of '{b}' is joined more than once",
                )
            reached.add(target)
            declared = self._declared_join(a, b, step.from_columns, step.to_columns)
            edges.append(
                _Edge(
                    from_object=a,
                    to_object=b,
                    from_columns=tuple(step.from_columns),
                    to_columns=tuple(step.to_columns),
                    island=target,
                    required=declared.required if declared else False,
                )
            )
        for i, island in enumerate(islands):
            if i != fact and i not in reached:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"Data objects {', '.join(sorted(island))} are not joined to the "
                    "measures' data source",
                )
        for edge in edges:
            self._check_dimension_island(islands[edge.island], edge.to_object)
        return edges

    def _check_dimension_island(self, island: set[str], entry: str) -> None:
        """Every object of a dimension island is reached from its entry many-to-one.

        Otherwise the dimension leg would return several rows per key, and
        joining it would multiply the measures.
        """
        reached = {entry}
        changed = True
        while changed:
            changed = False
            for step in self.resolved.join_steps:
                if (
                    step.from_object in reached
                    and step.to_object in island
                    and step.to_object not in reached
                ):
                    if step.reversed or step.cardinality != Cardinality.MANY_TO_ONE:
                        raise self.refuse(
                            "UNSUPPORTED_JOIN",
                            f"'{step.from_object}' -> '{step.to_object}' would repeat the "
                            f"rows of '{entry}' in its data source",
                        )
                    reached.add(step.to_object)
                    changed = True
        for name in island - reached:
            nested = self.model.data_objects[name].nested_in
            if nested is not None:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"Nested data object '{name}' is not supported on the dimension side "
                    "of a federated query",
                )

    def _declared_join(
        self, a: str, b: str, from_columns: list[str], to_columns: list[str]
    ) -> DataObjectJoin | None:
        for join in self.model.data_objects[a].joins:
            if join.join_to == b and list(join.columns_from) == list(from_columns):
                return join
        return None

    def _island_source(self, island: Iterable[str]) -> str:
        names = {source_name(self.model, o) for o in island}
        return ", ".join(sorted(n or "no data source" for n in names))

    # -- measures -----------------------------------------------------------

    def atomic_measures(self) -> set[str]:
        """The measures the fact leg computes: selected, and those metrics read."""
        effective = self.model.effective_measures
        # By name, not ``is_expression``: a measure written as an expression
        # is one too, and only a metric is left out.
        names = {m.name for m in self.resolved.measures if m.name in effective}
        names |= set(self.resolved.metric_components)
        for item in self.query.having:
            names |= {f for f in _filter_fields(item) if f in effective}
        names |= {o.field for o in self.query.order_by if o.field in effective}
        return names

    def check_measures(self, fact_island: set[str]) -> list[str]:
        """Refuse a measure no federated query computes; return those needing rows.

        A measure combines from partial results the fact leg computes per
        group (:meth:`combines`). When one does not - a median, a
        correlation, a variance of floats, an average with a total - the fact
        leg returns its rows instead, and the combine step computes every
        measure from them (:meth:`check_rows`). So it does when a measure
        reads a column of another data source, in its expression, a filter or
        its ordering: the dimension leg of that source returns the column
        beside its keys, and the combine step computes the measure after the
        join, as one database computes it over the joined rows.
        """
        for rm in self.resolved.measures:
            if rm.is_cumulative or rm.is_pop or rm.is_window or rm.is_reaggregate:
                raise self.refuse(
                    "UNSUPPORTED_METRIC",
                    f"Metric '{rm.name}' is not a derived metric, which is all a "
                    "federated query supports yet",
                )
        effective = self.model.effective_measures
        names = sorted(self.atomic_measures())
        for name in names:
            measure = effective[name]
            if measure.filter_context is not None or measure.grain is not None:
                raise self.refuse(
                    "UNSUPPORTED_AGGREGATION",
                    f"Measure '{name}' sets filterContext or grain, which a federated "
                    "query does not support yet",
                )
            for obj, column, _ in _read_columns(measure):
                apart = sorted(
                    o
                    for o in self._column_objects(obj, column)
                    if self.sources.get(o) != self.sources.get(obj)
                )
                if apart:
                    raise self.refuse(
                        "MEASURE_ACROSS_SOURCES",
                        f"Measure '{name}' reads '{obj}.{column}', which is computed from "
                        f"{', '.join(apart)}, of another data source",
                        hint="Compute the column from columns of its own data source.",
                    )
        across = {
            name
            for name in names
            if any(obj not in fact_island for obj, _, _ in _read_columns(effective[name]))
        }
        for parent, owned in self.parents().items():
            if reading := sorted(across.intersection(owned)):
                raise self.refuse(
                    "MEASURE_ACROSS_SOURCES",
                    f"Measure '{reading[0]}' is counted once per row of '{parent}', and "
                    "reads a column of another data source, which a federated query does "
                    "not support yet",
                    hint=f"Query '{reading[0]}' on one data source.",
                )
        return [
            name
            for name in names
            if name in across or not self.combines(effective[name], fact_island)
        ]

    def combines(self, measure: Measure, fact_island: set[str]) -> bool:
        """Whether *measure* combines from the fact leg's partial results (:func:`_leg_measures`).

        Sums, counts, minima, maxima and any value re-aggregate; an average
        divides its summed sums by its summed counts; a count of the distinct
        values of one column counts them across the groups they reach. A
        variance or standard deviation combines from its moments when its
        values are integers or decimals the leg squares exactly
        (:meth:`squares_exactly`). None of them with a total but the
        re-aggregated ones, nor any other aggregation.
        """
        if _counts_distinct(measure):
            return _counts_distinct_column(measure) and not measure.total
        if measure.distinct:
            return False
        if measure.aggregation in _REAGGREGATE:
            return True
        if measure.aggregation == AggregationType.AVG:
            return not measure.total
        if measure.aggregation in _VARIANCES:
            return not measure.total and self.squares_exactly(measure, fact_island)
        return False

    def squares_exactly(self, measure: Measure, fact_island: set[str]) -> bool:
        """Whether the fact leg squares the values of a variance exactly.

        The combine step computes it from each group's count, sum and sum of
        squares, which add up exactly only when no value is a float: the
        values are integers, or a decimal column declared with its width
        (:func:`~orionbelt.compiler.type_resolver.exact_input_scale`), each
        squared in the fact leg's engine in a type wide enough for its places
        (:meth:`Dialect.exact_square`).
        """
        scale = exact_input_scale(measure, self.model)
        if scale is None or scale > _VARIANCE_MOST_PLACES:
            return False
        dialect = self.leg_dialect(fact_island)
        return dialect.exact_square(ColumnRef(name=measure.name), scale) is not None

    def check_rows(self, reason: str) -> None:
        """Refuse what the combine step cannot compute from the fact leg's rows.

        The fact leg returns the rows of the query's base object grouped by
        everything the combine step reads of them, each group with the
        number of rows it stands for, and the combine step computes every
        measure from that many copies (:func:`_row_measures`). What does not
        survive it is a value the leg's engine compares otherwise than
        DuckDB, where a measure compares it (:func:`_compared_columns`): the
        combine step compares it as DuckDB does (:meth:`Dialect.equality_key`).
        A count of the distinct values of one column is counted as its engine
        counts, as when the leg combines partial results.

        A measure of an object read through the fact rows (:meth:`parents`)
        is computed from that object's rows once each: what it reads must have
        one value per row of it, as columns of the object and of those it joins
        many-to-one have.

        *reason* is the measure that needs the rows.
        """
        effective = self.model.effective_measures
        for parent, names in self.parents().items():
            side = self._one_row_each(parent)
            for name in names:
                for obj, column, _ in _read_columns(effective[name]):
                    if not self._column_objects(obj, column) <= side:
                        raise self.refuse(
                            "UNSUPPORTED_AGGREGATION",
                            f"{_from_rows(reason, name)}, from the rows of '{parent}' once "
                            f"each, but it reads '{obj}.{column}', which one row of "
                            f"'{parent}' can have several values of",
                            hint=_apart(reason, name),
                        )
        for name in sorted(self.atomic_measures()):
            measure = effective[name]
            if _counts_distinct_column(measure):
                continue
            for obj, column, counted in _compared_columns(measure):
                # Read on its own data source: the fact leg's, or another's.
                dialect = self.leg_dialect({obj})
                kind = self.model.data_objects[obj].columns[column].abstract_type.value
                if dialect.equality_key(ColumnRef(name=column), kind, counted=counted) is not None:
                    raise self.refuse(
                        "UNSUPPORTED_AGGREGATION",
                        f"{_from_rows(reason, name)}, in DuckDB, which compares the values "
                        f"of '{obj}.{column}' otherwise than {dialect.name}",
                        hint=_apart(reason, name),
                    )

    def repeats(
        self, edges: list[_Edge], fact_dims: list[str], own_grain: Iterable[str]
    ) -> list[str]:
        """The measures counted once per row of their object that partial results repeat.

        One database aggregates a measure of an object the query's joins repeat
        (customers' ages over their sales) over that object's distinct rows,
        per group of the query's dimensions (``grain_dedup``). The fact leg
        does the same, but per group of what *it* groups by, which is more:
        the federation keys, the dimensions only a filter after the join reads
        (*fact_dims* beyond the selected ones, and those selected at a time
        grain it reads at their own, *own_grain*), and the values a count of
        distinct values counts (:func:`_leg_measures`). The selected ones
        count too where the combine step adds groups across them: a
        ``rollup`` or ``cube`` subtotal, and a measure's ``total``. A customer
        whose sales reach two products is one row in each product's group,
        and the combine step, adding the groups, counted it twice. Measured,
        the variance of ages 10 and 20 came out 33.3 for 50 and their sum 40
        for 30, in a group, a subtotal and a total alike.

        Exact when each of those is read from that object, or from one it
        joins many-to-one, so each of its rows has one value. Otherwise the
        fact leg returns rows, and the combine step counts each row of the
        object once itself (:func:`_row_measures`). An aggregation repeated
        rows do not change (``min``, ``count_distinct``) combines either way.
        """
        model = self.model
        selected = {
            DimensionRef.parse(d).name for d in self.query.select.dimensions if isinstance(d, str)
        }

        # The objects each value the fact leg groups by beyond the query's
        # selected dimensions is read from.
        always = [
            set().union(*(self._column_objects(edge.from_object, c) for c in edge.from_columns))
            for edge in edges
        ]
        fact_names = [DimensionRef.parse(d).name for d in fact_dims]
        always += [self._dimension_objects(name) for name in fact_names if name not in selected]
        always += [self._dimension_objects(name) for name in own_grain]
        effective = model.effective_measures
        for name in self.atomic_measures():
            if _counts_distinct_column(effective[name]):
                ref = effective[name].columns[0]
                always.append(self._column_objects(ref.view or "", ref.column or ""))

        repeated = []
        for name, target in sorted(self.resolved.dedup_targets.items()):
            measure = effective[name]
            if measure.distinct or measure.aggregation in MULTIPLICITY_SAFE_AGGREGATIONS:
                continue
            groupings = list(always)
            if self.query.grouping is not None or measure.total:
                # The combine step adds groups across the selected ones too.
                groupings += [self._dimension_objects(d) for d in fact_names if d in selected]
            one = self._one_row_each(target)
            if any(not objects <= one for objects in groupings):
                repeated.append(name)
        return repeated

    def parents(self) -> dict[str, list[str]]:
        """The objects measures are read from through the base's rows, each with its measures.

        As :meth:`~orionbelt.compiler.resolution.QueryResolver._parent_of` has
        it: a measure whose values come from one object, not the base. One
        database counts each row of such an object once where the joins repeat
        it (``grain_dedup``), and adds its rows no base row reaches, customers
        without sales.
        """
        base = self.resolved.base_object
        effective = self.model.effective_measures
        parents: dict[str, list[str]] = {}
        for name in sorted(self.atomic_measures()):
            sources = effective[name].source_objects
            if len(sources) == 1 and (source := next(iter(sources))) != base:
                parents.setdefault(source, []).append(name)
        return parents

    def read_once(
        self,
        parents: Iterable[str],
        fact_dims: list[str],
        edges: list[_Edge],
        key_names: list[list[str]],
        own_grain: dict[str, str],
    ) -> dict[str, str]:
        """The fact leg's dimensions and keys read through an object measures read, each to it.

        The dimensions at their own grain too (*own_grain*: each column's name
        with the dimension's).

        Such an object (:meth:`parents`, from rows) and the objects it joins
        many-to-one have one row per row of it: one database reads those
        dimensions and keys through it, also for its rows no fact row reaches.
        One read through two such objects is refused: whose rows without a
        fact row it groups is not one answer.
        """
        sides = {parent: self._one_row_each(parent) for parent in parents}

        def through(objects: set[str], what: str) -> str | None:
            owners = sorted(parent for parent, side in sides.items() if objects <= side)
            if len(owners) > 1:
                raise self.refuse(
                    "UNSUPPORTED_AGGREGATION",
                    f"The query reads {what} through each of {', '.join(owners)}, whose "
                    "measures are computed from the fact leg's rows: which one's rows "
                    "without a fact row it groups is not one answer",
                    hint="Query the measures of each object apart.",
                )
            return owners[0] if owners else None

        read: dict[str, str] = {}
        for name in (DimensionRef.parse(d).name for d in fact_dims):
            if owner := through(self._dimension_objects(name), f"dimension '{name}'"):
                read[name] = owner
        for column, name in own_grain.items():
            if owner := through(self._dimension_objects(name), f"dimension '{name}'"):
                read[column] = owner
        for edge, names in zip(edges, key_names, strict=True):
            objects = set().union(
                *(self._column_objects(edge.from_object, c) for c in edge.from_columns)
            )
            what = f"the key of the join from '{edge.from_object}' to '{edge.to_object}'"
            if owner := through(objects, what):
                read.update(dict.fromkeys(names, owner))
        return read

    def _column_objects(self, obj: str, column: str) -> set[str]:
        """*obj* and the objects its *column* reads, a computed one's dependencies."""
        return {obj} | self.model.column_reference_objects(obj, column)

    def _dimension_objects(self, name: str) -> set[str]:
        """The objects dimension *name* reads."""
        model = self.model
        return {model.dimensions[name].view or ""} | model.dimension_join_objects(name)

    def _one_row_each(self, target: str) -> set[str]:
        """*target* and the objects of the query with one row per row of it."""
        reached = {target}
        changed = True
        while changed:
            changed = False
            for step in self.resolved.join_steps:
                if step.nested or step.cardinality == Cardinality.MANY_TO_MANY:
                    continue
                pairs = [(step.from_object, step.to_object)]
                if step.cardinality == Cardinality.ONE_TO_ONE:
                    pairs.append((step.to_object, step.from_object))
                elif step.reversed:
                    # Traversed from the one side: the many side is to_object.
                    pairs = [(step.to_object, step.from_object)]
                for many, one in pairs:
                    if many in reached and one not in reached:
                        reached.add(one)
                        changed = True
        return reached

    def counts_beside_others(self) -> list[str]:
        """The counts of distinct values beside measures of other data objects.

        A fact leg of partial results groups by the counted column as well
        (:func:`_leg_measures`). Grouping an object's rows by one of its own
        columns leaves that object's sums, counts, minima and averages exact,
        since each combines from partial results. A measure of another object
        does not survive it: a customer-level sum is repeated once per order
        when the leg groups by order, and the rows the planner keeps for a
        count over customers - those without orders - are not the rows it
        keeps for a sum over orders. Such a query is computed from the fact
        leg's rows instead. The objects compared are all a measure reads, a
        computed column's dependencies included: a Customers column computed
        from Orders makes the counted values per order.
        """
        effective = self.model.effective_measures
        atomic = sorted(self.atomic_measures())
        distinct = [name for name in atomic if _counts_distinct_column(effective[name])]
        counted = {effective[name].columns[0].view for name in distinct}
        for name in atomic:
            measure = effective[name]
            # Every object a measure reads, the counted ones included: a
            # computed column, its own or a filter's, reads the objects its
            # expression names, so "a Customers column" can be per order.
            objects = measure.source_objects | self.model.measure_join_objects(name)
            if objects != counted or len(counted) != 1 or measure.anchor:
                return distinct
        return []

    # -- filters ------------------------------------------------------------

    def place_filters(
        self, islands: list[set[str]], fact: int
    ) -> tuple[dict[int, list[QueryFilterItem]], list[QueryFilterItem], list[_Absent]]:
        """Split ``where`` into what each island's leg runs and what the combine step runs.

        A condition on the fact island's dimensions alone runs in the fact
        leg. A condition on one dimension island's dimensions alone runs in
        that island's leg when it is never true where they are all NULL, and
        the combine step keeps only the fact rows that leg matched: an
        unmatched fact row reads NULL there, which the condition rejects, as
        it rejects the fact rows whose key the leg dropped. Any other runs only in
        the combine step, over the joined rows: pushed into a dimension leg, a
        predicate true for NULL (``IS NULL``) would also match the fact rows
        whose key that leg dropped.

        An ``exists`` filter is a condition on its subject. A ``nonexists``
        on a dimension island holds for a fact row with no row there, so it
        becomes the absence of the fact row's key among those the subquery
        holds for (:class:`_Absent`). The combine step cannot run a subquery:
        an ``exists`` that would run there is refused.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        legs: dict[int, list[QueryFilterItem]] = {}
        combine: list[QueryFilterItem] = []
        absent: list[_Absent] = []
        for item in self.query.where:
            owners = {island_of.get(o) for o in self._filter_objects(item)}
            owner = owners.pop() if len(owners) == 1 else None
            if owner is not None and (owner == fact or _on_null(item) is not True):
                legs.setdefault(owner, []).append(item)
                continue
            if (
                owner is not None
                and isinstance(item, QueryFilter)
                and item.op == FilterOperator.NONEXISTS
            ):
                absent.append(_Absent(owner, item))
                continue
            if any(True for _ in _exists_filters([item])):
                raise self.refuse(
                    "UNSUPPORTED_FILTER",
                    "An exists filter that runs after the join (beside a condition on "
                    "another data source, or in a group true where its subject is "
                    "missing) is not supported yet",
                    hint="Filter with exists apart, or beside conditions of its subject's "
                    "data source.",
                )
            combine.append(item)
        return legs, combine, absent

    def _filter_objects(self, item: QueryFilterItem) -> set[str]:
        """The data objects whose rows *item* is a condition on: an ``exists``'s subject."""
        if isinstance(item, QueryFilterGroup):
            return {o for sub in item.filters for o in self._filter_objects(sub)}
        if item.subquery is not None:
            return {_exists_subject(item, self.model) or ""}
        dimension = self.model.dimensions.get(DimensionRef.parse(item.field).name)
        if dimension is None:
            raise self.refuse(
                "UNSUPPORTED_FILTER",
                f"The filter on '{item.field}' is not on a dimension",
            )
        return {dimension.view or ""}

    def dimensions_by_island(
        self,
        islands: list[set[str]],
        fact: int,
        edges: list[_Edge],
        combine_filters: list[QueryFilterItem],
    ) -> tuple[list[str], list[list[str]], dict[int, list[str]]]:
        """The dimensions each leg returns: selected, or read by a combine filter.

        A leg returns a dimension once, at the grain the query selects it. A
        combine filter on one the query selects at a time grain compares it
        at its own grain, as one database does: its leg returns it so too, a
        column of its own. The third value holds those dimensions, by island.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        wanted = [d for d in self.query.select.dimensions if isinstance(d, str)]
        selected = {DimensionRef.parse(d).name for d in wanted}
        at_grain = {DimensionRef.parse(d).name for d in wanted if DimensionRef.parse(d).grain}
        regrained: dict[int, list[str]] = {}
        for item in combine_filters:
            for field in _filter_fields(item):
                if field in at_grain:
                    island = island_of[self.model.dimensions[field].view]
                    if field not in regrained.get(island, []):
                        regrained.setdefault(island, []).append(field)
                elif field not in selected:
                    wanted.append(field)
                    selected.add(field)
        fact_dims: list[str] = []
        edge_dims: list[list[str]] = [[] for _ in edges]
        edge_of = {edge.island: k for k, edge in enumerate(edges)}
        for d in wanted:
            island = island_of[self.model.dimensions[DimensionRef.parse(d).name].view]
            if island == fact:
                fact_dims.append(d)
            else:
                edge_dims[edge_of[island]].append(d)
        return fact_dims, edge_dims, regrained

    # -- legs ---------------------------------------------------------------

    def island_model(
        self,
        island: set[str],
        keys: dict[str, tuple[str, str]],
        measures: dict[str, Measure],
        *,
        own_zone: Iterable[str] = (),
        own_grain: dict[str, str] | None = None,
    ) -> SemanticModel:
        """The model a leg compiles against: the island alone, keys as dimensions.

        It keeps the static filters of the island's objects; a dimension leg
        leaves some to the combine step (:func:`static_filters_after_join`).
        Each of *own_grain* (a column's name, a dimension's) is that dimension
        under the column's name, at its own grain.
        *measures* are partial results (:func:`_leg_measures`): no ``total``,
        no ``defaultValue`` (an empty group's default would become an input to
        the combined aggregate), and a sum at a width that does not round
        before the partials are added (``partial_sums``). A key named in
        *own_zone* reads a copy of its column, under the key's name, which
        the leg compiles in its own time (:func:`columns_in_own_zone`): the
        column itself may be read in the query's time too.
        """
        model = self.model
        objects = {
            name: obj.model_copy(update={"joins": [j for j in obj.joins if j.join_to in island]})
            for name, obj in model.data_objects.items()
            if name in island
        }
        dimensions = {name: dim for name, dim in model.dimensions.items() if dim.view in island}
        for column, name in (own_grain or {}).items():
            dimensions[column] = model.dimensions[name].model_copy(update={"name": column})
        copies = set(own_zone)
        for key, (obj, column) in keys.items():
            col = model.data_objects[obj].columns[column]
            if key in copies:
                columns = {**objects[obj].columns, key: col.model_copy(update={"name": key})}
                objects[obj] = objects[obj].model_copy(update={"columns": columns})
                column = key
            dimensions[key] = Dimension(
                name=key, view=obj, column=column, result_type=col.abstract_type
            )
        return model.model_copy(
            update={
                "data_objects": objects,
                "dimensions": dimensions,
                "measures": measures,
                "metrics": {},
                "filters": [f for f in model.filters if f.data_object in island],
                "rules": {},
                "expose_counts": False,
                # Read by no compilation, and kept by a plan that injects keys.
                "description": None,
                "examples": [],
                "custom_extensions": [],
                "ontology": None,
                "external_concept_mappings": [],
            }
        )

    def leg_dialect(self, island: set[str]) -> Dialect:
        """The dialect the leg of *island* is written in."""
        source = source_name(self.model, next(iter(sorted(island))))
        return DialectRegistry.get(self.source_dialect(source) or self.dialect_name)

    def leg(
        self,
        name: str,
        island: set[str],
        query: QueryObject,
        model: SemanticModel,
        columns: list[DataObjectColumn],
        compile_leg: Callable[[QueryObject, SemanticModel, str], str],
        *,
        exact_sums: tuple[tuple[str, int], ...] = (),
        integer_sums: tuple[tuple[str, int], ...] = (),
        rows: str | None = None,
        rekeyed: dict[str, tuple[str, bool]] | None = None,
        kept: dict[str, str] | None = None,
    ) -> FederatedLeg:
        """One leg; *rekeyed* and *kept* as :func:`_rekeyed` returns them."""
        source = source_name(self.model, next(iter(sorted(island))))
        dialect = self.leg_dialect(island)
        sql = compile_leg(query, model, dialect.name)
        if rekeyed:
            sql = _rewrite_leg(dialect, sql, columns, rekeyed, kept or {})
        return FederatedLeg(
            name=name,
            data_source=source,
            dialect=dialect.name,
            sql=sql,
            data_objects=tuple(sorted(island)),
            columns=tuple((c.name, c.abstract_type.value) for c in columns),
            physical_tables=_physical_tables(self.model, island),
            exact_sums=exact_sums,
            integer_sums=integer_sums,
            rows=rows,
        )


def _injectable_key_type(model: SemanticModel, edge: _Edge) -> str | None:
    """The OBML type of *edge*'s key, when the fact leg may read a list of its values.

    One column of the same type on both sides, an integer or a string: their
    literals compare as the column does on every engine.
    """
    if len(edge.from_columns) != 1:
        return None
    (fact_column,), (dim_column,) = edge.from_columns, edge.to_columns
    key_type = model.data_objects[edge.from_object].columns[fact_column].abstract_type
    dim_type = model.data_objects[edge.to_object].columns[dim_column].abstract_type
    return key_type.value if key_type == dim_type and key_type in _INJECTABLE_KEYS else None


def _physical_tables(model: SemanticModel, objects: Iterable[str]) -> tuple[str, ...]:
    """The distinct ``DATABASE.SCHEMA.CODE`` tables *objects* read."""
    tables: dict[str, None] = {}
    for name in sorted(objects):
        obj = model.data_objects[model.unnest_root(name)]
        parts = [str(p) for p in (obj.database, obj.schema_name, obj.code) if p]
        if parts:
            tables[".".join(parts)] = None
    return tuple(tables)


def _dimension_column(
    model: SemanticModel, name: str, column: str | None = None
) -> DataObjectColumn:
    """Dimension *name* as a leg returns it, as *column* (its own name)."""
    column = column or name
    return DataObjectColumn(
        name=column, code=column, abstract_type=model.dimensions[name].result_type
    )


def _key_column(model: SemanticModel, name: str, obj: str, column: str) -> DataObjectColumn:
    col = model.data_objects[obj].columns[column]
    return DataObjectColumn(name=name, code=name, abstract_type=col.abstract_type)


def _value_column(model: SemanticModel, name: str, obj: str, column: str) -> DataObjectColumn:
    """*column* of *obj* as the fact leg returns it, as *name*: computed there, typed as declared.

    Its declared width stays, which the combine step's exact averages and
    statistics read (:func:`~orionbelt.compiler.type_resolver.exact_input_scale`).
    """
    col = model.data_objects[obj].columns[column]
    return col.model_copy(
        update={"name": name, "code": name, "expression": None, "primary_key": False}
    )


def _combine_dimension(
    model: SemanticModel, name: str, leg: str, column: str | None = None
) -> Dimension:
    """*name* as the combine step reads it, from *column* of *leg* (its own name)."""
    dimension = model.dimensions[name]
    return Dimension(
        name=column or name,
        view=leg,
        column=column or name,
        result_type=dimension.result_type,
        format=dimension.format,
    )


def _rekeyed(
    dialect: Dialect,
    columns: list[DataObjectColumn],
    candidates: dict[str, bool],
    grouped: set[str],
    kept: dict[str, str],
) -> tuple[list[DataObjectColumn], dict[str, tuple[str, bool]], dict[str, str]]:
    """The leg's columns once its engine's equality is applied to them.

    *candidates* maps the leg columns the combine step groups by or counts to
    whether they are counted; a dimension counts only when *grouped* (selected
    without a grain). Returns the columns with the type each is then read
    as, the rewritten ones with their type before and whether counted, and
    the hidden columns (from *kept*) keeping a rewritten dimension's value
    for a combine filter, each with the column it copies.
    """
    out: list[DataObjectColumn] = []
    rekeyed: dict[str, tuple[str, bool]] = {}
    hidden: dict[str, str] = {}
    copies: list[DataObjectColumn] = []
    for column in columns:
        counted = candidates.get(column.name)
        if counted is None or (not counted and column.name not in grouped):
            out.append(column)
            continue
        kind = column.abstract_type.value
        key = dialect.equality_key(ColumnRef(name=column.name), kind, counted=counted)
        if key is None:
            out.append(column)
            continue
        rekeyed[column.name] = (kind, counted)
        out.append(column.model_copy(update={"abstract_type": DataType(key[1])}))
        if column.name in kept:
            name = kept[column.name]
            hidden[name] = column.name
            copies.append(column.model_copy(update={"name": name, "code": name}))
    return [*out, *copies], rekeyed, hidden


def _rewrite_leg(
    dialect: Dialect,
    sql: str,
    columns: list[DataObjectColumn],
    rekeyed: dict[str, tuple[str, bool]],
    kept: dict[str, str],
) -> str:
    """*sql* wrapped so its *rekeyed* columns compare in DuckDB as in their engine."""
    projection: list[tuple[str, Expr]] = []
    for column in columns:
        source = kept.get(column.name, column.name)
        ref = ColumnRef(name=source, table=_LEG_ALIAS)
        if column.name in rekeyed:
            kind, counted = rekeyed[column.name]
            key = dialect.equality_key(ref, kind, counted=counted)
            assert key is not None
            projection.append((column.name, key[0]))
        else:
            projection.append((column.name, ref))
    return dialect.wrap_leg(sql, projection, _LEG_ALIAS)


def _renamed(item: QueryFilterItem, names: dict[str, str]) -> QueryFilterItem:
    """*item* reading each field in *names* from the column named for it."""
    if isinstance(item, QueryFilterGroup):
        return item.model_copy(update={"filters": [_renamed(f, names) for f in item.filters]})
    return item.model_copy(update={"field": names.get(item.field, item.field)})


def _partial_measure(measure: Measure) -> Measure:
    """The measure as a leg computes it: a partial result to combine later.

    The output type and the default are the combined measure's to apply once:
    no ``dataType``, no ``defaultValue`` (an empty group's default would become
    an input to the combined aggregate), no ``total``. A sum is also left
    uncast while the fact leg compiles (``partial_sums``).
    """
    return measure.model_copy(update={"total": False, "default_value": None, "data_type": None})


def _leg_measures(
    model: SemanticModel, names: set[str], taken: set[str]
) -> tuple[
    dict[str, Measure],
    dict[str, tuple[str, str, int | None]],
    dict[str, tuple[str, str, str]],
    dict[str, tuple[str, str, str, int]],
]:
    """The fact leg's partial measures for *names*, and how the others split.

    A measure is computed as itself (:func:`_partial_measure`), except:

    * an ``avg``: an average of per-group averages is not the average, so the
      leg returns its argument's ``SUM`` and ``COUNT`` instead, under the same
      filters, and the combine step divides their totals
      (:func:`~orionbelt.compiler.type_resolver.combined_averages`). Both skip
      NULLs, so they cover exactly the rows ``AVG`` would.
    * a count of distinct values: counts of distinct values do not add up, so
      the leg groups by the column it counts as well, and the combine step
      counts that column's distinct values across all the groups they reach.
      The measure stays in the leg only to keep the rows it keeps; the other
      measures, of the same data object (:meth:`_Planner.counts_beside_others`),
      are still exact at that finer grain, since each combines from partial
      results.
    * a ``variance``, ``var_pop``, ``stddev`` or ``stddev_pop``: per-group
      statistics do not combine, their moments do. The leg returns its
      argument's ``COUNT``, ``SUM`` and sum of squares, each value squared
      exactly (:func:`~orionbelt.compiler.type_resolver.partial_squares`), and
      the combine step computes the statistic from their totals
      (:meth:`Dialect.combined_variance`). The values are integers or declared
      decimals (:meth:`_Planner.check_variance`), so every sum is exact.

    Returns the leg's measures by name; per ``avg`` the leg columns of its sums
    and its counts, with the scale the sums are read at (0 for an integer
    measure, :data:`_AVERAGE_SCALE` for a decimal one, ``None`` for a
    ``float`` one, whose sums are doubles and divide in floating point); and
    per count of distinct values the leg column of its values, with the data
    object and column they are read from; per variance the leg columns of its
    counts, sums and squares, with the places of its values. Each generated
    column takes a name none of *taken* (casefolded) is, and is added to it: a
    declared measure named like a partial would replace it in the leg.
    """
    effective = model.effective_measures
    measures: dict[str, Measure] = {}
    averages: dict[str, tuple[str, str, int | None]] = {}
    distinct: dict[str, tuple[str, str, str]] = {}
    variances: dict[str, tuple[str, str, str, int]] = {}
    for name in sorted(names):
        measure = _partial_measure(effective[name])
        if _counts_distinct(measure):
            ref = measure.columns[0]
            column = _unused(f"__distinct{len(distinct)}", taken)
            distinct[name] = (column, ref.view or "", ref.column or "")
            # Kept, though the combine step counts the column instead: the
            # measure is what makes the planner keep the rows a count over
            # its object keeps, those without a match on the other side.
            measures[name] = measure
            continue
        if measure.aggregation in _VARIANCES:
            places = exact_input_scale(measure, model)
            assert places is not None  # _Planner.check_variance
            prefix = f"__var{len(variances)}"
            counts = _unused(f"{prefix}_count", taken)
            sums = _unused(f"{prefix}_sum", taken)
            squares = _unused(f"{prefix}_squares", taken)
            # Typed as their values, so neither is summed as a double
            # (``apply_float_input``); the squares as a decimal, so no
            # integer widening wraps them in a narrower type.
            moments = {
                counts: (AggregationType.COUNT, DataType.INT),
                sums: (AggregationType.SUM, DataType.INT if places == 0 else DataType.DECIMAL),
                squares: (AggregationType.SUM, DataType.DECIMAL),
            }
            for column, (aggregation, result_type) in moments.items():
                measures[column] = measure.model_copy(
                    update={"name": column, "aggregation": aggregation, "result_type": result_type}
                )
            variances[name] = (counts, sums, squares, places)
            continue
        if measure.aggregation != AggregationType.AVG:
            measures[name] = measure
            continue
        sums = _unused(f"__avg{len(averages)}_sum", taken)
        counts = _unused(f"__avg{len(averages)}_count", taken)
        measures[sums] = measure.model_copy(
            update={"name": sums, "aggregation": AggregationType.SUM}
        )
        measures[counts] = measure.model_copy(
            update={
                "name": counts,
                "aggregation": AggregationType.COUNT,
                "result_type": DataType.INT,
            }
        )
        scale: int | None = _AVERAGE_SCALE
        if measure.result_type == DataType.INT:
            scale = 0
        elif measure.result_type == DataType.FLOAT:
            scale = None
        averages[name] = (sums, counts, scale)
    return measures, averages, distinct, variances


def _counts_distinct(measure: Measure) -> bool:
    """Whether *measure* counts distinct values: ``count_distinct``, or a distinct ``count``."""
    return measure.aggregation == AggregationType.COUNT_DISTINCT or (
        measure.aggregation == AggregationType.COUNT and measure.distinct
    )


def _from_rows(reason: str, name: str) -> str:
    """How a refusal says *name* is computed from rows, which *reason* needs."""
    if name == reason:
        return f"Measure '{name}' is computed from the fact leg's rows"
    return f"Measure '{reason}' is computed from the fact leg's rows, and so is '{name}'"


def _apart(reason: str, name: str) -> str:
    return "Query it on one data source." if name == reason else f"Query '{name}' apart."


def _counts_distinct_column(measure: Measure) -> bool:
    """Whether *measure* counts the distinct values of one column, unfiltered."""
    return (
        _counts_distinct(measure)
        and not measure.expression
        and not measure.filters
        and len(measure.columns) == 1
        and bool(measure.columns[0].column)
    )


def _row_measures(
    model: SemanticModel,
    names: set[str],
    taken: set[str],
    base: str,
    identities: dict[str, list[str]],
) -> tuple[
    dict[str, Measure], str, dict[str, tuple[str, str, str]], dict[tuple[str, str, bool], str]
]:
    """The fact leg's measure when it returns rows, and what else it groups by.

    The leg returns the rows of the query's base object grouped by all the
    combine step reads of them: the dimensions and keys, and each column a
    measure reads, as its argument, in a filter, as its ordering or in its
    expression, under a name of its own. Its one measure counts each group's
    rows, a ``COUNT(*)`` of the base object, whose rows the joins of its
    island do not repeat. The combine step repeats each row that many times
    (:func:`_with_rows`) and computes each measure from the copies, as one
    database computes it from its rows (:func:`_row_measure`). A count of
    the distinct values of one column counts a column of its own, as its
    engine counts them, as in :func:`_leg_measures`. The leg returns the
    *identities* of the objects measures read through the base's rows
    (:meth:`_Planner.parents`) as well, and counts each one's rows: the
    combine step reads those measures from each object's rows once each,
    and the count makes the leg return the rows of the object no base row
    reaches, as one database adds them, with no base rows to stand for.

    Returns the leg's measures by name; the name of its count; per count of
    the distinct values of one column, the leg column of its values with the
    data object and column they are read from; and the leg column of each
    ``(data object, column, in its own time)`` the other measures read
    (:func:`_read_columns`). Each generated column takes a name none of
    *taken* (casefolded) is, and is added to it.
    """
    effective = model.effective_measures
    count = _unused("__rows", taken)
    distinct: dict[str, tuple[str, str, str]] = {}
    columns: dict[tuple[str, str, bool], str] = {}
    for name in sorted(names):
        measure = effective[name]
        if _counts_distinct_column(measure):
            ref = measure.columns[0]
            column = _unused(f"__distinct{len(distinct)}", taken)
            distinct[name] = (column, ref.view or "", ref.column or "")
            continue
        for read in _read_columns(measure):
            if read not in columns:
                columns[read] = _unused(f"__value{len(columns)}", taken)
    measures = {
        count: Measure(
            name=count,
            columns=[DataColumnRef(view=base)],
            aggregation=AggregationType.COUNT,
            result_type=DataType.INT,
        )
    }
    for i, (target, identity) in enumerate(sorted(identities.items())):
        for column in identity:
            if (target, column, False) not in columns:
                columns[(target, column, False)] = _unused(f"__value{len(columns)}", taken)
        # A count of the object's rows, which the leg answers as one database
        # does (grain_dedup): with its rows no base row reaches as well, as
        # rows that stand for none of the base object's.
        once = _unused(f"__once{i}", taken)
        measures[once] = Measure(
            name=once,
            columns=[DataColumnRef(view=target)],
            aggregation=AggregationType.COUNT,
            result_type=DataType.INT,
        )
    return measures, count, distinct, columns


def _row_measure(
    model: SemanticModel,
    name: str,
    columns: dict[tuple[str, str, bool], str],
    view: str,
    elsewhere: dict[tuple[str, str, bool], str],
) -> Measure:
    """The measure as the combine step computes it from the fact leg's rows.

    The measure itself, reading each column it reads from *view*'s column of
    it (:func:`_row_measures`): the fact leg's, or, for a measure counted
    once per row of its object, that object's rows once each. A column of
    another data source it reads from the dimension leg *elsewhere* names,
    which the combine step joins to the fact rows. It keeps its type,
    default and ``total``. A count of rows counts *view*'s rows.
    """
    measure = model.effective_measures[name]

    def at(ref: DataColumnRef, own_zone: bool = False) -> DataColumnRef:
        if not ref.column:
            return DataColumnRef(view=view)
        read = (ref.view or "", ref.column, own_zone)
        return DataColumnRef(view=elsewhere.get(read, view), column=columns[read])

    update: dict[str, Any] = {
        "columns": [at(ref) for ref in measure.columns],
        "filters": [_filter_at(item, at) for item in measure.filters],
        "anchor": None,
    }
    if measure.expression:
        update["expression"] = repoint_references(
            measure.expression,
            {
                (obj, col): (elsewhere.get((obj, col, own), view), leg)
                for (obj, col, own), leg in columns.items()
                if not own
            },
        )
    if measure.within_group is not None:
        ordering = measure.within_group.column
        update["within_group"] = measure.within_group.model_copy(
            update={"column": at(ordering, _orders_in_own_zone(measure))}
        )
    return measure.model_copy(update=update)


def _read_columns(measure: Measure) -> list[tuple[str, str, bool]]:
    """Each column *measure* reads, as ``(data object, column, in its own time)``.

    Its arguments, filters, ordering and expression, all read in the query's
    time as one database reads them, but for the key a ``first`` or ``last``
    is ordered by, which it reads in the column's own (:func:`_orders_in_own_zone`).
    """
    read = [(ref.view or "", ref.column, False) for ref in measure.columns if ref.column]
    read += [(obj, column, False) for obj, column in _filter_columns(measure.filters)]
    ordering = measure.within_group.column if measure.within_group is not None else None
    if ordering is not None and ordering.column:
        read.append((ordering.view or "", ordering.column, _orders_in_own_zone(measure)))
    if measure.expression:
        read += [(obj, column, False) for obj, column in find_qualified_refs(measure.expression)]
    return read


def _orders_in_own_zone(measure: Measure) -> bool:
    """Whether *measure* is ordered by its key in the column's own time: ``first``, ``last``.

    As :meth:`~orionbelt.compiler.resolution.QueryResolver._within_group_order`
    orders them; a ``listagg`` is ordered in the query's time, as its values are.
    """
    return measure.aggregation in (AggregationType.FIRST, AggregationType.LAST)


def _compared_columns(measure: Measure) -> list[tuple[str, str, bool]]:
    """The columns whose values *measure* compares, each with whether it counts them.

    Its filters, its ordering and its expression compare what they read; its
    arguments are compared by an aggregation that orders them, or counts
    equal ones once (:data:`_ORDERING`, :data:`_COUNTING`, ``distinct``).
    Counting compares equality alone, which an engine may judge otherwise
    than order (:meth:`Dialect.equality_key`).
    """
    counts = measure.distinct or measure.aggregation in _COUNTING
    compared = [(obj, column, False) for obj, column in _filter_columns(measure.filters)]
    ordering = measure.within_group.column if measure.within_group is not None else None
    if ordering is not None and ordering.column:
        compared.append((ordering.view or "", ordering.column, False))
    if measure.expression:
        compared += [(obj, col, counts) for obj, col in find_qualified_refs(measure.expression)]
    if counts or measure.aggregation in _ORDERING:
        compared += [(ref.view or "", ref.column, counts) for ref in measure.columns if ref.column]
    return compared


def _filter_columns(items: Iterable[MeasureFilterItem]) -> list[tuple[str, str]]:
    """Each ``(data object, column)`` measure filters read."""
    columns: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, MeasureFilterGroup):
            columns += _filter_columns(item.filters)
        elif item.column is not None and item.column.column:
            columns.append((item.column.view or "", item.column.column))
    return columns


def _filter_at(
    item: MeasureFilterItem, at: Callable[[DataColumnRef], DataColumnRef]
) -> MeasureFilterItem:
    """*item* reading each column where *at* puts it."""
    if isinstance(item, MeasureFilterGroup):
        return item.model_copy(update={"filters": [_filter_at(f, at) for f in item.filters]})
    if item.column is None:
        return item
    return item.model_copy(update={"column": at(item.column)})


def _with_rows(
    sql: str,
    dialect: Dialect,
    leg: str,
    count: str,
    once: dict[str, tuple[list[str], list[str]]],
) -> str:
    """*sql* reading :data:`_ROWS_RELATION`: *leg* with each row repeated *count* times.

    And each relation of *once*: the distinct rows of the columns of *leg* it
    names where its identity columns are present, an object's rows once each
    (a base row without a match on the object is none of them). Common table
    expressions ahead of any the combine query has, so the query stays one
    statement over the legs' results as they are returned. The count is
    cast, an engine whose integers are decimals (Snowflake's ``NUMBER``) may
    return it as one; a row standing for no base row counts none.
    """
    q = dialect.quote_identifier
    alias = q(_LEG_ALIAS)
    relations = [
        f"{q(_ROWS_RELATION)} AS (\nSELECT {alias}.* EXCLUDE ({q(count)})\nFROM {q(leg)} AS "
        f"{alias}, range(CAST(COALESCE({alias}.{q(count)}, 0) AS BIGINT))\n)",
        *(
            f"{q(name)} AS (\nSELECT DISTINCT {', '.join(map(q, columns))}\nFROM {q(leg)}\n"
            f"WHERE {' AND '.join(f'{q(c)} IS NOT NULL' for c in identity)}\n)"
            for name, (columns, identity) in once.items()
        ),
    ]
    with_ = ",\n".join(relations)
    if sql.startswith("WITH "):
        return f"WITH {with_},\n{sql.removeprefix('WITH ')}"
    return f"WITH {with_}\n{sql}"


def _unused(name: str, taken: set[str]) -> str:
    """*name*, prefixed with underscores until it is none of *taken*, which are casefolded."""
    while name.casefold() in taken:
        name = f"_{name}"
    taken.add(name.casefold())
    return name


def _combine_measure(
    model: SemanticModel, name: str, column: str | None = None, view: str = "leg_0"
) -> Measure:
    """The measure re-aggregated from the fact leg's partial results.

    It carries the measure's type, default and ``total``. A count is combined
    as a ``SUM`` of counts, which over no rows is NULL where a count is 0: it
    falls back to 0, whatever default it declares, since a SQL ``COUNT`` never
    applies one. An ``avg`` is the ``SUM`` of its leg *column* of sums, which
    the combine compile divides by its counts; a variance keeps its
    aggregation over its leg *column* of sums, which the combine compile
    reads with its counts and squares; and a count of distinct values counts
    its leg *column* of values (:func:`_leg_measures`), from *view* where the
    fact leg returns rows (:func:`_row_measures`).
    """
    measure = model.effective_measures[name]
    counts_distinct = _counts_distinct(measure)
    default = measure.default_value
    if measure.aggregation in (AggregationType.COUNT, AggregationType.COUNT_DISTINCT):
        default = 0
    if counts_distinct:
        aggregation = AggregationType.COUNT_DISTINCT
    elif measure.aggregation in _VARIANCES:
        aggregation = measure.aggregation
    elif column is not None:
        aggregation = AggregationType.SUM
    else:
        aggregation = _REAGGREGATE[measure.aggregation]
    averages = measure.aggregation == AggregationType.AVG
    # The type the one database's query casts to, widened for wide sources. An
    # average is typed for the dialect that divides it: an integer one takes
    # the widened type every engine's exact integer AVG casts to (#316), where
    # typed without a dialect it would take the narrow default, too small for
    # an average of 64-bit integers.
    dialect = DialectRegistry.get(COMBINE_DIALECT) if averages else None
    resolved = resolve_measure_cast_type(measure, model.settings, dialect, model)
    return Measure(
        name=name,
        columns=[DataColumnRef(view=view, column=column or name)],
        aggregation=aggregation,
        result_type=measure.result_type,
        data_type=measure.data_type
        or (resolved.render() if isinstance(resolved, DecimalType) else None),
        total=measure.total,
        default_value=default,
        format=measure.format,
    )


def _filter_fields(item: QueryFilterItem) -> list[str]:
    if isinstance(item, QueryFilterGroup):
        return [f for sub in item.filters for f in _filter_fields(sub)]
    return [item.field]


def _on_null(item: QueryFilterItem) -> bool | None:
    """What *item* is where every field it reads is NULL: true, false or unknown (``None``).

    A group without conditions compiles to none, so it holds.
    """
    if isinstance(item, QueryFilter):
        if item.op in _TRUE_FOR_NULL:
            return True
        return False if item.op in _FALSE_FOR_NULL else None
    values = [_on_null(sub) for sub in item.filters if _filter_fields(sub)]
    if not values:
        return True
    decisive = item.logic == FilterLogic.OR
    value = decisive if decisive in values else None if None in values else not decisive
    return not value if item.negated and value is not None else value


def _exists_filters(items: Iterable[QueryFilterItem]) -> Iterator[QueryFilter]:
    """Each ``exists`` / ``nonexists`` filter of *items*, those in groups too."""
    for item in items:
        if isinstance(item, QueryFilterGroup):
            yield from _exists_filters(item.filters)
        elif item.subquery is not None:
            yield item


def _exists_subject(item: QueryFilter, model: SemanticModel) -> str | None:
    """The data object an ``exists`` filter correlates to: its dimension's, or its ``Object``'s."""
    dimension = model.dimensions.get(DimensionRef.parse(item.field).name)
    if dimension is not None:
        return dimension.view
    obj, dot, _ = item.field.partition(".")
    return obj.strip() if dot else None


def _exists_objects(item: QueryFilter, model: SemanticModel) -> set[str]:
    """The data objects an ``exists`` filter's subquery reads.

    Its target, the objects on the join path from the subject to it and the
    parents of the nested ones, and those its filters and the enforced static
    filters read, with the objects joined to reach them, inner ``exists``
    included, as
    :func:`~orionbelt.compiler.filters.build_exists_filter_expr` joins them;
    the subject is the outer query's.
    """
    if item.subquery is None:
        return set()
    return _subquery_reads(item.subquery, _exists_subject(item, model), model)


def _subquery_reads(sub: Subquery, subject: str | None, model: SemanticModel) -> set[str]:
    graph = JoinGraph.of(model)
    body = {sub.data_object}
    if subject is not None and sub.path_name is None:
        body |= _step_objects(graph.find_join_path_undirected(subject, sub.data_object))
    # A nested object's rows are its parent's array: the body reads the
    # parent's table and unnests them.
    nested = (model.data_objects[name].nested_in for name in body if name in model.data_objects)
    body |= {parent.data_object for parent in nested if parent is not None}
    if subject is not None:
        body.discard(subject)
    inner: set[str] = set()

    def join(objects: Iterable[str]) -> None:
        # Each reached from what the body joins already, in the order it joins them.
        for obj in objects:
            if obj not in body:
                body.update({obj}, _step_objects(graph.find_join_path(set(body), {obj})))

    for item in sub.filter:
        ref = _resolve_subquery_filter_field(item.field, model, sub.data_object, [])
        if ref is None:
            continue
        obj, column = ref
        if item.subquery is not None:
            join([obj])
            inner |= _subquery_reads(item.subquery, obj, model)
        else:
            join([obj, *sorted(model.column_reference_objects(obj, column))])
    for mf in model.filters:
        needed = [
            mf.data_object,
            *sorted(model.column_reference_objects(mf.data_object, mf.column)),
        ]
        if mf.enforce == FilterEnforcement.ALWAYS and not set(needed) <= {subject}:
            join(needed)
    return body | inner


def _step_objects(steps: Iterable[JoinStep]) -> set[str]:
    return {o for step in steps for o in (step.from_object, step.to_object)}
