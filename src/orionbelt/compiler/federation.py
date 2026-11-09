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
* **Key maps.** Where the fact leg groups by more than a key, it can join a
  small dimension leg's keys as a map to the ordinals of that leg's rows and
  group by those instead (:class:`KeyMap`), within the same limit.

Only what splits exactly is planned. Everything else is refused with a named
``FEDERATION_*`` error rather than answered with a different number; see
``design/PLAN_federation.md`` §5.5 for which shapes come later.

This module plans and compiles; ``service/federated_executor.py`` runs the
plan.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from orionbelt.ast.nodes import (
    CaseExpr,
    ColumnRef,
    Expr,
    JoinType,
    Literal,
    OrderByItem,
    UnaryOp,
    WindowFunction,
)
from orionbelt.compiler.cfl import CFLPlanner
from orionbelt.compiler.cfl_projection import (
    collect_correlated_tables,
    collect_table_refs,
    group_measures_by_object,
)
from orionbelt.compiler.composability import metric_leaf_measures, metric_measure_names
from orionbelt.compiler.count_zero import read_as_counts
from orionbelt.compiler.expr_rewrite import rewrite_column_refs
from orionbelt.compiler.fill_wrap import fill_bounds
from orionbelt.compiler.filter_resolution import (
    report_unapplied_query_filter,
    report_unapplied_static_filter,
)
from orionbelt.compiler.filter_wrap import kept_by_context, query_item_fields
from orionbelt.compiler.filters import (
    _resolve_subquery_filter_field,
    build_filter_expr,
    build_measure_filter_condition,
)
from orionbelt.compiler.grain_dedup import (
    MULTIPLICITY_SAFE_AGGREGATIONS,
    dedup_identity,
    detect_dedup_measures,
)
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.compiler.metric_resolution import reaggregate_per_ref
from orionbelt.compiler.null_rejection import is_null_without
from orionbelt.compiler.passes import refuse_grain_under_grouping
from orionbelt.compiler.raw_resolution import raw_field_aliases
from orionbelt.compiler.resolution import (
    LegZone,
    QueryResolver,
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    columns_in_own_zone,
    dimension_reference,
    make_column_expr,
    make_period_expr,
    static_filters_after_join,
    tables_as_key_maps,
    timestamps_in_zone,
)
from orionbelt.compiler.star import grouping_flag_alias
from orionbelt.compiler.type_resolver import (
    combined_averages,
    combined_variances,
    exact_input_scale,
    partial_squares,
    partial_sums,
    resolve_measure_cast_type,
    resolve_metric_data_type,
)
from orionbelt.dialect.base import Dialect
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.errors import SemanticError
from orionbelt.models.expressions import find_qualified_refs, repoint_references
from orionbelt.models.query import (
    CoalesceDimension,
    DimensionRef,
    FilterOperator,
    Grouping,
    NullsPosition,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
    Subquery,
    UsePathName,
)
from orionbelt.models.semantic import (
    DATE_BEARING_TYPES,
    AggregationType,
    Cardinality,
    DataColumnRef,
    DataObject,
    DataObjectColumn,
    DataObjectJoin,
    DataType,
    Dimension,
    FilterContext,
    FilterContextFilter,
    FilterEnforcement,
    FilterLogic,
    FilterValue,
    Measure,
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    MetricType,
    ModelFilter,
    ModelSettings,
    ReaggregateAggType,
    ReaggregatePopulation,
    SemanticModel,
    TimeGrain,
    WithinGroup,
)
from orionbelt.models.synthesis import synthesize_count_measure
from orionbelt.models.types import DecimalType, SimpleType
from orionbelt.models.warnings import WarningCode, warning

COMBINE_DIALECT = "duckdb"

# The alias a leg's own query is read under when the leg rewrites some of its
# columns (:meth:`Dialect.equality_key`).
_LEG_ALIAS = "__leg"

# The columns of a key map a fact leg joins (:class:`KeyMap`): a key, and its
# ordinal among the dimension leg's rows.
_MAP_KEY = "k"
_MAP_ORDINAL = "n"

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
# The measure filter operators that match a pattern, which can tell apart
# values a comparison calls equal (:meth:`_Planner.filters_in_leg`).
_PATTERN_OPERATORS = frozenset(
    {
        "contains",
        "notcontains",
        "starts_with",
        "ends_with",
        "like",
        "notlike",
        "ilike",
        "notilike",
    }
)
# The filter operators that compare no value, which no engine compares
# otherwise than DuckDB (:meth:`_Planner.compared_in_leg`).
_VALUELESS_OPERATORS = frozenset(
    {
        FilterOperator.SET,
        FilterOperator.NOT_SET,
        FilterOperator.IS_NULL,
        FilterOperator.IS_NOT_NULL,
        FilterOperator.RELATIVE,
        FilterOperator.EXISTS,
        FilterOperator.NONEXISTS,
    }
)
# The filter operators that hold for NULL, or fail for it, where any other
# with a value is unknown: the null test the combine step tests their
# outcome with, which a row the join leaves unmatched passes, or fails
# (:func:`_outcome`).
_NULL_TESTS = {
    FilterOperator.BLANK: FilterOperator.IS_NULL,
    FilterOperator.NOT_BLANK: FilterOperator.IS_NOT_NULL,
}
# The filter operators that hold for every value of a group its engine calls
# equal, or for none, given values of the column's own type: comparisons.
_COMPARISON_OPERATORS = frozenset(
    {
        FilterOperator.EQUALS,
        FilterOperator.NOT_EQUALS,
        FilterOperator.EQ,
        FilterOperator.NEQ,
        FilterOperator.GT,
        FilterOperator.GTE,
        FilterOperator.LT,
        FilterOperator.LTE,
        FilterOperator.GREATER,
        FilterOperator.GREATER_EQ,
        FilterOperator.LESS,
        FilterOperator.LESS_EQ,
        FilterOperator.IN_LIST,
        FilterOperator.NOT_IN_LIST,
        FilterOperator.IN,
        FilterOperator.NOT_IN,
        FilterOperator.BETWEEN,
        FilterOperator.NOT_BETWEEN,
    }
)

# The relation the combine step reads a fact leg returning rows as: the leg,
# each row repeated as often as it counts (:func:`_with_rows`); and the prefix
# of each relation of an object's rows once each, the parent of measures that
# read it through the fact rows (:meth:`_Planner.parents`).
_ROWS_RELATION = "leg_0_rows"
_ONCE_RELATION = "leg_0_once"
# The suffix of the relation of a dimension leg's rows the fact leg's reach,
# whose object's measures the combine step computes (:func:`_reached_rows`).
_REACHED_RELATION = "_reached"
# The result of each part of a query split by fact, and their union; and the
# count of its fact's rows a part grouping by no dimension returns (:class:`_Part`).
_PART_RELATION = "__part"
_PARTS_RELATION = "__parts"
# The parts' rows but those of the parts of values, which give its groups values.
_HELD_RELATION = "__parts_held"
# The groups of the parts' union, under a rollup or cube (:func:`_grouped`),
# and the grouping sets each part's rows join (:func:`_in_grouping_sets`).
_GROUPED_RELATION = "__grouped"
_GROUPING_SETS_RELATION = "__grouping_sets"
# The combine query's rows, sorted by the ranks its legs give its strings, and
# each column's ranks (:func:`_sorted_by_ranks`).
_SORTED_RELATION = "__sorted"
_RANKS_RELATION = "__ranks"
_PRESENT = "__present"

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
        joins: The joins between data sources whose keys it returns, as an
            error names them (:meth:`_Planner.join_label`): it returns a row
            per key of each, beside the query's dimensions, however few groups
            the result has.
        raw: Whether it returns its data source's rows as a query of fields
            (``select.fields``) reads them, rather than groups.
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
    joins: tuple[str, ...] = ()
    raw: bool = False


@dataclass(frozen=True)
class KeyInjection:
    """A dimension leg whose keys may filter the fact leg.

    The leg runs a filter of the query, and the combine step keeps only the
    fact rows it matched: the fact leg may read only the keys it returned.

    Attributes:
        leg: The dimension leg (``leg_1``).
        column: The key column, of the same name in that leg and the fact leg.
        abstract_type: The key's OBML type, ``int`` or ``string``.
        values: The dimension leg's column of the key's values, where the
            key column holds the key as its engine matches it
            (:meth:`Dialect.match_key`); else the key column holds them.
    """

    leg: str
    column: str
    abstract_type: str
    values: str | None = None


@dataclass(frozen=True)
class KeyMap:
    """A dimension leg the fact leg may join as a map of its keys.

    Each key maps to the ordinal of its row among the leg's rows told apart by
    everything but the key: the fact leg joins the map, as rows of literals,
    and groups by the ordinal where it grouped by the key, returning it in the
    key's column. The combine step reads one row per ordinal for the leg, the
    ordinal in its key column, and joins the two as before. An integer key the
    combine step reads for that join alone, of a fact leg grouped by more than
    it (:func:`_maps_key`).

    Attributes:
        leg: The dimension leg (``leg_1``).
        column: The key column, of the same name in that leg and the fact leg.
    """

    leg: str
    column: str


@dataclass(frozen=True)
class MapJoin:
    """How the fact leg joins a key map (:class:`KeyMap`).

    Attributes:
        name: The data object standing for the map in the leg's model.
        data_object: The data object whose column holds the key.
        column: That column.
        inner: Whether the leg reads only the keys the map holds: its
            dimension leg runs a filter of the query, and the combine step
            keeps only the fact rows it matched.
    """

    name: str
    data_object: str
    column: str
    inner: bool


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
        body: The objects of other islands its ``exists`` subqueries read, whose
            static filters it leaves out (:func:`static_filters_after_join`).
        rekeyed: Its columns compared as their engine compares them.
        kept: The copies of rekeyed columns a combine filter reads.
        predicates: Its columns of measure filters it evaluates (:func:`_leg_predicates`).
        compile_leg: As :func:`plan_federated` takes it.
        zone: The zone it reads its timestamps without a zone in
            (:func:`timestamps_in_zone`).
        map_joins: How it joins the map of each key column a map may stand for.
    """

    leg: FederatedLeg
    query: QueryObject
    model: SemanticModel
    columns: tuple[DataObjectColumn, ...]
    squares: dict[str, int]
    own_zone: frozenset[tuple[str, str]]
    body: frozenset[str]
    rekeyed: dict[str, tuple[str, bool]]
    kept: dict[str, str]
    predicates: dict[str, Expr]
    compile_leg: Callable[[QueryObject, SemanticModel, str], str] = dataclasses.field(
        compare=False, repr=False
    )
    zone: LegZone | None = dataclasses.field(default=None, compare=False, repr=False)
    map_joins: dict[str, MapJoin] = dataclasses.field(default_factory=dict)

    def __call__(
        self,
        keys: dict[str, list[Any]],
        maps: Mapping[str, Sequence[tuple[int, int]]] | None = None,
    ) -> FederatedLeg:
        """The fact leg reading only *keys*; an empty list for a column reads no rows.

        It joins each of *maps*, a key column's ``(key, ordinal)`` pairs, and
        returns the ordinal in that column (:class:`KeyMap`). A column has keys
        or a map, not both: its list would test the ordinal.
        """
        maps = maps or {}
        assert not keys.keys() & maps.keys()
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
            static_filters_after_join(self.body),
            timestamps_in_zone(self.zone),
            tables_as_key_maps(
                {
                    self.map_joins[column].name: (_MAP_KEY, _MAP_ORDINAL, tuple(pairs))
                    for column, pairs in maps.items()
                }
            ),
        ):
            sql = self.compile_leg(query, self._mapped(maps), dialect.name)
        if self.rekeyed or self.predicates:
            sql = _rewrite_leg(
                dialect, sql, list(self.columns), self.rekeyed, self.kept, self.predicates
            )
        return dataclasses.replace(self.leg, sql=sql)

    def _mapped(self, maps: Collection[str]) -> SemanticModel:
        """The leg's model with a data object for each of *maps*, joined from its
        key's object, and the key column's dimension reading the ordinal there."""
        if not maps:
            return self.model
        objects = dict(self.model.data_objects)
        dimensions = dict(self.model.dimensions)
        integer = DataType.INT
        for column in maps:
            join = self.map_joins[column]
            holder = objects[join.data_object]
            # On the leg's data source, which the leg reads alone.
            objects[join.name] = DataObject(
                name=join.name,
                code=join.name,
                database="",
                schema_name="",
                data_source=holder.data_source,
                columns={
                    name: DataObjectColumn(name=name, code=name, abstract_type=integer)
                    for name in (_MAP_KEY, _MAP_ORDINAL)
                },
                countable=False,
            )
            objects[join.data_object] = holder.model_copy(
                update={
                    "joins": [
                        *holder.joins,
                        DataObjectJoin(
                            join_type=Cardinality.MANY_TO_ONE,
                            join_to=join.name,
                            columns_from=[join.column],
                            columns_to=[_MAP_KEY],
                            required=join.inner,
                        ),
                    ]
                }
            )
            dimensions[column] = Dimension(
                name=column, view=join.name, column=_MAP_ORDINAL, result_type=integer
            )
        return self.model.model_copy(update={"data_objects": objects, "dimensions": dimensions})


@dataclass(frozen=True)
class FederatedPlan:
    """The legs of a federated query and the DuckDB query combining them.

    Attributes:
        injections: The dimension legs whose keys may filter the fact leg
            (``legs[0]``), which then runs after them.
        key_maps: The dimension legs the fact leg may join as maps of their
            keys, which it then runs after too.
        inject: The fact leg reading only the keys given for each injection's
            column, and joining the maps given for key maps' columns; an
            empty list for any of the keys reads no rows.
        timezone: The zone the legs return timestamps in, which the result is
            labeled with: the model's ``queryTimezone``, else the zone they
            convert those without a zone to; ``None`` where the model says
            its timestamps are in its ``defaultTimezone``.
    """

    legs: tuple[FederatedLeg, ...]
    combine_sql: str
    combine_dialect: str = COMBINE_DIALECT
    injections: tuple[KeyInjection, ...] = ()
    key_maps: tuple[KeyMap, ...] = ()
    timezone: str | None = None
    # In the repr, which the compilation cache sizes an entry by.
    inject: KeyInjector | None = dataclasses.field(default=None, compare=False)


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
    objects = _outer_objects(resolved, model)
    for item in _exists_filters([*query.where, *query.having]):
        objects |= _exists_objects(item, model)
    objects.discard("")
    return objects


def _outer_objects(resolved: ResolvedQuery, model: SemanticModel) -> set[str]:
    """The data objects a resolved query reads outside its ``exists`` subqueries.

    A metric across periods reads its time dimension's data object too,
    whether or not the query joins it (:meth:`_Planner.check_period_dates`),
    and the objects one database joins to reach it from the base object: a
    calendar reached through the CRM's customers reads the CRM. So does a
    measure the query only orders by, which one database computes in a
    query of its own (``order_only``): the tickets' count of the CRM; and a
    reaggregate metric's first stage, a query of its own too, reads the
    objects of its ``per`` dimensions (:func:`_per_steps`).
    """
    objects = _joined_objects(resolved)
    effective = model.effective_measures
    ordered = {
        obj
        for name in resolved.order_only_measures
        if name in effective
        for obj in effective[name].source_objects
    }
    objects |= _per_objects(resolved, model) | _step_objects(_per_steps(resolved, model))
    unjoined = (set(_period_dates(resolved, model).values()) | ordered) - objects
    if unjoined:
        graph = JoinGraph.of(model)
        for obj in sorted(unjoined):
            objects |= {obj} | _step_objects(graph.find_join_path({resolved.base_object}, {obj}))
    return objects


def _reaggregates(resolved: ResolvedQuery, model: SemanticModel) -> list[str]:
    """The reaggregate metrics a resolved query reads, those other metrics read too.

    Selected, only in ``having`` or ``orderBy``, or read by another metric,
    another reaggregate one included.
    """
    pending = [m.name for m in resolved.measures]
    pending += [*resolved.metric_components, *resolved.order_only_measures]
    pending += resolved.having_only_measures
    seen: set[str] = set()
    while pending:
        name = pending.pop()
        if name not in seen and name in model.metrics:
            seen.add(name)
            pending += metric_measure_names(model, name)
    return sorted(n for n in seen if model.metrics[n].type == MetricType.REAGGREGATE)


def _per_dimensions(resolved: ResolvedQuery, model: SemanticModel) -> list[str]:
    """The dimensions the query's reaggregate metrics group their first stage by."""
    return list(
        dict.fromkeys(
            reaggregate_per_ref(model, entry).name
            for name in _reaggregates(resolved, model)
            for entry in model.metrics[name].per
        )
    )


def _per_objects(resolved: ResolvedQuery, model: SemanticModel) -> set[str]:
    """The data objects the ``per`` dimensions of the query's reaggregate metrics read."""
    return {
        obj
        for name in _per_dimensions(resolved, model)
        for obj in {model.dimensions[name].view or ""} | model.dimension_join_objects(name)
    } - {""}


def _per_steps(resolved: ResolvedQuery, model: SemanticModel) -> list[JoinStep]:
    """The joins a reaggregate metric's first stage makes that the query does not.

    One database groups the first stage by the query's dimensions and the
    metric's ``per`` dimensions, a query of its own that joins their objects
    from the base object: the customers, for revenue per customer by order
    month. An object no join reaches from the base (on the many side of
    one) has no path, and the planner refuses the query.
    """
    return _steps_to(resolved, model, _per_objects(resolved, model))


def _steps_to(resolved: ResolvedQuery, model: SemanticModel, objects: set[str]) -> list[JoinStep]:
    """The joins from the base object to *objects* that the query does not make."""
    unjoined = objects - _joined_objects(resolved)
    graph = JoinGraph.of(model, use_path_names=resolved.use_path_names or None)
    steps: list[JoinStep] = []
    for obj in sorted(unjoined):
        path = graph.find_join_path({resolved.base_object}, {obj})
        steps += [s for s in path if s not in steps and s not in resolved.join_steps]
    return steps


def _joined_objects(resolved: ResolvedQuery) -> set[str]:
    """The data objects the resolved query itself reads and joins."""
    objects = set(resolved.required_objects) | {resolved.base_object}
    for step in resolved.join_steps:
        objects |= {step.from_object, step.to_object}
    objects |= {d.object_name for d in resolved.dimensions}
    objects |= set(resolved.measure_source_objects)
    objects.discard("")
    return objects


def _period_dates(resolved: ResolvedQuery, model: SemanticModel) -> dict[str, str]:
    """Each metric across periods by name, with the data object of its time dimension."""
    dates: dict[str, str] = {}
    for m in resolved.measures:
        if m.is_cumulative or m.is_pop or m.is_window:
            name = m.cumulative_time_dimension or m.pop_time_dimension or m.window_time_dimension
            if name in model.dimensions:
                dates[m.name] = model.dimensions[name].view
    return dates


def is_federated(resolved: ResolvedQuery, query: QueryObject, model: SemanticModel) -> bool:
    """Whether a query reads data objects of more than one data source."""
    sources = object_sources(model)
    read = query_objects(resolved, query, model)
    return len({sources.get(o) for o in read if o in sources}) > 1


def columns_as_dimensions(query: QueryObject, model: SemanticModel) -> SemanticModel | None:
    """*model* with a dimension for each column a filter names, or ``None`` for none.

    A ``where`` filter, or a measure's ``filterContext`` include, may name a
    column of a data object (``Customers.Region``) where it names no
    dimension. One database compares the column as it compares a dimension
    over it, the same expression in the same time zone. The federation
    planner places a filter by its dimension, so each such column becomes
    one, named as the filter names it: the filter, a filter context keeping
    or leaving it out by that name, an ``exists`` on the column and the
    errors read it unchanged. Not where a dimension, measure, metric or
    column has that name (a measure may be named ``Orders.Amount``), nor
    another such column without its case: then no column becomes one, and
    the query is refused as it was.
    """

    def fields(item: QueryFilterItem) -> list[str]:
        if isinstance(item, QueryFilterGroup):
            return [field for sub in item.filters for field in fields(sub)]
        return [] if item.subquery is not None else [item.field]

    named = [field for item in query.where for field in fields(item)]
    named += [
        item.field
        for measure in model.measures.values()
        if measure.filter_context is not None
        for item in measure.filter_context.include
    ]
    taken = _reserved_names(model)
    added: dict[str, Dimension] = {}
    for field in named:
        obj, _, column = (part.strip() for part in field.partition("."))
        declared = model.data_objects.get(obj)
        if field in model.dimensions or field in added:
            continue
        if declared is None or column not in declared.columns:
            continue
        if field.casefold() in taken:
            # Its name is taken, by the model or by another such column
            # without its case (``Orders.AMOUNT``): the query is planned as
            # without any, and refused as it was.
            return None
        taken.add(field.casefold())
        added[field] = Dimension(
            name=field,
            view=obj,
            column=column,
            result_type=declared.columns[column].abstract_type,
        )
    if not added:
        return None
    return model.model_copy(update={"dimensions": {**model.dimensions, **added}})


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
    plan_part: Callable[[QueryObject, SemanticModel], FederatedPlan] | None = None,
    timezone: str | None = None,
    source_timezone: Callable[[str | None, str], str | None] | None = None,
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
        plan_part: Plans one fact's part of a query whose facts one database
            unions (:meth:`_Planner.parts`), as a query of its own: resolved
            against the model given, *model* or a copy with a measure more,
            and planned as this function plans one.
        timezone: The zone every leg reads its timestamps without a zone in
            where the model sets no ``queryTimezone``: its ``defaultTimezone``,
            else the server's.
        source_timezone: The zone a data source (the dialect's own for
            ``None``) runs its sessions in, or ``None``; asked for a leg that
            reads a timestamp without a zone. Without it, or with a model
            stating the zone they were written in (``overrideDatabaseTimezone``,
            or ``queryTimezone`` with ``defaultTimezone``), the legs read them
            as the model alone says.

    Raises:
        ResolutionError: A ``FEDERATION_*`` error for a query that does not
            split exactly.
    """
    planner = _Planner(
        query, model, resolved, dialect_name, source_dialect, timezone, source_timezone
    )
    leg_limit = leg_row_limit + 1 if leg_row_limit is not None else None
    planner.check_query_shape()
    planner.check_period_dates()
    islands = planner.islands()
    planner.check_computed_columns(islands)
    body_reads = planner.body_reads(islands)
    parts = planner.parts(islands) if plan_part is not None else None
    if parts is not None:
        assert plan_part is not None
        planner.place_nulls([{part.root} for part in parts])
        plans = [plan_part(part.query, part.model) for part in parts]
        return _plan_parts(planner, parts, plans, compile_leg=compile_leg)
    planner.check_per(islands)
    fact = planner.fact_island(islands)
    planner.place_nulls([islands[fact]])
    query = planner.query
    edges = planner.federation_edges(islands, fact)
    planner.check_leg_parents(islands, fact, edges)
    unnested = planner.unnested_objects(islands, fact)
    planner.check_unnested_dimensions(unnested)
    if query.select.is_raw:
        return _plan_raw(
            planner, islands, fact, edges, body_reads, compile_leg=compile_leg, leg_limit=leg_limit
        )
    rows_for = planner.check_measures(islands[fact])
    leg_filters, combine_filters, absent = planner.place_filters(islands, fact)
    planner.check_leg_parent_rows(leg_filters.get(fact, []), islands[fact])
    # Every column a leg gets that the model does not declare takes a name no
    # dimension, measure or metric has, nor another such column: a leg or the
    # combine model reading two columns of one name returned one for both.
    # Compared without case, as DuckDB compares identifiers, quoted or not.
    taken = _reserved_names(model)
    combine_filters, by_keys = planner.tested_by_keys(combine_filters, islands, fact, taken)
    absent += by_keys
    # The fields the combine step filters on: in ``where``, and in the
    # filters a measure's filter context includes.
    filtered = [
        *(
            field
            for item in combine_filters
            for field in _filter_fields(item)
            if field not in {k.tested for k in by_keys}
        ),
        *(item.field for context in planner.contexts() for item in context.include),
        *planner.per(),
    ]
    fact_dims, edge_dims, regrained = planner.dimensions_by_island(islands, fact, edges, filtered)
    deferred = planner.deferred_static_filters(islands[fact])
    # The edges from the fact island: the fact leg returns their keys.
    from_fact = [edge for edge in edges if edge.parent == fact]
    # Partial results count a row of an object the joins repeat once per
    # value the fact leg groups by beyond the query, and other objects'
    # measures beside a count of distinct values once per value it counts;
    # from rows, the combine step counts each once itself.
    rows_for = (
        rows_for
        or planner.repeats(from_fact, fact_dims, regrained.get(fact, []), deferred)
        or planner.counts_beside_others()
    )
    if rows_for:
        planner.check_rows(rows_for[0], islands[fact])
    from_rows = bool(rows_for)
    legs: list[FederatedLeg] = []
    combine_objects: dict[str, DataObject] = {}
    combine_dimensions: dict[str, Dimension] = {}
    combine_filters_static: list[ModelFilter] = []
    # The leg columns static filters read in the combine step, by the column
    # each one filters: a measure's filter context names that one.
    static_names: dict[str, list[str]] = {}

    # A combine filter on a dimension the query selects at a time grain reads
    # it at its own grain, from a column of its own: by island, each column's
    # name with the dimension's. Beside a metric across periods, each date
    # column is returned once as stored, and every dimension over it reads it
    # (*stored*, by island): the combine step applies its grains, and sees
    # ``Order Month`` and ``Order Date`` filter one column.
    own_grain: dict[int, dict[str, str]] = {}
    stored: dict[int, dict[str, str]] = {}
    for home, names in regrained.items():
        columns = own_grain.setdefault(home, {})
        if not planner.stores_dates():
            columns.update(
                {_unused(f"__og{home}_{n}", taken): name for n, name in enumerate(names)}
            )
            continue
        of_column: dict[tuple[str, str], str] = {}
        for name in names:
            dimension = model.dimensions[name]
            axis = (dimension.view, dimension.column)
            if axis not in of_column:
                of_column[axis] = _unused(f"__as{home}_{len(of_column)}", taken)
                columns[of_column[axis]] = name
            stored.setdefault(home, {})[name] = of_column[axis]
    # The edge keys, one name per key column, the same in both legs it joins.
    key_names = [
        [_unused(f"__fk{e}_{i}", taken) for i in range(len(edge.from_columns))]
        for e, edge in enumerate(edges)
    ]
    for edge, names in zip(edges, key_names, strict=True):
        planner.register_keys(edge, names)
    # The keys both legs of an edge return as their engine matches them
    # (:meth:`_Planner.match_keys`): by island, each key column's expression.
    matched: dict[int, dict[str, Expr]] = {}
    for edge, names in zip(edges, key_names, strict=True):
        edge_keys = planner.match_keys(edge, islands[edge.parent], islands[edge.island], names)
        for side in (edge.parent, edge.island):
            matched.setdefault(side, {}).update(edge_keys)
    # Each condition after the join, in ``where`` or a filter context's
    # ``include``, on a value its leg's engine compares otherwise than DuckDB,
    # evaluated by that leg (:meth:`_Planner.compared_in_leg`): by island, each
    # leg column's outcome; by condition, its column; and by field, those
    # columns, which a filter context names with the field. Its leg returns
    # the value too, a field the combine step filters on.
    island_of = {o: i for i, island in enumerate(islands) for o in island}
    in_leg: dict[int, dict[str, Expr]] = {}
    outcome_of: dict[tuple[str, str, str], str] = {}
    outcomes: dict[str, list[str]] = {}
    # An exists the combine step tests by a leg's keys is that test's column,
    # which a filter context keeping or leaving out its field names too.
    for by_key in by_keys:
        assert by_key.tested is not None
        outcomes.setdefault(by_key.item.field, []).append(by_key.tested)

    def outcome(leaf: QueryFilter) -> str | None:
        dimension = model.dimensions.get(leaf.field)
        if leaf.subquery is not None or dimension is None:
            return None
        home = island_of[dimension.view]
        if not planner.compared_in_leg(
            islands[home], dimension.result_type, leaf.op, leaf.value, grouped=home == fact
        ):
            return None
        key = _condition_key(leaf.field, leaf.op, leaf.value)
        if key not in outcome_of:
            condition = build_filter_expr(ColumnRef(name=leaf.field, table=_LEG_ALIAS), leaf, [])
            if condition is None:
                return None
            outcome_of[key] = _unused(f"__wf{len(outcome_of)}", taken)
            in_leg.setdefault(home, {})[outcome_of[key]] = _outcome(condition, leaf.op)
            outcomes.setdefault(leaf.field, []).append(outcome_of[key])
        return outcome_of[key]

    combine_filters = [_tested(item, outcome) for item in combine_filters]
    for context in planner.contexts():
        for included in context.include:
            outcome(QueryFilter(field=included.field, op=included.op, value=included.value))
    # The fields the combine step's filters still compare themselves.
    compared = {field for item in combine_filters for field in _filter_fields(item)} | {
        included.field
        for context in planner.contexts()
        for included in context.include
        if _condition_key(included.field, included.op, included.value) not in outcome_of
    }
    # A string the query sorts by is ranked by its leg, in its own engine, and
    # sorted by that rank after the combine query (:meth:`_Planner.ranked_sorts`).
    ranked = planner.ranked_sorts(islands, taken)
    for column, (home, rank) in ranked.items():
        in_leg.setdefault(home, {})[rank] = _rank_of(column)

    # The fact leg: the island's dimensions, the keys of the edges from it and
    # the measures. A dimension leg returns the keys of the edges from its own.
    out_keys = {
        island: {
            key_names[e][i]: (edge.from_object, column)
            for e, edge in enumerate(edges)
            if edge.parent == island
            for i, column in enumerate(edge.from_columns)
        }
        for island in range(len(islands))
    }
    fact_keys = out_keys[fact]
    measures = planner.atomic_measures()
    # A measure of another island's object is the combine step's alone, from
    # that island's leg (:meth:`_Planner.check_leg_parents`).
    fact_measures = measures - planner.leg_parents.keys()
    # The leg column counting the rows each of its rows stands for, and the
    # leg column of each column a measure reads, when the leg returns rows.
    row_count: str | None = None
    measure_columns: dict[tuple[str, str, bool], str] = {}
    # The measures of each object read through the fact rows, and its identity.
    parents: dict[str, list[str]] = {}
    identities: dict[str, list[str]] = {}
    if from_rows:
        parents = planner.parents()
        identities = {
            parent: dedup_identity(parent, names, resolved, model)
            for parent, names in parents.items()
        }
        leg_measures, row_count, distinct, measure_columns = _row_measures(
            model, fact_measures, taken, resolved.base_object, identities
        )
        averages: dict[str, tuple[str, str, int | None]] = {}
        variances: dict[str, tuple[str, str, str, int]] = {}
    else:
        leg_measures, averages, distinct, variances = _leg_measures(model, fact_measures, taken)
    # A minimum or maximum of strings combines as the value the fact leg ranks
    # least or greatest among its groups, in its own engine (:func:`_ranks`).
    ranks = {} if from_rows else _ranks(model, fact_measures, taken)
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
    planner.check_missing_rows(islands, edges, [(obj, col) for obj, col, _ in elsewhere])
    # A measure of another island's object reads what it reads, and the
    # object's identity, from that island's leg: a row per row of the object,
    # whose identity the combine step deduplicates it by.
    leg_parent_of: dict[str, str] = {}
    leg_identities: set[str] = set()
    for parent in sorted(set(planner.leg_parents.values())):
        owned = sorted(n for n, p in planner.leg_parents.items() if p == parent)
        identity = [(parent, c, False) for c in dedup_identity(parent, owned, resolved, model)]
        owned_reads = [r for n in owned for r in _read_columns(model.effective_measures[n])]
        for ref in [*identity, *owned_reads]:
            if ref not in measure_columns:
                measure_columns[ref] = _unused(f"__value{len(measure_columns)}", taken)
            elsewhere[ref] = island_legs[island_of[parent]]
        leg_identities |= {measure_columns[ref] for ref in identity}
        leg_parent_of |= dict.fromkeys(owned, island_legs[island_of[parent]])
    row_values = {
        column: (obj, col)
        for (obj, col, own), column in measure_columns.items()
        if (obj, col, own) not in elsewhere
    }
    # The keys are read in their own time, as one database's join reads
    # them, and so are the columns measures are ordered by.
    own_zone = {
        column
        for (obj, col, own), column in measure_columns.items()
        if own and (obj, col, own) not in elsewhere
    } | set(fact_keys)
    in_own_zone = _zone_pairs({**fact_keys, **row_values}, own_zone)
    # The filters the fact leg evaluates in its own engine, of measures
    # computed from rows, each measure's outcome a column of the leg.
    filtered_in_leg, predicates = _leg_predicates(
        model,
        [
            m
            for m in sorted(fact_measures)
            if from_rows and planner.filters_in_leg(m, islands[fact])
        ],
        row_values,
        taken,
    )
    predicates.update(in_leg.get(fact, {}))
    fact_own_grain = own_grain.get(fact, {})
    fact_body = frozenset(body_reads.get(fact, set()))
    # A static filter of the fact island a measure's filter context leaves
    # out runs in the combine step, on the column the fact leg returns for
    # it, as one of a dimension island does.
    fact_static = {
        _unused(f"__sf{n}", taken): (f.data_object, f.column, f) for n, f in enumerate(deferred)
    }
    fact_static_columns = {name: (obj, col) for name, (obj, col, _) in fact_static.items()}
    # Such a filter its leg evaluates (:meth:`_Planner.static_outcome`): the
    # leg column is its outcome, which the combine step tests.
    fact_static_outcomes = {
        name: found
        for name, (_, _, f) in fact_static.items()
        if (found := planner.static_outcome(islands[fact], name, f, grouped=True)) is not None
    }
    predicates.update(fact_static_outcomes)
    fact_model = planner.island_model(
        islands[fact] | fact_body,
        {**fact_keys, **fact_values, **row_values, **fact_static_columns},
        leg_measures,
        own_zone=own_zone,
        own_grain=fact_own_grain,
        after_join=[f for _, _, f in fact_static.values()],
    )
    fact_query = QueryObject(
        select=QuerySelect(
            dimensions=[
                *fact_dims,
                *fact_own_grain,
                *fact_keys,
                *fact_values,
                *row_values,
                *fact_static_columns,
            ],
            measures=sorted(leg_measures),
        ),
        where=leg_filters.get(fact, []),
        limit=leg_limit,
        use_path_names=_paths_within(query, islands[fact]),
    )
    fact_columns = [
        *[_dimension_column(model, DimensionRef.parse(d).name) for d in fact_dims],
        *[planner.own_column(name, column) for column, name in fact_own_grain.items()],
        *[
            _key_column(model, name, obj, col)
            for name, (obj, col) in {**fact_keys, **fact_values}.items()
        ],
        *[_value_column(model, name, obj, col) for name, (obj, col) in row_values.items()],
        *[
            _key_column(model, name, obj, col)
            for name, (obj, col) in fact_static_columns.items()
            if name not in fact_static_outcomes
        ],
        *[
            DataObjectColumn(name=n, code=n, abstract_type=m.result_type)
            for n, m in sorted(leg_measures.items())
        ],
    ]
    # Values the combine step groups by or counts are compared there as their
    # own engine compares them (:meth:`Dialect.equality_key`): the selected
    # dimensions without a grain, and the values a count of distinct values
    # counts. A rewritten dimension a combine filter compares keeps its value
    # too, under a name of its own, so the filter compares what it compared.
    grouped = {
        DimensionRef.parse(d).name
        for d in _dimension_refs(query)
        if not DimensionRef.parse(d).grain
    }
    kept = {name: _unused(f"__eqv{n}", taken) for n, name in enumerate(sorted(grouped & compared))}
    renamed = (
        {}
        if planner.stores_dates()
        else {name: column for found in own_grain.values() for column, name in found.items()}
    )
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
    fact_columns += [
        DataObjectColumn(name=column, code=column, abstract_type=DataType.INT)
        for column in [*predicates, *ranks.values()]
    ]
    fact_predicates = {
        **predicates,
        **matched.get(fact, {}),
        **{
            rank: WindowFunction(
                func_name="DENSE_RANK",
                order_by=[OrderByItem(expr=ColumnRef(name=name, table=_LEG_ALIAS))],
            )
            for name, rank in ranks.items()
        },
    }
    squares = {squared: scale for _, _, squared, scale in variances.values()}
    with (
        partial_sums(),
        partial_squares(planner.leg_dialect(islands[fact]), squares),
        columns_in_own_zone(in_own_zone),
        static_filters_after_join(fact_body),
    ):
        legs.append(
            planner.leg(
                "leg_0",
                islands[fact] | fact_body,
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
                predicates=fact_predicates,
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
            parents,
            fact_dims,
            from_fact,
            [names for edge, names in zip(edges, key_names, strict=True) if edge.parent == fact],
            {
                **fact_own_grain,
                **{
                    column: field
                    for field, columns in outcomes.items()
                    for column in columns
                    if column in in_leg.get(fact, {})
                },
            },
            fact_static_columns,
        ).items()
    }
    on_once |= {
        hidden: on_once[source] for hidden, source in fact_kept.items() if source in on_once
    }
    for name, (_, column, f) in fact_static.items():
        view = on_once.get(name, "leg_0")
        combine_filters_static.append(
            _static_after_join(f, view, name, tested=name in fact_static_outcomes)
        )
        static_names.setdefault(column, []).append(name)
    for d in fact_dims:
        name = DimensionRef.parse(d).name
        combine_dimensions[name] = _combine_dimension(model, name, on_once.get(name, "leg_0"))
    for column in in_leg.get(fact, {}):
        combine_dimensions[column] = _outcome_dimension(column, on_once.get(column, "leg_0"))
    for hidden, source in fact_kept.items():
        view = on_once.get(hidden, "leg_0")
        combine_dimensions[hidden] = _combine_dimension(model, source, view, hidden)
    for column, name in fact_own_grain.items():
        view = on_once.get(column, "leg_0")
        combine_dimensions[column] = _combine_dimension(model, name, view, column)
    for name, column in stored.get(fact, {}).items():
        view = on_once.get(column, "leg_0")
        combine_dimensions[name] = _combine_dimension(model, name, view).model_copy(
            update={"column": column}
        )

    # The fact leg may join a dimension leg as a map of its keys (KeyMap)
    # where it returns partial results grouped by more than that key, on an
    # engine measured for it, and the key is its leg's own: not read by a
    # nonexists leg or another of its columns, nor returned as its engine
    # matches it.
    key_maps: list[KeyMap] = []
    map_joins: dict[str, MapJoin] = {}
    grain = (
        len(fact_dims)
        + len(fact_own_grain)
        + len(fact_keys)
        + len(fact_values)
        + len(fact_static_columns)
    )
    maps_keys = not from_rows and grain > 1 and planner.leg_dialect(islands[fact]).pushes_key_maps
    fact_reads = {
        *(
            (model.dimensions[name].view, model.dimensions[name].column)
            for name in [*(DimensionRef.parse(d).name for d in fact_dims), *fact_own_grain.values()]
        ),
        *fact_values.values(),
        *fact_static_columns.values(),
    }
    absent_islands = {missing.island for missing in absent}
    map_names = {name.casefold() for name in fact_model.data_objects}

    # The joins of the dimension legs a chained edge starts from, by leg.
    leg_joins: dict[str, list[DataObjectJoin]] = {}

    def joins_from(e: int) -> list[DataObjectJoin]:
        """Where the combine step joins edge *e*'s legs from: what holds its key."""
        if edges[e].parent != fact:
            return leg_joins.setdefault(island_legs[edges[e].parent], [])
        owner = on_once.get(key_names[e][0])
        return once_joins.setdefault(owner, []) if owner else fact_joins

    # One leg per dimension island: its keys and the dimensions read there.
    for e, edge in enumerate(edges, start=1):
        leg_name = f"leg_{e}"
        keys = {
            key_names[e - 1][i]: (edge.to_object, column)
            for i, column in enumerate(edge.to_columns)
        }
        onward = out_keys[edge.island]
        dims = edge_dims[e - 1]
        dim_own_grain = own_grain.get(edge.island, {})
        island = islands[edge.island]
        body = body_reads.get(edge.island, set())
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
        } | {*keys, *onward}
        planner.check_unnested_reads(
            unnested,
            dims,
            dim_own_grain.values(),
            {
                **{
                    f"The join '{on.from_object}' -> '{on.to_object}'": [
                        (on.from_object, column) for column in on.from_columns
                    ]
                    for on in edges
                    if on.parent == edge.island
                },
                "A model filter": static_columns.values(),
                "A measure": read_here.values(),
            },
        )
        dim_model = planner.island_model(
            island | body,
            {**keys, **onward, **static_columns, **read_here},
            {},
            own_zone=read_own_zone,
            own_grain=dim_own_grain,
        )
        dim_query = QueryObject(
            select=QuerySelect(
                dimensions=[*dims, *dim_own_grain, *keys, *onward, *static_columns, *read_here]
            ),
            where=leg_filters.get(edge.island, []),
            limit=leg_limit,
            use_path_names=_paths_within(query, island),
        )
        # The conditions after the join this leg evaluates, its static
        # filters' among them: their columns are their outcomes.
        dim_predicates = {
            **in_leg.get(edge.island, {}),
            **{
                name: found
                for name, (_, _, f) in static.items()
                if (found := planner.static_outcome(island, name, f, grouped=False)) is not None
            },
        }
        # Where the leg runs filters of the query, the fact leg may read only
        # the keys it returns: as stored, where it returns a key as its
        # engine matches it (``matched``), in a column of their own.
        key_type = (
            _injectable_key_type(model, edge)
            if edge.island in leg_filters and edge.parent == fact
            else None
        )
        injected = next(iter(keys))
        key_values = None
        if key_type is not None and injected in matched.get(edge.island, {}):
            key_values = _unused(f"__fv{e - 1}", taken)
        dim_columns = [
            *[_dimension_column(model, DimensionRef.parse(d).name) for d in dims],
            *[planner.own_column(name, column) for column, name in dim_own_grain.items()],
            *[
                _key_column(model, name, obj, col)
                for name, (obj, col) in {**keys, **onward, **static_columns}.items()
                if name not in dim_predicates
            ],
            *[
                _value_column(model, name, obj, col).model_copy(
                    update={"primary_key": name in leg_identities}
                )
                for name, (obj, col) in read_here.items()
            ],
            *[
                DataObjectColumn(name=column, code=column, abstract_type=DataType.INT)
                for column in dim_predicates
            ],
            *([_key_column(model, key_values, *keys[injected])] if key_values else []),
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
            columns_in_own_zone(_zone_pairs({**keys, **onward, **read_here}, read_own_zone)),
            static_filters_after_join(after_join | body),
        ):
            legs.append(
                planner.leg(
                    leg_name,
                    island | body,
                    dim_query,
                    dim_model,
                    dim_columns,
                    compile_leg,
                    rekeyed=dim_rekeyed,
                    kept=dim_kept,
                    predicates={
                        **dim_predicates,
                        **matched.get(edge.island, {}),
                        **(
                            {key_values: ColumnRef(name=injected, table=_LEG_ALIAS)}
                            if key_values
                            else {}
                        ),
                    },
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
        for name, (_, column, f) in static.items():
            combine_filters_static.append(
                _static_after_join(f, leg_name, name, tested=name in dim_predicates)
            )
            static_names.setdefault(column, []).append(name)
        if edge.island in leg_filters:
            # The leg dropped the rows its filters reject: the combine step
            # keeps the fact rows it matched, which joins it also when the
            # query reads nothing else there.
            combine_filters_static.append(
                ModelFilter(
                    data_object=leg_name,
                    column=injected,
                    operator=FilterOperator.IS_NOT_NULL,
                    enforce=FilterEnforcement.ALWAYS,
                )
            )
            if key_type is not None:
                injections.append(KeyInjection(leg_name, injected, key_type, key_values))
        if (
            maps_keys
            and edge.parent == fact
            and edge.island not in absent_islands
            and leg_name not in leg_parent_of.values()
            and injected not in matched.get(fact, {})
            and _maps_key(model, edge, fact_reads, planner.leg_dialect(islands[fact]))
        ):
            key_maps.append(KeyMap(leg_name, injected))
            map_joins[injected] = MapJoin(
                name=_unused(f"__map{e - 1}", map_names),
                data_object=edge.from_object,
                column=edge.from_columns[0],
                inner=edge.island in leg_filters,
            )
        for d in dims:
            name = DimensionRef.parse(d).name
            combine_dimensions[name] = _combine_dimension(model, name, leg_name)
        for column in in_leg.get(edge.island, {}):
            combine_dimensions[column] = _outcome_dimension(column, leg_name)
        for hidden, source in dim_kept.items():
            combine_dimensions[hidden] = _combine_dimension(model, source, leg_name, hidden)
        for column, name in dim_own_grain.items():
            combine_dimensions[column] = _combine_dimension(model, name, leg_name, column)
        for name, column in stored.get(edge.island, {}).items():
            combine_dimensions[name] = _combine_dimension(model, name, leg_name).model_copy(
                update={"column": column}
            )
        joins_from(e - 1).append(
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
        island = islands[missing.island] | body_reads.get(missing.island, set())
        with (
            columns_in_own_zone(_zone_pairs(keys, keys)),
            static_filters_after_join(island - planner.in_subqueries),
        ):
            legs.append(
                planner.leg(
                    leg_name,
                    island,
                    QueryObject(
                        select=QuerySelect(dimensions=[*keys]),
                        where=[holds],
                        limit=leg_limit,
                        use_path_names=_paths_within(query, island),
                    ),
                    planner.island_model(island, keys, {}, own_zone=keys),
                    key_columns,
                    compile_leg,
                    predicates={
                        name: key
                        for name, key in matched.get(missing.island, {}).items()
                        if name in keys
                    },
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
        if missing.tested is not None:
            combine_dimensions[missing.tested] = Dimension(
                name=missing.tested,
                view=leg_name,
                column=next(iter(keys)),
                result_type=key_columns[0].abstract_type,
            )
        else:
            combine_filters_static.append(
                ModelFilter(
                    data_object=leg_name,
                    column=next(iter(keys)),
                    operator=FilterOperator.IS_NULL,
                    enforce=FilterEnforcement.ALWAYS,
                )
            )
        # Where the combine step counts the rows of the island's object, it
        # adds those no fact row reaches, which the test must reach too: the
        # leg joins that island's leg, on the same keys.
        counting_leg = island_legs[missing.island]
        joins = (
            leg_joins.setdefault(counting_leg, [])
            if counting_leg in leg_parent_of.values()
            else joins_from(e)
        )
        joins.append(
            DataObjectJoin(
                join_type=Cardinality.MANY_TO_ONE,
                join_to=leg_name,
                columns_from=list(keys),
                columns_to=list(keys),
            )
        )
    for name, joins in leg_joins.items():
        combine_objects[name] = combine_objects[name].model_copy(update={"joins": joins})

    # A dimension leg holds every row of its object. Where one database adds
    # none no fact row reaches (a filter on the orders drops the customers
    # without one), the combine step reads that object's rows the fact rows
    # reach, so its own grain_dedup adds none either.
    childless = {leg.parent for leg in resolved.childless_legs}
    reached: dict[str, tuple[str, list[str]]] = {}
    for e, edge in enumerate(edges):
        leg_name = f"leg_{e + 1}"
        if leg_name in leg_parent_of.values() and edge.to_object not in childless:
            relation = f"{leg_name}{_REACHED_RELATION}"
            combine_objects[leg_name] = combine_objects[leg_name].model_copy(
                update={"code": relation}
            )
            reached[relation] = (leg_name, key_names[e])

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
                on_rows = planner.reads_fact_rows(measure, parent, islands[fact])
                read |= {
                    measure_columns[r]
                    for r in _read_columns(effective[measure])
                    if r not in on_rows
                }
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
    if planner.stores_dates():
        # A dimension declaring its grain is a period there too: a rolling
        # window over months counts months. Its leg returned it at that grain.
        combine_dimensions = {
            name: dimension.model_copy(update={"time_grain": model.dimensions[name].time_grain})
            if name in model.dimensions
            else dimension
            for name, dimension in combine_dimensions.items()
        }
    combine_model = SemanticModel(
        name=model.name,
        settings=_combine_settings(model),
        data_objects=combine_objects,
        dimensions=combine_dimensions,
        measures={
            m: _combine_context(
                _row_measure(model, m, measure_columns, leg_parent_of[m], elsewhere)
                if m in leg_parent_of
                else _row_measure(
                    model,
                    m,
                    measure_columns,
                    once_of.get(m, "leg_0"),
                    elsewhere,
                    filtered_in_leg.get(m),
                    {
                        read: "leg_0"
                        for parent, names in parents.items()
                        if m in names
                        for read in planner.reads_fact_rows(m, parent, islands[fact])
                    },
                )
                if from_rows and m not in distinct
                else _combine_measure(
                    model, m, leg_columns.get(m), once_of.get(m, "leg_0"), ranks.get(m)
                ),
                renamed,
                {
                    field: [*static_names.get(field, []), *outcomes.get(field, [])]
                    for field in {*static_names, *outcomes}
                },
                outcome_of,
            )
            for m in measures
        },
        metrics=model.metrics,
        filters=combine_filters_static,
        expose_counts=False,
    )
    combine_query = query.model_copy(
        update={
            "select": QuerySelect(
                dimensions=[
                    d
                    if isinstance(d, CoalesceDimension) or planner.stores_dates()
                    else DimensionRef.parse(d).name
                    for d in query.select.dimensions
                ],
                measures=list(query.select.measures),
            ),
            "where": [_renamed(item, renamed) for item in combine_filters],
            "use_path_names": [],
            **({"order_by": [], "limit": None, "offset": None} if ranked else {}),
        }
    )
    combiner = DialectRegistry.get(COMBINE_DIALECT)
    with (
        combined_averages(combiner, {m: (c, scale) for m, (_, c, scale) in averages.items()}),
        combined_variances(
            combiner, {m: (c, sq, scale) for m, (c, _, sq, scale) in variances.items()}
        ),
        read_as_counts(_counts(model, measures)),
    ):
        combine_sql = compile_leg(combine_query, combine_model, COMBINE_DIALECT)
    if row_count:
        combine_sql = _with_rows(combine_sql, combiner, "leg_0", row_count, once_columns)
    if reached:
        combine_sql = _reached_rows(combine_sql, combiner, "leg_0", reached)
    if ranked:
        leg_of = {fact: "leg_0"} | island_legs
        combine_sql = _sorted_by_ranks(
            combine_sql,
            planner.sort(),
            {column: (leg_of[home], column, rank) for column, (home, rank) in ranked.items()},
            planner.shown(),
            query.limit,
            query.offset,
        )
    inject = None
    if injections or key_maps:
        inject = KeyInjector(
            leg=legs[0],
            query=fact_query.model_copy(deep=True),
            model=fact_model.model_copy(deep=True),
            columns=tuple(fact_columns),
            squares=squares,
            own_zone=in_own_zone,
            body=fact_body,
            rekeyed=fact_rekeyed,
            kept=fact_kept,
            predicates=fact_predicates,
            compile_leg=compile_leg,
            zone=planner.leg_zone(islands[fact] | fact_body),
            map_joins=map_joins,
        )
    return FederatedPlan(
        legs=tuple(legs),
        combine_sql=combine_sql,
        injections=tuple(injections),
        key_maps=tuple(key_maps),
        inject=inject,
        timezone=planner.result_zone,
    )


def _plan_raw(
    planner: _Planner,
    islands: list[set[str]],
    fact: int,
    edges: list[_Edge],
    body_reads: dict[int, set[str]],
    *,
    compile_leg: Callable[[QueryObject, SemanticModel, str], str],
    leg_limit: int | None,
) -> FederatedPlan:
    """A raw-mode query (``select.fields``) across sources: rows, joined.

    Each leg returns its island's rows with the columns the query reads
    there, as ``Object.Column``, and the join keys, read in their own time as
    one database's join reads them; the fact leg each fact row once. The
    combine step joins them as one database joins the objects, and returns
    the fields under the names the query gave them (:func:`raw_field_aliases`),
    distinct, ordered and limited as asked.

    A filter reading the fact island alone runs in the fact leg: the
    many-to-one joins repeat no fact row, so it keeps the rows one
    database's ``WHERE`` keeps. Any other runs after the join, on the
    columns the legs return, as one database runs it on the joined rows; a
    static filter on another island too (see :func:`plan_federated`). An
    ``exists`` there is refused: the combine step runs no subquery.
    """
    query, model, resolved = planner.query, planner.model, planner.resolved
    planner.check_missing_rows(islands, edges)
    island_of = {o: i for i, island in enumerate(islands) for o in island}
    leg_of = {fact: "leg_0"} | {edge.island: f"leg_{e}" for e, edge in enumerate(edges, start=1)}
    reads: dict[int, list[str]] = {island: [] for island in leg_of}
    keys: dict[int, dict[str, tuple[str, str]]] = {island: {} for island in leg_of}
    key_names: dict[tuple[str, str, bool], str] = {}
    taken = _reserved_names(model)

    def read(obj: str, column: str) -> str:
        """*obj*'s *column* among its leg's columns; the combine step's reference to it."""
        ref = f"{obj}.{column}"
        if ref not in reads[island_of[obj]]:
            reads[island_of[obj]].append(ref)
        return f"{leg_of[island_of[obj]]}.{ref}"

    def key(obj: str, column: str, matched: bool) -> str:
        """The leg column of *obj*'s join key *column*: a copy of its own (``island_model``).

        One for the joins reading it as stored, another for those reading it
        as its engine matches it (:meth:`_Planner.match_keys`): a column
        joined to legs on two engines has both.
        """
        if (obj, column, matched) not in key_names:
            key_names[obj, column, matched] = _unused(f"__fk{len(key_names)}", taken)
            keys[island_of[obj]][key_names[obj, column, matched]] = (obj, column)
        return f"{obj}.{key_names[obj, column, matched]}"

    fields = [read(f.object_name, f.column_name) for f in resolved.fields]
    joins_on: dict[_Edge, tuple[list[str], list[str]]] = {}
    for edge in edges:
        types = planner.matched_types(edge, islands[edge.parent], islands[edge.island])
        joins_on[edge] = (
            [
                key(edge.from_object, c, kind is not None)
                for c, kind in zip(edge.from_columns, types, strict=True)
            ],
            [
                key(edge.to_object, c, kind is not None)
                for c, kind in zip(edge.to_columns, types, strict=True)
            ],
        )
    for edge, (from_keys, to_keys) in joins_on.items():
        planner.register_keys(edge, [*from_keys, *to_keys])
    # The keys each leg returns as its engine matches them, as in the
    # aggregated plan (:meth:`_Planner.match_keys`).
    matched: dict[int, dict[str, Expr]] = {island: {} for island in leg_of}
    for edge, sides in joins_on.items():
        for island, names in zip((edge.parent, edge.island), sides, strict=True):
            matched[island] |= planner.match_keys(
                edge, islands[edge.parent], islands[edge.island], names
            )

    leg_where: list[QueryFilterItem] = []
    combine_where: list[QueryFilterItem] = []
    renamed: dict[str, str] = {}
    combine_dimensions: dict[str, Dimension] = {}
    # As in the aggregated plan, a condition after the join on a value its
    # leg's engine compares otherwise than DuckDB runs in that leg, which
    # returns its outcome (:meth:`_Planner.compared_in_leg`), and so does a
    # static filter's: by island, each outcome column's. A leg returns rows.
    in_leg: dict[int, dict[str, Expr]] = {island: {} for island in leg_of}

    def returned(island: int, outcome: Expr | None) -> str | None:
        """The leg column of a condition's *outcome* (:func:`_outcome`), in *island*'s leg."""
        if outcome is None:
            return None
        name = _unused(f"__wf{sum(map(len, in_leg.values()))}", taken)
        in_leg[island][name] = outcome
        combine_dimensions[name] = _outcome_dimension(name, leg_of[island])
        return name

    def outcome(leaf: QueryFilter) -> str | None:
        dimension = model.dimensions.get(leaf.field)
        if dimension is not None:
            obj, column, kind = dimension.view, dimension.column, dimension.result_type
        else:
            obj, column = (part.strip() for part in leaf.field.split(".", 1))
            kind = model.data_objects[obj].columns[column].abstract_type
        home = island_of[obj]
        if not planner.compared_in_leg(islands[home], kind, leaf.op, leaf.value, grouped=False):
            return None
        value = read(obj, column).split(".", 1)[1]
        condition = build_filter_expr(ColumnRef(name=value, table=_LEG_ALIAS), leaf, [])
        return returned(home, None if condition is None else _outcome(condition, leaf.op))

    for item in query.where:
        owners = {island_of.get(o) for o in planner.raw_filter_objects(item)}
        if owners == {fact}:
            leg_where.append(item)
            continue
        if any(True for _ in _exists_filters([item])):
            raise planner.refuse(
                "UNSUPPORTED_FILTER",
                "An exists filter in a raw-mode query runs in the leg of the facts' data "
                "source: on its subject there, and beside no condition on another source",
                hint="Ask for dimensions and measures instead, or keep the exists filter on "
                "the facts' data source and out of an OR with another source's conditions.",
            )
        for field in _filter_fields(item):
            dimension = model.dimensions.get(field)
            if dimension is not None:
                planner.check_zone_after_join(field)
                ref = read(dimension.view, dimension.column)
                leg, column = ref.split(".", 1)
                combine_dimensions[field] = dimension.model_copy(
                    update={"view": leg, "column": column, "via": None}
                )
            else:
                obj, column = (part.strip() for part in field.split(".", 1))
                renamed[field] = read(obj, column)
        combine_where.append(_tested(item, outcome))
    # A string field the query sorts by is ranked by its leg, as in the
    # aggregated plan: by field, the leg, its column there and its rank's.
    ranks: dict[str, tuple[str, str, str]] = {}
    for alias, (home, rank) in planner.ranked_sorts(islands, taken).items():
        sorted_field = next(f for f in resolved.fields if f.alias == alias)
        value = read(sorted_field.object_name, sorted_field.column_name).split(".", 1)[1]
        in_leg[home][rank] = _rank_of(value)
        ranks[alias] = (leg_of[home], value, rank)

    combine_filters_static: list[ModelFilter] = []
    legs: list[FederatedLeg] = []
    combine_objects: dict[str, DataObject] = {}
    for island, leg_name in leg_of.items():
        body = body_reads.get(island, set())
        objects = islands[island] | body
        # As in the aggregated plan: a static filter on another island runs
        # in the combine step, on the column its leg returns for it.
        after_join = body
        if island != fact:
            after_join = (islands[island] - planner.in_subqueries) | body
            for f in model.filters:
                if f.data_object in islands[island] - planner.in_subqueries:
                    column = read(f.data_object, f.column).split(".", 1)[1]
                    found = planner.static_outcome(islands[island], column, f, grouped=False)
                    tested = returned(island, found)
                    combine_filters_static.append(
                        _static_after_join(f, leg_name, tested or column, tested=tested is not None)
                    )
        own = keys[island]
        columns = [
            *[_raw_column(model, ref) for ref in reads[island]],
            *[_key_column(model, f"{obj}.{name}", obj, col) for name, (obj, col) in own.items()],
            *[
                DataObjectColumn(name=name, code=name, abstract_type=DataType.INT)
                for name in in_leg[island]
            ],
        ]
        leg_query = QueryObject(
            select=QuerySelect(fields=[c.name for c in columns if c.name not in in_leg[island]]),
            where=leg_where if island == fact else [],
            limit=leg_limit,
            use_path_names=_paths_within(query, islands[island]),
        )
        with (
            columns_in_own_zone(_zone_pairs(own, own)),
            static_filters_after_join(after_join),
        ):
            legs.append(
                planner.leg(
                    leg_name,
                    objects,
                    leg_query,
                    planner.island_model(objects, own, {}, own_zone=own),
                    columns,
                    compile_leg,
                    predicates={**in_leg[island], **matched[island]},
                    raw=True,
                )
            )
        combine_objects[leg_name] = DataObject(
            name=leg_name,
            code=leg_name,
            database="",
            schema_name="",
            columns={c.name: c for c in columns},
            countable=False,
        )
    for edge, (columns_from, columns_to) in joins_on.items():
        parent = leg_of[edge.parent]
        joins = [
            *combine_objects[parent].joins,
            DataObjectJoin(
                join_type=Cardinality.MANY_TO_ONE,
                join_to=leg_of[edge.island],
                columns_from=columns_from,
                columns_to=columns_to,
                required=edge.required,
            ),
        ]
        combine_objects[parent] = combine_objects[parent].model_copy(update={"joins": joins})

    combine_model = SemanticModel(
        name=model.name,
        settings=_combine_settings(model),
        data_objects=combine_objects,
        dimensions=combine_dimensions,
        measures={},
        metrics={},
        filters=combine_filters_static,
        expose_counts=False,
    )
    combine_query = query.model_copy(
        update={
            "select": QuerySelect(fields=fields, distinct=query.select.distinct),
            "where": [_renamed(item, renamed) for item in combine_where],
            "use_path_names": [],
            **({"order_by": [], "limit": None, "offset": None} if ranks else {}),
        }
    )
    aliases = {field: f.alias for field, f in zip(fields, resolved.fields, strict=True)}
    with raw_field_aliases(aliases):
        combine_sql = compile_leg(combine_query, combine_model, COMBINE_DIALECT)
    if ranks:
        combine_sql = _sorted_by_ranks(
            combine_sql, planner.sort(), ranks, planner.shown(), query.limit, query.offset
        )
    return FederatedPlan(legs=tuple(legs), combine_sql=combine_sql, timezone=planner.result_zone)


@dataclass(frozen=True)
class _Part:
    """One fact's part of a query split by fact (:meth:`_Planner.parts`).

    *query* is planned against *model* as a query of its own, of the rows of
    *root*, the fact's object, and what it reaches. Grouping by no
    dimension, it returns a row however many rows its fact has, none too,
    where one database's union leg returns none: such a part also counts
    them, as the measure *present* of *model* (:meth:`counting`). A part of
    *values* gives a group of the other parts its values, and adds none
    (:meth:`_Planner.context_parts`).
    """

    query: QueryObject
    model: SemanticModel
    root: str
    present: str | None = None
    values: bool = False

    def counting(self) -> _Part:
        """The part, also counting the rows of its root, under a name of its own.

        A ``bigint``: cast to the ``integer`` of a declared count, more rows
        than one holds failed a query that asks for no count at all.
        """
        present = _unused(_PRESENT, _reserved_names(self.model))
        measure = synthesize_count_measure(
            self.root, self.model.data_objects[self.root], present
        ).model_copy(update={"data_type": "bigint"})
        model = self.model.model_copy(
            update={"measures": {**self.model.measures, present: measure}}
        )
        measures = [*self.query.select.measures, present]
        select = self.query.select.model_copy(update={"measures": measures})
        return _Part(
            self.query.model_copy(update={"select": select}), model, self.root, present, self.values
        )


def _plan_parts(
    planner: _Planner,
    parts: list[_Part],
    plans: list[FederatedPlan],
    *,
    compile_leg: Callable[[QueryObject, SemanticModel, str], str],
) -> FederatedPlan:
    """A query split by fact (:meth:`_Planner.parts`): its parts' legs, and their union.

    Each part's legs are renamed apart, and its combine query reads them
    under their own names again, from common table expressions of its own.
    The combine step unions the parts' results, each a row per group of the
    dimensions its fact reaches, NULL for the others, and groups them by all
    of them, as one database groups its union of the facts. A part grouping
    by no dimension is in the union where it counted rows of its fact
    (:class:`_Part`): a fact with none adds no group, as in one database. A
    group holds a measure from one part at most, which the combine query
    takes (:func:`_part_measure`); there it computes the metrics,
    ``having``, ``orderBy`` and ``limit``. The parts' legs run at once, none
    filtered by another's keys. A part of values adds its rows to the groups
    the other parts hold, and none of its own (:func:`_given_values`).
    """
    query, model = planner.query, planner.model
    q = DialectRegistry.get(COMBINE_DIALECT).quote_identifier
    # The column each dimension the query selects is, as one database names it
    # (``column_name``): a later grain of one under a name of its own.
    column_of = dict(
        zip(_dimension_refs(query), (d.name for d in planner.resolved.dimensions), strict=True)
    )
    legs: list[FederatedLeg] = []
    ctes: list[str] = []
    union: list[str] = []
    values: list[str] = []
    for k, (part, plan) in enumerate(zip(parts, plans, strict=True)):
        names = {leg.name: f"{_PART_RELATION}{k}_{leg.name}" for leg in plan.legs}
        legs += [dataclasses.replace(leg, name=names[leg.name]) for leg in plan.legs]
        reading = ",\n".join(
            f"{q(own)} AS (SELECT * FROM {q(name)})" for own, name in names.items()
        )
        sql = plan.combine_sql.removeprefix("WITH ")
        sql = f"WITH {reading},\n{sql}" if sql != plan.combine_sql else f"WITH {reading}\n{sql}"
        ctes.append(f"{q(f'{_PART_RELATION}{k}')} AS (\n{sql}\n)")
        if query.grouping is not None:
            rows = _in_grouping_sets(query, column_of, part, f"{_PART_RELATION}{k}")
        elif part.present is None:
            rows = f"SELECT * FROM {q(f'{_PART_RELATION}{k}')}"
        else:
            present = q(part.present)
            rows = (
                f"SELECT * EXCLUDE ({present}) FROM {q(f'{_PART_RELATION}{k}')} WHERE {present} > 0"
            )
        if part.values:
            values.append(_given_values(query, column_of, part, rows))
        else:
            union.append(rows)
    if values:
        ctes.append(f"{q(_HELD_RELATION)} AS (\n" + "\nUNION ALL BY NAME\n".join(union) + "\n)")
        union = [f"SELECT * FROM {q(_HELD_RELATION)}", *values]
    ctes.append(f"{q(_PARTS_RELATION)} AS (\n" + "\nUNION ALL BY NAME\n".join(union) + "\n)")

    shown = planner.resolved.dimensions
    dimensions = [d.dimension for d in shown]
    # Each part returns a dimension under the column one database names it
    # (``column_name``): the dimension's name, and a later grain of it under
    # its own, ``Order Date:month`` or ``Order Date:value``. Each is read from
    # its column: the first as the dimension, a later grain as a dimension of
    # its own, which the query returns under its column's name (:func:`_returned`).
    taken = _reserved_names(model)
    inner: dict[str, str] = {}
    for n, d in enumerate(shown):
        later = d.dimension in inner.values()
        inner[d.name] = _unused(f"__dim{n}", taken) if later else d.dimension
    two_grains = len(set(dimensions)) != len(dimensions)
    measures = planner.atomic_measures()
    effective = model.effective_measures
    # A part computed each reaggregate metric (:meth:`_Planner.parts`): the
    # union holds it as a measure's value.
    reaggregates = {
        name: _part_reaggregate(model, name) for name in _reaggregates(planner.resolved, model)
    }
    counts = _counts(model, measures) | {
        name for name in reaggregates if model.metrics[name].aggregation is ReaggregateAggType.COUNT
    }
    # Under a rollup or cube, each row's grouping set is its flags (:func:`_in_grouping_sets`),
    # each read as a dimension named like its dimension's: a later grain's
    # flag, ``_g_Order Date:month``, would name a grain of ``_g_Order Date``.
    flags = (
        {grouping_flag_alias(d.name): grouping_flag_alias(inner[d.name]) for d in shown}
        if query.grouping
        else {}
    )
    columns = [
        *(_dimension_column(model, d.dimension, d.name) for d in shown),
        *(
            DataObjectColumn(name=m, code=m, abstract_type=effective[m].result_type)
            for m in sorted(measures)
        ),
        *(
            DataObjectColumn(name=m, code=m, abstract_type=reaggregates[m].result_type)
            for m in sorted(reaggregates)
        ),
        *(DataObjectColumn(name=f, code=f, abstract_type=DataType.INT) for f in flags),
    ]
    parts_model = SemanticModel(
        name=model.name,
        settings=_combine_settings(model),
        data_objects={
            _PARTS_RELATION: DataObject(
                name=_PARTS_RELATION,
                code=_PARTS_RELATION,
                database="",
                schema_name="",
                columns={c.name: c for c in columns},
                countable=False,
            )
        },
        dimensions={
            **{
                inner[d.name]: _combine_dimension(
                    model, d.dimension, _PARTS_RELATION, d.name
                ).model_copy(
                    update={
                        "name": inner[d.name],
                        # Filled, the date keeps its declared grain, whose
                        # periods a filter on it compares (``fill_wrap``).
                        "time_grain": model.dimensions[d.dimension].time_grain
                        if query.fill
                        else None,
                    }
                )
                for d in shown
            },
            **{
                read: Dimension(name=read, view=_PARTS_RELATION, column=f, result_type=DataType.INT)
                for f, read in flags.items()
            },
        },
        measures={
            **{m: _every_grain(_part_measure(model, m), inner, shown) for m in measures},
            **reaggregates,
        },
        metrics={
            n: m
            for n, m in model.metrics.items()
            if m.type in (MetricType.DERIVED, MetricType.CUMULATIVE, MetricType.WINDOW)
        },
        expose_counts=False,
    )
    returned = list(query.select.measures)
    # Filled, the date is read at its grain, which each part truncated it to
    # already, and its periods span the bounds the query's filters on it set,
    # as one database's (``fill_wrap``), filtering no row again.
    # So is it beside a metric across its periods, which they count at it.
    at_grain = query.fill or planner.reads_periods()
    read = [
        f"{inner[d.name]}:{d.grain.value}" if at_grain and d.grain else inner[d.name] for d in shown
    ]
    bounds = _fill_bounds(planner, parts_model, inner) if query.fill else []
    if query.grouping is None and not two_grains:
        parts_query = query.model_copy(
            update={
                "select": QuerySelect(dimensions=read, measures=returned),
                "where": [],
                "order_by": [
                    QueryOrderBy(
                        field=DimensionRef.parse(o.field).name,
                        direction=o.direction,
                        nulls=o.nulls,
                    )
                    for o in query.order_by
                ],
                "use_path_names": [],
            }
        )
        with fill_bounds(bounds), read_as_counts(counts):
            sql = compile_leg(parts_query, parts_model, COMBINE_DIALECT)
    else:
        # Each row is a group of the union by the dimensions, each read from
        # its own column, and the flags, which the query returns after its
        # measures, sorted as one database sorts them (:func:`_returned`).
        # A sort field names the column one database sorts by: a reference
        # its grain's, a name the dimension's first.
        sorted_as: dict[str, str] = {}
        for d in shown:
            sorted_as.setdefault(dimension_reference(d), d.name)
            sorted_as.setdefault(d.dimension, d.name)
        selected = {*sorted_as, *query.select.measures}
        sorted_by = [o.field for o in query.order_by if o.field not in selected]
        grouped = query.model_copy(
            update={
                "select": QuerySelect(
                    dimensions=[*read, *flags.values()], measures=[*returned, *sorted_by]
                ),
                "where": [],
                "grouping": None,
                "order_by": [],
                "limit": None,
                "offset": None,
                "use_path_names": [],
            }
        )
        with fill_bounds(bounds), read_as_counts(counts):
            sql = compile_leg(grouped, parts_model, COMBINE_DIALECT)
        sql = _returned(
            sql,
            query,
            [(inner[d.name], d.name) for d in shown],
            sorted_as,
            returned,
            [(read, f) for f, read in flags.items()],
        )
    with_ = ",\n".join(ctes)
    if sql.startswith("WITH "):
        sql = f"WITH {with_},\n{sql.removeprefix('WITH ')}"
    else:
        sql = f"WITH {with_}\n{sql}"
    return FederatedPlan(legs=tuple(legs), combine_sql=sql, timezone=planner.result_zone)


def _fill_bounds(
    planner: _Planner, parts_model: SemanticModel, inner: dict[str, str]
) -> list[Expr]:
    """The query's filters on its filled date's column, on the union's column of it.

    The parts ran them; the combine step's fill reads their bounds alone
    (:func:`~orionbelt.compiler.fill_wrap.fill_bounds`), on the dimension
    *inner* names the filled column, as one database's fill reads its
    ``where``: a filter on a dimension over the column without
    a grain bounds the periods by its dates; one declaring the grain the
    filled dimension declares, by its periods, which the fill compares its
    periods with too; any other bounds nothing.
    """
    model = planner.model
    (filled,) = [d for d in planner.resolved.dimensions if d.grain is not None]
    assert filled.grain is not None
    dimension = parts_model.dimensions[inner[filled.name]]
    column = make_column_expr(parts_model, _PARTS_RELATION, filled.name)
    period = (
        make_period_expr(parts_model, dimension, filled.grain)
        if dimension.time_grain == filled.grain
        else None
    )
    subjects: dict[TimeGrain | None, Expr | None] = {None: column, filled.grain: period}
    bounds: list[Expr] = []
    for item in planner.query.where:
        for condition in _item_conjuncts(item):
            if not isinstance(condition, QueryFilter) or condition.subquery is not None:
                continue
            on = model.dimensions.get(condition.field)
            if on is None or (on.view, on.column, on.via) != (
                filled.object_name,
                filled.column_name,
                filled.via,
            ):
                continue
            subject = subjects.get(on.time_grain)
            if subject is not None and (predicate := build_filter_expr(subject, condition, [])):
                bounds.append(predicate)
    return bounds


def _filter_leaves_of(item: QueryFilterItem) -> list[QueryFilter]:
    """Every condition of *item*, in any group of it."""
    if isinstance(item, QueryFilterGroup):
        return [leaf for sub in item.filters for leaf in _filter_leaves_of(sub)]
    return [item]


def _every_grain(
    measure: Measure, inner: dict[str, str], shown: list[ResolvedDimension]
) -> Measure:
    """*measure* with its grain naming each column of a dimension it names.

    A later grain of a dimension is a dimension of its own in the combine
    step (*inner*, by the column of each of the *shown* dimensions); one
    database's grain names the dimension at each grain the query selects.
    """
    grain = measure.grain
    later: dict[str, list[str]] = {}
    for d in shown:
        if inner[d.name] != d.dimension:
            later.setdefault(d.dimension, []).append(inner[d.name])
    if grain is None or not later:
        return measure

    def every(names: list[str]) -> list[str]:
        return [each for name in names for each in (name, *later.get(name, []))]

    regrained = grain.model_copy(
        update={
            "include": every(grain.include),
            "exclude": every(grain.exclude),
            "keep_only": every(grain.keep_only),
        }
    )
    return measure.model_copy(update={"grain": regrained})


def _grouping_sets(query: QueryObject, names: list[str]) -> list[frozenset[str]]:
    """The columns, *names*, each grouping set of *query*'s rollup or cube keeps."""
    if query.grouping == Grouping.ROLLUP:
        return [frozenset(names[:n]) for n in range(len(names), -1, -1)]
    return [frozenset(kept) for n in range(len(names), -1, -1) for kept in combinations(names, n)]


def _given_values(query: QueryObject, column_of: dict[str, str], part: _Part, rows: str) -> str:
    """The *rows* of a part of values (:meth:`_Planner.context_parts`) whose
    group one of the other parts holds.

    Each is a group of the dimensions its fact reaches, NULL on the others,
    and of the query's grouping set where it has a rollup or cube: a row of
    the others' (``_HELD_RELATION``) of the same dimensions, NULL ones too,
    and flags, as one database joins a filter context's scan to its groups.
    """
    q = DialectRegistry.get(COMBINE_DIALECT).quote_identifier
    given, held = q("__given"), q("__held")
    reached = {column_of[d] for d in _dimension_refs(part.query)}
    matches = [
        f"{held}.{q(c)} IS NOT DISTINCT FROM {given}.{q(c)}"
        if c in reached
        else f"{held}.{q(c)} IS NULL"
        for c in column_of.values()
    ]
    if query.grouping is not None:
        matches += [
            f"{held}.{q(flag)} = {given}.{q(flag)}"
            for flag in map(grouping_flag_alias, column_of.values())
        ]
    return (
        f"SELECT {given}.* FROM (\n{rows}\n) AS {given}\n"
        f"WHERE EXISTS (SELECT 1 FROM {q(_HELD_RELATION)} AS {held} "
        f"WHERE {' AND '.join(matches)})"
    )


def _in_grouping_sets(
    query: QueryObject, column_of: dict[str, str], part: _Part, relation: str
) -> str:
    """The rows of *part*, its result *relation*, in each grouping set of *query*'s rollup or cube.

    One database rolls up the union of the facts' rows, each NULL on the
    dimensions its fact does not reach. A part rolls up its own fact's rows
    over the dimensions it reaches, so its rows keeping those of a set's
    dimensions it reaches are that set's rows of its fact: NULL on the other
    dimensions, which the set keeps, a group of their own, or rolls up. One
    of its grouping sets can be several of the query's: the tickets per
    region are a region's tickets of no order month, and of every month.
    So the part's rows join the query's grouping sets, each its flags, on
    the flags of the dimensions it reaches; a part grouping by no dimension
    joins every set, where it counted rows. Each dimension is the column
    *column_of* names it: two grains of one are two columns, rolled up apart.
    """
    q = DialectRegistry.get(COMBINE_DIALECT).quote_identifier
    names = list(column_of.values())
    reached = [column_of[d] for d in _dimension_refs(part.query)]
    measures = [m for m in part.query.select.measures if m != part.present]
    flags = [grouping_flag_alias(name) for name in names]
    rows = ", ".join(
        "(" + ", ".join(str(int(name not in kept)) for name in names) + ")"
        for kept in _grouping_sets(query, names)
    )
    sets = q(_GROUPING_SETS_RELATION)
    columns = [
        *(f"{q(relation)}.{q(name)} AS {q(name)}" for name in [*reached, *measures]),
        *(f"{sets}.{q(flag)} AS {q(flag)}" for flag in flags),
    ]
    on = " AND ".join(
        f"{q(relation)}.{q(grouping_flag_alias(name))} = {sets}.{q(grouping_flag_alias(name))}"
        for name in reached
    )
    # A part's rows standing for none of its fact's are left out, but in the
    # grand total, which one database returns over no rows too.
    grand_total = " AND ".join(f"{sets}.{q(flag)} = 1" for flag in flags)
    where = (
        f"\nWHERE {q(relation)}.{q(part.present)} > 0 OR {grand_total}"
        if part.present is not None
        else ""
    )
    return (
        f"SELECT {', '.join(columns)}\nFROM {q(relation)}\n"
        f"JOIN (VALUES {rows}) AS {sets}({', '.join(map(q, flags))}) ON {on or 'TRUE'}{where}"
    )


def _rank_of(column: str) -> Expr:
    """The rank of a leg's value of *column* among its values, in the leg's engine."""
    return WindowFunction(
        func_name="DENSE_RANK",
        order_by=[OrderByItem(expr=ColumnRef(name=column, table=_LEG_ALIAS))],
    )


def _sorted_by_ranks(
    sql: str,
    order: list[QueryOrderBy],
    ranks: dict[str, tuple[str, str, str]],
    columns: set[str],
    limit: int | None,
    offset: int | None,
) -> str:
    """The rows of *sql* sorted by *order*, a string by its rank in its leg, then limited.

    By the column of *sql* each string is in, *ranks* names the leg returning
    it, its column there and the column of its rank (:meth:`_Planner.ranked_sorts`):
    each row reads the rank of its value, the same in every row of the leg
    holding it, a NULL none, and sorts by it. Any other sort field is one of
    the *columns* of *sql*, a measure ``2024`` too, else a position, as one
    database resolves it. *sql* is neither sorted nor limited.
    """
    q = DialectRegistry.get(COMBINE_DIALECT).quote_identifier
    rows = q(_SORTED_RELATION)
    joins: list[str] = []
    keys: dict[str, str] = {}
    for n, (column, (leg, value, rank)) in enumerate(ranks.items()):
        alias = q(f"{_RANKS_RELATION}{n}")
        joins.append(
            f"LEFT JOIN (\nSELECT DISTINCT {q(value)}, {q(rank)}\nFROM {q(leg)}\n"
            f"WHERE {q(value)} IS NOT NULL\n) AS {alias} "
            f"ON {rows}.{q(column)} = {alias}.{q(value)}"
        )
        keys[column] = f"{alias}.{q(rank)}"

    def key(o: QueryOrderBy) -> str:
        if o.field in keys:
            return keys[o.field]
        return f"{rows}.{q(o.field)}" if o.field in columns else o.field

    def nulls(o: QueryOrderBy) -> str:
        return f" NULLS {o.nulls.value.upper()}" if o.nulls is not None else ""

    sorted_by = ", ".join(f"{key(o)} {o.direction.value.upper()}{nulls(o)}" for o in order)
    result = "\n".join(
        [f"SELECT {rows}.*\nFROM (\n{sql}\n) AS {rows}", *joins, f"ORDER BY {sorted_by}"]
    )
    if limit is not None:
        result += f"\nLIMIT {limit}"
    if offset:
        result += f"\nOFFSET {offset}"
    return result


def _returned(
    sql: str,
    query: QueryObject,
    dimensions: list[tuple[str, str]],
    sorted_as: dict[str, str],
    measures: list[str],
    flags: list[tuple[str, str]],
) -> str:
    """The rows of *sql* as *query* returns them, as one database returns them.

    Its *dimensions*, each the column of *sql* holding it and the name one
    database returns it under; then *measures*, then the grouping *flags*,
    each named so too.
    Sorted as one database sorts: by the query's sort fields, a dimension's
    the column *sorted_as* names, else by its dimensions where it has a
    rollup or cube, or a limit; under a rollup or cube a NULL first where the
    query does not say. Then limited and offset.
    """
    q = DialectRegistry.get(COMBINE_DIALECT).quote_identifier
    columns = [
        *(f"{q(inner)} AS {q(alias)}" for inner, alias in dimensions),
        *map(q, measures),
        *(f"{q(inner)} AS {q(alias)}" if inner != alias else q(alias) for inner, alias in flags),
    ]
    sorted_by = query.order_by
    if not sorted_by and (query.grouping is not None or query.limit is not None):
        sorted_by = [QueryOrderBy(field=alias) for _, alias in dimensions]

    def nulls(o: QueryOrderBy) -> str:
        position = o.nulls or (NullsPosition.FIRST if query.grouping is not None else None)
        return f" NULLS {position.value.upper()}" if position is not None else ""

    order = ", ".join(
        f"{q(sorted_as.get(o.field, o.field))} {o.direction.value.upper()}{nulls(o)}"
        for o in sorted_by
    )
    rows = f"SELECT {', '.join(columns)}\nFROM (\n{sql}\n) AS {q(_GROUPED_RELATION)}"
    if order:
        rows += f"\nORDER BY {order}"
    if query.limit is not None:
        rows += f"\nLIMIT {query.limit}"
    if query.offset:
        rows += f"\nOFFSET {query.offset}"
    return rows


def _counts(model: SemanticModel, names: Iterable[str]) -> set[str]:
    """The measures of *names* that are counts.

    The combine step recombines them with another aggregation, and reads them
    as counts all the same (:func:`~orionbelt.compiler.count_zero.read_as_counts`).
    """
    effective = model.effective_measures
    return {
        name
        for name in names
        if effective[name].aggregation in (AggregationType.COUNT, AggregationType.COUNT_DISTINCT)
    }


def _part_measure(model: SemanticModel, name: str) -> Measure:
    """Measure *name* as the combine step takes it from the union of the parts.

    The part computing it gave each of its groups the finished value, typed
    and cast; a group of the union holds it once at most, so its ``MAX`` is
    that value. A group no row of its fact reaches holds none: a count reads
    0, as one database's count over no rows does, any other its default. A
    total, or a value at a coarser grain, is the same in each group of its
    fact that shares it, and one database gives it to every group that does,
    the other facts' too: the ``MAX`` over those groups, as a total or grain
    the measure keeps.
    """
    measure = model.effective_measures[name]
    counts = measure.aggregation in (AggregationType.COUNT, AggregationType.COUNT_DISTINCT)
    return Measure(
        name=name,
        columns=[DataColumnRef(view=_PARTS_RELATION, column=name)],
        aggregation=AggregationType.MAX,
        result_type=measure.result_type,
        data_type=measure.data_type,
        default_value=0 if counts else measure.default_value,
        format=measure.format,
        total=measure.total,
        grain=measure.grain,
    )


def _part_reaggregate(model: SemanticModel, name: str) -> Measure:
    """Reaggregate metric *name* as the combine step takes it from the union of the parts.

    The part of its fact computed it, typed and cast, in each group of that
    fact: its ``MAX`` is that value, as a measure's (:func:`_part_measure`).
    A group of another fact holds none, and reads NULL, as one database's
    join of the metric's groups to the union's gives it; a count reads 0,
    as there (:func:`~orionbelt.compiler.count_zero.read_as_counts`), in the
    type the metric declares. The 0 meets the value inside that type's cast,
    where the part gave the value the type already: a count declared
    ``string`` reads ``'0'``, as one database's cast 0 does.
    """
    metric = model.metrics[name]
    measure = model.effective_measures.get(metric.measure or "")
    zero: int | str | None = None
    if metric.aggregation is ReaggregateAggType.COUNT:
        result_type = DataType.INT
        text = resolve_metric_data_type(metric, model.settings) == SimpleType(name="string")
        zero = "0" if text else 0
    else:
        result_type = measure.result_type if measure is not None else DataType.DECIMAL
    return Measure(
        name=name,
        columns=[DataColumnRef(view=_PARTS_RELATION, column=name)],
        aggregation=AggregationType.MAX,
        result_type=result_type,
        data_type=metric.data_type,
        default_value=zero,
        format=metric.format,
    )


def _item_conjuncts(item: QueryFilterItem) -> list[QueryFilterItem]:
    """The conditions query filter *item* ANDs together, an AND group's flattened.

    Each restricts rows on its own, so a part applies the ones its fact
    reaches, as one database's union leg does (:func:`conjuncts`). A group
    of one condition is that condition, whatever its logic, as one database
    compiles it, and a group of none compiles to none. A negated group, and
    an OR group of several conditions, is one condition.
    """
    if not isinstance(item, QueryFilterGroup) or item.negated:
        return [item]
    filters = [sub for sub in item.filters if _filter_fields(sub)]
    if item.logic == FilterLogic.AND or len(filters) == 1:
        return [condition for sub in filters for condition in _item_conjuncts(sub)]
    return [item]


def _raw_column(model: SemanticModel, ref: str) -> DataObjectColumn:
    """The leg column of the ``Object.Column`` *ref*, under that name, typed as declared."""
    obj, column = ref.split(".", 1)
    declared = model.data_objects[obj].columns[column]
    return DataObjectColumn(name=ref, code=ref, abstract_type=declared.abstract_type)


@dataclass(frozen=True)
class _Edge:
    """A many-to-one join into a dimension island, from the island *parent*.

    The parent is the fact island, or a dimension island nearer it: a chain of
    sources, orders to the CRM's customers to the regions of a third.
    *required* is how the combine step joins it; *outer* whether its own join
    is a left join, which leaves the island's row missing for a fact row it
    does not match, before any inner join the leg makes.
    """

    from_object: str
    to_object: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    island: int
    required: bool
    parent: int
    outer: bool


@dataclass(frozen=True)
class _Absent:
    """A ``nonexists`` filter on a dimension island, and the island.

    The combine step keeps the fact rows whose key is not among those of
    the island's rows the subquery holds for: a leg of its own returns those
    keys (``exists``), and a fact row whose key has no row in the island at
    all is kept, as one database keeps it. An ``exists`` or ``nonexists``
    the combine step runs, in an ``or`` with a condition on another data
    source, *tested* that leg's key as a dimension of that name instead:
    set where the subquery holds for the fact row's row there.
    """

    island: int
    item: QueryFilter
    tested: str | None = None


class _Planner:
    def __init__(
        self,
        query: QueryObject,
        model: SemanticModel,
        resolved: ResolvedQuery,
        dialect_name: str,
        source_dialect: Callable[[str | None], str | None],
        timezone: str | None = None,
        source_timezone: Callable[[str | None, str], str | None] | None = None,
    ) -> None:
        self.query = query
        self.dialect_name = dialect_name
        self.source_dialect = source_dialect
        # A model that says which zone its timestamps were written in has every
        # leg read them so: as its defaultTimezone where it overrides the
        # database's, or from it into its queryTimezone. Else each leg converts
        # them from its source's zone, into the queryTimezone if there is one.
        settings = model.settings
        query_zone = settings.query_timezone if settings is not None else None
        stated = settings is not None and bool(
            settings.override_database_timezone or (query_zone and settings.default_timezone)
        )
        self.zone = (query_zone or timezone) if source_timezone is not None and not stated else None
        self.result_zone = query_zone or self.zone
        self.source_timezone = source_timezone
        self.leg_zones: dict[tuple[str | None, str], LegZone] = {}
        # Each key column a leg may return, by name: the joins it is a key of.
        self.key_joins: dict[str, list[str]] = {}
        self.model = model
        self.resolved = resolved
        self.sources = object_sources(model)
        self.objects = query_objects(resolved, query, model)
        # The objects only an ``exists`` subquery reads: the outer query of a
        # leg does not join them, and their static filters apply in the body.
        self.in_subqueries = self.objects - _outer_objects(resolved, model)
        # The query's joins, and those a reaggregate metric's first stage
        # makes to reach its ``per`` dimensions.
        self.join_steps = [*resolved.join_steps, *_per_steps(resolved, model)]
        # The measures of an object of another island the fact rows join
        # many-to-one, each with that object (:meth:`fact_island`).
        self.leg_parents: dict[str, str] = {}

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

    def check_period_dates(self) -> None:
        """Refuse a metric across periods whose dates the query does not join.

        Its time dimension's data object, when the query neither selects nor
        filters on it (a running total by region alone, over a calendar):
        one database joins it in a query of the metric's own, apart from the
        others, by the base object's own join, so it limits no other measure.
        A leg joining it would join it for every measure.
        """
        joined = _joined_objects(self.resolved)
        for metric, obj in sorted(_period_dates(self.resolved, self.model).items()):
            if obj not in joined:
                raise self.refuse(
                    "UNSUPPORTED_METRIC",
                    f"Metric '{metric}' reads the dates of '{obj}', which the query does not join",
                    hint="Select its time dimension, or query the metric on one data source.",
                )

    def check_query_shape(self) -> None:
        q = self.query
        if q.select.is_raw and self.resolved.requires_cfl:
            raise self.refuse(
                "UNSUPPORTED_QUERY",
                "A federated raw-mode query (select.fields) cannot read independent facts",
                hint="Select the fields of one fact and the objects it joins.",
            )

    # -- islands and edges --------------------------------------------------

    def islands(self) -> list[set[str]]:
        """The query's objects grouped by source and joined within it.

        An ``exists`` subquery runs where its subject is read, so the objects
        it alone reads join its subject's island; they must be of its data
        source. One the outer query reads too stays in its own island, and the
        subject's leg reads it for the subquery as well (:meth:`body_reads`).
        """
        parent = {o: o for o in self.objects}

        def find(o: str) -> str:
            while parent[o] != o:
                parent[o] = parent[parent[o]]
                o = parent[o]
            return o

        for step in self.join_steps:
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
            for o in read & self.in_subqueries:
                parent[find(o)] = find(subject)
        groups: dict[str, set[str]] = {}
        for o in sorted(self.objects):
            groups.setdefault(find(o), set()).add(o)
        return list(groups.values())

    def body_reads(self, islands: list[set[str]]) -> dict[int, set[str]]:
        """The objects of another island an ``exists`` subquery reads, by its subject's island.

        One database joins such an object twice: in the outer query, as an
        order's sales rep, and in the subquery, as the agent of the
        customer's ticket. Its own island's leg reads it for the first, the
        subject's leg for the second, its static filters left to its own leg
        (:func:`static_filters_after_join`).
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        reads: dict[int, set[str]] = {}
        for item in _exists_filters(self.query.where):
            home = island_of[_exists_subject(item, self.model) or ""]
            apart = {o for o in _exists_objects(item, self.model) if island_of.get(o) != home}
            if apart:
                reads.setdefault(home, set()).update(apart)
        return reads

    def check_computed_columns(self, islands: list[set[str]]) -> None:
        """Each dimension the query reads, and each static filter on an object it
        reads, is computed in its own data object's leg.

        A column computed from another data object's column reads it as the
        query joins it. One the query joins outside the island of the column's
        own object, on another data source or through another join (an
        order's sales rep, not the customer's manager), is not in that leg:
        its query would name a table it does not read, or, where an
        ``exists`` subquery reads that object there too (:meth:`body_reads`),
        the subquery's join of it. A static filter on an object only a
        subquery reads applies in the subquery, which joins what it reads.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        for what, obj, column in self._column_reads(island_of):
            home = island_of.get(obj)
            apart = sorted(
                o
                for o in self.model.column_reference_objects(obj, column)
                if island_of.get(o) != home
            )
            if apart:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"{what} is computed from {', '.join(apart)}, which the query does "
                    f"not join to '{obj}' within its data source",
                    hint=f"Compute it from columns of objects '{obj}' joins on its own "
                    "data source, or query it on one data source.",
                )

    def _column_reads(self, island_of: dict[str, int]) -> list[tuple[str, str, str]]:
        """The columns the query reads, each as ``(what, data object, column)``.

        Its dimensions, those a measure's filter context includes a filter
        on, its raw-mode fields and the columns its raw-mode filters name,
        and the columns of the static filters on the objects it reads (not
        only in a subquery).
        """
        q = self.query
        names = {DimensionRef.parse(d).name for d in _dimension_refs(q)}
        names |= {f for item in q.where for f in _filter_fields(item)}
        names |= {DimensionRef.parse(o.field).name for o in q.order_by}
        names |= {item.field for context in self.contexts() for item in context.include}
        reads: list[tuple[str, str, str]] = []
        for name in sorted(names):
            dimension = self.model.dimensions.get(name)
            if dimension is not None:
                reads.append((f"Dimension '{name}'", dimension.view, dimension.column))
            elif q.select.is_raw and "." in name:
                obj, column = (part.strip() for part in name.split(".", 1))
                if obj in island_of and column in self.model.data_objects[obj].columns:
                    reads.append((f"Field '{obj}.{column}'", obj, column))
        reads += [
            (f"Field '{f.object_name}.{f.column_name}'", f.object_name, f.column_name)
            for f in self.resolved.fields
        ]
        reads += [
            (f"The static filter on '{f.data_object}.{f.column}'", f.data_object, f.column)
            for f in self.model.filters
            if f.data_object in island_of and f.data_object not in self.in_subqueries
        ]
        return reads

    def check_missing_rows(
        self,
        islands: list[set[str]],
        edges: list[_Edge],
        measure_reads: Iterable[tuple[str, str]] = (),
    ) -> None:
        """Refuse a computed column the combine step reads as NULL where one database has a value.

        A fact row whose left join from another data source matches no row
        (an order whose customer is not in the CRM) reads every column of
        that source's objects as NULL in one database, and computes a
        computed column from those: ``COALESCE(region, 'unknown')`` is
        ``'unknown'``. The leg computes it over the rows it has, and the
        combine step reads NULL for the missing one, whether the query
        selects the column, filters on it or a measure reads it, and a join
        key: ``COALESCE(region_id, -1)`` joins the missing customer to the
        unknown region there. A column certainly NULL over NULL inputs reads
        the same (``null_rejection``), and one through a required join is
        never read for a missing row; an inner join within the island's leg
        does not say so, its key may be such a column.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        outer = {edge.island for edge in edges if edge.outer}
        reads = [
            *self._column_reads(island_of),
            *((f"A measure's column '{obj}.{col}'", obj, col) for obj, col in measure_reads),
            *(
                (f"The join key '{step.from_object}.{col}'", step.from_object, col)
                for step in self.join_steps
                for col in step.from_columns
            ),
        ]
        for what, obj, column in reads:
            home = island_of.get(obj)
            declared = self.model.data_objects[obj].columns.get(column)
            if home not in outer or declared is None or not declared.expression:
                continue
            expr = make_column_expr(self.model, obj, column, in_query_timezone=False)
            if not is_null_without(expr, islands[home]):
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"{what} is computed by an expression with a value where its row of "
                    f"'{obj}' is missing (a fact row its join from another data source does "
                    "not match), which the combine step would read as NULL",
                    hint="Compute it so it is NULL where its inputs are, or query it on one "
                    "data source.",
                )

    def parts(self, islands: list[set[str]]) -> list[_Part] | None:
        """The query split by fact, where one database unions facts of several islands.

        One database computes the measures of independent facts in a union of
        one leg per fact (``cfl``): each groups its fact's rows by the
        dimensions it reaches, NULL for the others, under the filters it
        reaches, and the union is grouped by all of them. Where those facts
        are on different islands, each is a query of its own, holding its
        measures and what its fact reaches, and the combine step unions their
        results (:func:`_plan_parts`). A filter a fact does not reach is left
        out of its part, with the warning one database gives, and so is a
        static filter (an ``enforce: always`` one is refused, by the part's
        own planning); of an AND group, only the conditions it does not
        reach. ``None`` where one island holds the facts: its leg unions them
        itself.

        A measure the query only orders by is a part's whose fact reaches its
        objects; one of another fact is refused: its groups would be rows of
        the query, where one database's are its sort keys alone.
        """
        if not self.resolved.requires_cfl or self.query.select.is_raw:
            return None
        effective = self.model.effective_measures
        graph = JoinGraph.of(self.model, use_path_names=self.resolved.use_path_names or None)

        def reach(root: str) -> set[str]:
            return graph.descendants_without_unnest(root) | {root}

        groups, across = group_measures_by_object(CFLPlanner(), self.resolved, self.model)
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        if len({island_of.get(root) for root in groups}) < 2:
            return None
        roots = {root: [m.name for m in measures] for root, measures in groups.items()}
        # One database's union leg of an object's measures starts at the object
        # that reaches it, the query's dimensions and filters, where there is
        # one: the customers' count, by order month, from the orders. A part
        # started at the customers would count them all in no month.
        wanted = {d.object_name for d in self.resolved.dimensions}
        for resolved_filter in self.resolved.where_filters:
            collect_table_refs(resolved_filter.expression, wanted)
            collect_correlated_tables(resolved_filter.expression, wanted)
        for start, names in sorted(roots.items()):
            lead = graph.find_common_root(wanted | {start})
            if names and lead and lead != start and start in reach(lead):
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"Measures of independent facts on several data sources are combined, "
                    f"but measure '{names[0]}' is of '{start}', which one database counts "
                    f"through '{lead}', the fact reaching the query's dimensions",
                    hint="Query the measure beside the measures of one fact.",
                )

        def home(name: str) -> str | None:
            sources = effective[name].source_objects
            return next((root for root in sorted(roots) if sources <= reach(root)), None)

        for name in sorted(self.resolved.order_only_measures & effective.keys()):
            if (root := home(name)) is None:
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"The query is ordered by measure '{name}', of a fact none of its "
                    "selected measures is of",
                    hint="Select the measure, or order by a measure of a selected fact.",
                )
            roots[root].append(name)
        # A measure with a filter context is a scan of its own on one database,
        # in no union leg: it is computed in the part of the fact it reads.
        grouped = {name for names in roots.values() for name in names}
        contexts = {
            m.name
            for m in [*self.resolved.measures, *self.resolved.metric_components.values()]
            if m.filter_context is not None and m.name in effective
        }
        for name in sorted(contexts - grouped):
            if (root := home(name)) is None:
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"Measure '{name}' has a filterContext, and reads data objects none of "
                    "the facts reaches",
                    hint="Query the measure separately.",
                )
            roots[root].append(name)
        # A reaggregate metric is a query of its own on one database, whose
        # groups it joins to the union's (``reaggregate_wrap``): it is computed
        # in the part of the fact whose measures it reads, which joins it to
        # that fact's groups. One another metric reads is in none yet. Its
        # first stage reads its measure, its ``per`` dimensions and the
        # measures of its ``having``, which that fact must all reach: a stage
        # over several facts is refused.
        placed = {name: root for root, names in roots.items() for name in names}
        for name in _reaggregates(self.resolved, self.model):
            metric = self.model.metrics[name]
            leaves = set(metric_leaf_measures(self.model, name))
            for h in metric.having:
                leaves |= (
                    metric_leaf_measures(self.model, h.field)
                    if (h.field in self.model.metrics)
                    else {h.field}
                )
            objects = {
                o for leaf in leaves if leaf in effective for o in effective[leaf].source_objects
            }
            for entry in metric.per:
                objects |= self._dimension_objects(reaggregate_per_ref(self.model, entry).name)
            root = placed.get(name) or next((r for r in sorted(roots) if objects <= reach(r)), None)
            if root is None or not objects <= reach(root):
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"Measures of independent facts on several data sources are combined, "
                    f"but metric '{name}' reaggregates, or keeps by its having, measures of "
                    "several of the facts, which is not supported yet",
                    hint="Reaggregate, and keep by, the measures of one fact.",
                )
            if name not in placed:
                roots[root].append(name)
            self.check_stage_one(name, reach(root))
        self.check_parts(across)
        parts = []
        for root, measures in sorted(roots.items()):
            fact = reach(root)
            select = QuerySelect(
                dimensions=[
                    d
                    for d in _dimension_refs(self.query)
                    if self._dimension_objects(DimensionRef.parse(d).name) <= fact
                ],
                measures=measures,
            )
            where: list[QueryFilterItem] = []
            for item in self.query.where:
                missing: set[str] = set()
                for condition in _item_conjuncts(item):
                    outside = self._item_objects(condition) - fact
                    if not outside:
                        where.append(condition)
                    missing |= outside
                if not missing:
                    continue
                report_unapplied_query_filter(
                    self.resolved.warnings,
                    ", ".join(sorted(missing)),
                    ", ".join(f"'{m}'" for m in measures),
                )
            for f in self.model.filters:
                missing = self._column_objects(f.data_object, f.column) - fact
                if missing and f.enforce == FilterEnforcement.RELEVANT:
                    report_unapplied_static_filter(
                        [],
                        self.resolved.warnings,
                        None,
                        ", ".join(sorted(missing)),
                        ", ".join(f"'{m}'" for m in measures),
                    )
            # The combine step fills the periods of the union, as one database
            # fills its query's (``fill_wrap``).
            update = {
                "where": where,
                "having": [],
                "order_by": [],
                "limit": None,
                "offset": None,
                "fill": False,
            }
            if not select.dimensions:
                update["grouping"] = None
            fact_parts = [
                _Part(self.query.model_copy(update={"select": select, **update}), self.model, root),
                *self.context_parts(root, select, where, update),
            ]
            # Under a rollup or cube, a part's grand total stands for no rows
            # of an empty fact: each part counts them (:func:`_in_grouping_sets`).
            if not select.dimensions or self.query.grouping is not None:
                fact_parts = [part.counting() for part in fact_parts]
            parts += fact_parts
        return parts

    def context_parts(
        self,
        root: str,
        select: QuerySelect,
        where: list[QueryFilterItem],
        update: dict[str, Any],
    ) -> list[_Part]:
        """The parts computing a filter context's measures on the other facts' groups too.

        One database joins a filter context's scan to every group of the union
        of the facts (``filter_wrap``). The part of its fact computes it on
        that fact's groups alone, which the part's filters narrowed: under a
        filter on the order date, which a ``mode: FIXED`` context drops, the
        orders of no late order left out the region the tickets have, and its
        revenue of all orders read NULL. Where a context drops a filter of the
        part, its measures are also computed under the filters it keeps, in a
        part whose rows give a group of the others its values but add none
        (:func:`_plan_parts`). One per set of filters kept.
        """
        if not select.dimensions:
            return []
        effective = self.model.effective_measures
        kept_by: dict[tuple[int, ...], list[str]] = {}
        for name in select.measures:
            context = effective[name].filter_context if name in effective else None
            if context is None:
                continue
            kept = kept_by_context(context, where, query_item_fields)
            if len(kept) < len(where):
                key = tuple(n for n, item in enumerate(where) if item in kept)
                kept_by.setdefault(key, []).append(name)
        return [
            _Part(
                self.query.model_copy(
                    update={
                        **update,
                        "select": select.model_copy(update={"measures": names}),
                        "where": [where[n] for n in key],
                    }
                ),
                self.model,
                root,
                values=True,
            )
            for key, names in kept_by.items()
        ]

    def check_stage_one(self, name: str, fact: set[str]) -> None:
        """Raise one database's error where reaggregate metric *name*'s first stage has one.

        One database groups the first stage by every dimension of the query
        and filters it by every filter (``reaggregate_wrap``), which its fact,
        reaching *fact*, may not reach: its own resolution refuses that, as a
        query of the fact's measures would. The part of the fact leaves those
        out, and computed the metric anyway.
        """
        outside = any(
            not self._dimension_objects(DimensionRef.parse(d).name) <= fact
            for d in _dimension_refs(self.query)
        ) or any(not self._item_objects(item) <= fact for item in self.query.where)
        if not outside:
            return
        metric = self.model.metrics[name]
        stage_one = QueryObject(
            select=QuerySelect(
                dimensions=[*_dimension_refs(self.query), *metric.per],
                measures=[metric.measure or ""],
            ),
            where=list(self.query.where),
            use_path_names=list(self.query.use_path_names),
            allow_fan_out=self.query.allow_fan_out,
        )
        try:
            QueryResolver().resolve(stage_one, self.model)
        except ResolutionError as exc:
            raise ResolutionError(
                [
                    e.model_copy(
                        update={
                            "message": f"In the first stage of reaggregating '{metric.measure}' "
                            f"per {metric.per}: {e.message}"
                        }
                    )
                    for e in exc.errors
                ]
            ) from exc
        raise self.refuse(
            "UNSUPPORTED_QUERY",
            f"Measures of independent facts on several data sources are combined, but "
            f"metric '{name}' reaggregates measures of a fact that does not reach every "
            "dimension and filter of the query, which is not supported yet",
            hint="Query the metric beside the dimensions and filters its fact reaches.",
        )

    def check_parts(self, across: list[ResolvedMeasure]) -> None:
        """Refuse what a query split by fact (:meth:`parts`) does not compute yet.

        A rollup or cube, a total, a grain, a filter context, ``fill``,
        ``asOf`` and one dimension at two grains are the combine step's
        (:func:`_plan_parts`): under a rollup or cube each part rolls up its
        dimensions, a total and a filter context too. So are cumulative and
        window metrics, over the union's periods at the grain the query
        selects them: not over a time dimension it does not select, nor one
        it filters, whose earlier periods one database reads (``time_lookback``)
        where each part kept only those the filter admits, nor over a span of
        rows, which the parts have grouped.

        A grain beside a rollup or cube is refused as on one database, before
        the parts: a part of no dimension does not group at all.
        """
        refuse_grain_under_grouping(self.resolved)
        refs = _dimension_refs(self.query)
        names = {DimensionRef.parse(d).name for d in refs}
        two_grains = len(set(refs)) != len(names)
        periods = self.reads_periods()
        time_dimensions = self._metric_time_dimensions()
        looked_back = {
            (dimension.view, dimension.column)
            for m in self.resolved.measures
            if m.is_cumulative
            and (dimension := self.model.dimensions.get(m.cumulative_time_dimension or ""))
        }
        filtered = {
            (dimension.view, dimension.column)
            for item in self.query.where
            for leaf in _filter_leaves_of(item)
            if (dimension := self.model.dimensions.get(leaf.field)) is not None
        }
        refused = [
            (bool(across), f"measure '{across[0].name}' reads several of them" if across else ""),
            (bool(self.resolved.anchored_measures), "a measure is anchored to another fact"),
            (
                periods and (self.query.grouping is not None or two_grains),
                "a metric reads values across periods beside a rollup, a cube or one "
                "dimension at two grains",
            ),
            (
                not set(time_dimensions) <= names,
                "a metric reads values across the periods of a time dimension the query "
                "does not select",
            ),
            (
                bool(looked_back & filtered),
                "a metric reads values across the periods of a date the query filters, "
                "which it reads before the filter's first period",
            ),
            (
                any(m.is_cumulative and m.cumulative_window_span for m in self.resolved.measures),
                "a cumulative metric reads a span of rows",
            ),
            (
                self.query.dimensions_exclude
                or any(isinstance(d, CoalesceDimension) for d in self.query.select.dimensions),
                "the query excludes dimensions or coalesces them",
            ),
        ]
        for refuses, why in refused:
            if refuses:
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"Measures of independent facts on several data sources are combined, "
                    f"but {why}, which is not supported yet",
                    hint="Query each fact's measures separately, or leave out this option.",
                )

    def _item_objects(self, item: QueryFilterItem) -> set[str]:
        """The data objects filter *item* reads: a dimension's and those it joins."""
        if isinstance(item, QueryFilterGroup):
            return {o for sub in item.filters for o in self._item_objects(sub)}
        if item.subquery is None and DimensionRef.parse(item.field).name in self.model.dimensions:
            return self._dimension_objects(DimensionRef.parse(item.field).name)
        return self._filter_objects(item)

    def fact_island(self, islands: list[set[str]]) -> int:
        """The island of the measures, which must also hold the base object.

        A measure belongs to the island its values come from, its ``columns``
        and ``expression``: the base's, when they come from it and from
        another island too. That other island is read through the joins from
        the base's, as a dimension is (:meth:`federation_edges`), and so are
        the columns its filters or ordering read there; the combine step
        computes such a measure from rows (:meth:`check_measures`). A measure
        of one object of another island the query joins, which one database
        reads through the fact rows' joins (``grain_dedup``), is the combine
        step's, from that island's leg (:meth:`check_leg_parents`); but one
        with a filter context, which one database computes in a query of its
        own.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        base = island_of[self.resolved.base_object]
        effective = self.model.effective_measures
        joined = {step.to_object for step in self.resolved.join_steps}
        self.leg_parents = {
            name: parent
            for name in self.atomic_measures()
            if len(sources := effective[name].source_objects) == 1
            and (parent := next(iter(sources))) in joined
            and island_of.get(parent, base) != base
            and effective[name].filter_context is None
        }
        owners: set[int] = set()
        for name in self.atomic_measures() - self.leg_parents.keys():
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
                hint="Query each data source's measures separately.",
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

    def check_leg_parents(self, islands: list[set[str]], fact: int, edges: list[_Edge]) -> None:
        """Refuse a measure of another island's object the combine step cannot compute.

        Such a measure (:attr:`leg_parents`), the customers' count beside the
        orders' revenue, is computed in the combine step from the dimension
        leg of its object, which returns a row per row of it, joined from the
        fact rows many-to-one: the combine step's own ``grain_dedup`` counts
        each row once per group, and adds the rows no fact row reaches where
        one database adds them, customers without orders
        (:meth:`check_leg_parent_rows`). So the object is the one the fact
        island joins in that island, and what its measures read has a value
        per row of it.
        """
        entries = {edge.to_object for edge in edges if edge.parent == fact}
        source = self._island_source(islands[fact])
        effective = self.model.effective_measures
        for name, parent in sorted(self.leg_parents.items()):
            if parent not in entries:
                raise self.refuse(
                    "MULTI_FACT",
                    f"Measure '{name}' is of '{parent}', which no data object of the "
                    f"facts' data source ({source}) joins",
                    hint="Query the measure separately.",
                )
            side = self._one_row_each(parent)
            for obj, column, _ in _read_columns(effective[name]):
                if not self._column_objects(obj, column) <= side:
                    raise self.refuse(
                        "MULTI_FACT",
                        f"Measure '{name}' is of '{parent}', but reads '{obj}.{column}', "
                        f"which one row of '{parent}' can have several values of",
                        hint="Query the measure separately.",
                    )
            # Its leg returns the values as stored, and DuckDB compares them:
            # a MySQL string by its bytes, where its collation calls 'a' and
            # 'A' one value.
            for obj, column, counted in _compared_columns(effective[name]):
                dialect = self.leg_dialect({obj})
                kind = self.model.data_objects[obj].columns[column].abstract_type.value
                if dialect.equality_key(ColumnRef(name=column), kind, counted=counted) is not None:
                    raise self.refuse(
                        "UNSUPPORTED_AGGREGATION",
                        f"Measure '{name}' is of '{parent}', which the combine step "
                        f"computes in DuckDB, which compares the values of '{obj}.{column}' "
                        f"otherwise than {dialect.name}",
                        hint="Query the measure separately.",
                    )

    def check_leg_parent_rows(
        self, fact_filters: Sequence[QueryFilterItem], fact_island: set[str]
    ) -> None:
        """Refuse a measure of another island's object whose rows the fact leg's filters pick.

        One database adds the rows of such an object (:attr:`leg_parents`)
        that no fact row reaches, customers without orders, where the WHERE
        can hold for them; it finds them among all the fact rows, and tests
        them with the fact's columns NULL. The combine step finds them among
        the fact rows the fact leg returns, which its filters, a query's or a
        static model filter's, may have dropped: a customer whose orders
        ``Order Date is_null`` rejects would count as one without orders. A
        filter the combine step runs is one it tests them with, as one
        database does.
        """
        childless = {leg.parent for leg in self.resolved.childless_legs}
        static = [f for f in self.model.filters if f.data_object in fact_island]
        for name, parent in sorted(self.leg_parents.items()):
            if parent in childless and (fact_filters or static):
                raise self.refuse(
                    "MULTI_FACT",
                    f"Measure '{name}' counts the rows of '{parent}' without a row of "
                    f"{self._island_source(fact_island)} too, and a filter on "
                    f"{', '.join(sorted(fact_island & self.objects))} decides which those are",
                    hint=f"Query the measure separately, or filter on a dimension of '{parent}'.",
                )

    def federation_edges(self, islands: list[set[str]], fact: int) -> list[_Edge]:
        """The joins between islands, each from an island nearer the measures.

        In the order they are reached from the fact island: an edge's parent
        comes before it. An edge is required, an inner join, where its join is,
        or where its island's leg makes one: one database's inner join to the
        regions after the left join to the customers drops the orders whose
        customer has no region, and so does an inner join to the leg of the
        customers that holds only those with one.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        inner = {
            island_of.get(step.from_object)
            for step in self.join_steps
            if step.join_type == JoinType.INNER
            and island_of.get(step.from_object) == island_of.get(step.to_object)
        }
        steps = [
            step
            for step in self.join_steps
            if island_of.get(step.from_object) != island_of.get(step.to_object)
        ]
        edges: list[_Edge] = []
        reached = {fact}
        while steps:
            ends = [{island_of.get(s.from_object), island_of.get(s.to_object)} for s in steps]
            ready = [s for s, touched in zip(steps, ends, strict=True) if touched & reached]
            if not ready:
                break
            step = ready[0]
            steps.remove(step)
            a, b = step.from_object, step.to_object
            label = f"'{a}' -> '{b}'"
            if _repeats_rows(step) or island_of.get(a) not in reached:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"The join {label} between data sources is not many-to-one from the "
                    "measures' side",
                    hint="Leave out the dimensions reached through this join, or keep these "
                    "data objects in one data source.",
                )
            target = island_of[b]
            if target in reached:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"The data source of '{b}' is joined more than once",
                    hint="Query these data objects on one data source.",
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
                    required=bool(declared and declared.required) or target in inner,
                    parent=island_of[a],
                    outer=not (declared and declared.required),
                )
            )
        for i, island in enumerate(islands):
            if i != fact and i not in reached:
                raise self.refuse(
                    "UNSUPPORTED_JOIN",
                    f"Data objects {', '.join(sorted(island))} are not joined to the "
                    "measures' data source",
                    hint="Query these data objects on one data source.",
                )
        for edge in edges:
            self._check_dimension_island(islands[edge.island], edge.to_object)
        return edges

    def _check_dimension_island(self, island: set[str], entry: str) -> None:
        """Every object of a dimension island is reached from its entry many-to-one.

        Or one-to-one. Otherwise the dimension leg would return several rows
        per key, and joining it would multiply the measures: one database
        repeats a fact row once per row of a many-to-many join, which a leg,
        one row per value it groups by, does not. An object unnested from
        one it reaches returns a row per element, and so do those joined on
        from it; the query is checked for them (:meth:`check_unnested_dimensions`,
        :meth:`check_unnested_reads`).
        """
        reached = {entry}
        changed = True
        while changed:
            changed = False
            for step in self.join_steps:
                if (
                    step.from_object in reached
                    and step.to_object in island
                    and step.to_object not in reached
                ):
                    if _repeats_rows(step):
                        raise self.refuse(
                            "UNSUPPORTED_JOIN",
                            f"'{step.from_object}' -> '{step.to_object}' would repeat the "
                            f"rows of '{entry}' in its data source",
                            hint="Leave out the dimensions reached through this join, or keep "
                            "these data objects in one data source.",
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
                    hint="Filter on a dimension of the nested data object instead, or query "
                    "it on one data source.",
                )

    def unnested_objects(self, islands: list[set[str]], fact: int) -> dict[str, str]:
        """The objects of other islands than the fact island read through an unnest.

        Each with the nested object it is read through: that object itself,
        and those joined on from it, on its island or a further one. A leg
        reading one returns a row per element of the array, several per key.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        through: dict[str, str] = {}
        changed = True
        while changed:
            changed = False
            for step in self.join_steps:
                target = step.to_object
                if target in through or island_of.get(target, fact) == fact:
                    continue
                origin = through.get(step.from_object) or (target if step.nested else None)
                if origin is not None:
                    through[target] = origin
                    changed = True
        return through

    def check_unnested_dimensions(self, unnested: dict[str, str]) -> None:
        """Refuse an aggregate across a dimension read through an unnest on another source.

        The leg of such a dimension returns a row per element of the array
        (:meth:`unnested_objects`), several per key, and the combine step
        joins each fact row once per element: grouped by the dimension, each
        group holds a fact row once, as one database's does, which counts
        each once (``grain_dedup``). What adds the groups up does not: a
        ``rollup`` or ``cube`` subtotal, a measure's ``total``, or a
        ``grain`` without the dimension would count an order once per tag of
        its customer.
        """
        effective = self.model.effective_measures
        across = [
            (self.query.grouping is not None, f"a {self.query.grouping} subtotal"),
            *(
                (bool(effective[m].total), f"measure '{m}', a total")
                for m in sorted(self.atomic_measures())
            ),
            *(
                (effective[m].grain is not None, f"measure '{m}', at its own grain")
                for m in sorted(self.atomic_measures())
            ),
        ]
        why = next((why for adds_up, why in across if adds_up), None)
        if why is None:
            return
        for ref in _dimension_refs(self.query):
            name = DimensionRef.parse(ref).name
            for obj in sorted(self._dimension_objects(name) & unnested.keys()):
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"Dimension '{name}' {_unnest_place(obj, unnested[obj])} on another "
                    f"data source than the measures': its leg returns a row per element, "
                    f"and {why} would count a fact row once per element",
                    hint="Select it without a subtotal, a total or a grain, or query it "
                    "on one data source.",
                )

    def check_unnested_reads(
        self,
        unnested: dict[str, str],
        dimensions: Iterable[str],
        own_grain: Iterable[str],
        columns: dict[str, Iterable[tuple[str, str]]],
    ) -> None:
        """Refuse a dimension leg returning a value read through an unnest that the
        query does not group by.

        A leg returns a row per distinct row of what it returns: a value of
        an object read through an unnest (:meth:`unnested_objects`) that no
        dimension the query groups by returns gives it several rows per key
        and group, one per element, and the combine step would count a fact
        row once per element. One database counts it once per group
        (``grain_dedup``): by region, filtered on ``Amount > 90 OR Tag =
        'gold'``, an order of a customer with two tags would count twice.

        A value is a column at a time grain: by month, the dates of the
        month are values of their own. A dimension reading a timestamp in the
        model's ``queryTimezone`` is a value of its own too: in Berlin, 00:30
        and 01:30 UTC on the night the clocks go back both show 02:30, and
        their hours, 0 and 1 as stored, both 2. *dimensions*
        are those the leg returns, at the grain the query selects them at or
        their own; *own_grain* those it returns at their own grain, or as
        stored beside a metric across periods; *columns* the other columns it
        returns, as stored, by what reads them, with the objects their
        expressions read. A ``per`` dimension groups its metric's first stage
        only, not the query's other measures.
        """
        dimensions_of = self.model.dimensions
        grouped = {
            self._leg_value(ref.name, ref.grain or dimensions_of[ref.name].time_grain)
            for ref in map(DimensionRef.parse, _dimension_refs(self.query))
        }
        stored = self.stores_dates()
        reads = [
            (
                f"Dimension '{ref.name}'",
                self._dimension_objects(ref.name),
                self._leg_value(ref.name, ref.grain or dimensions_of[ref.name].time_grain),
            )
            for ref in map(DimensionRef.parse, dimensions)
        ]
        reads += [
            (
                f"Dimension '{name}'",
                self._dimension_objects(name),
                (dimensions_of[name].view, dimensions_of[name].column, None, None)
                if stored
                else self._leg_value(name, dimensions_of[name].time_grain),
            )
            for name in own_grain
        ]
        reads += [
            (what, {obj} | self.model.column_reference_objects(obj, col), (obj, col, None, None))
            for what, pairs in columns.items()
            for obj, col in pairs
        ]
        for what, objects, value in reads:
            if value in grouped:
                continue
            for obj in sorted(objects & unnested.keys()):
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"{what} {_unnest_place(obj, unnested[obj])} on another data source than "
                    "the measures', which the query does not group by: its leg returns a row "
                    "per element, and the combine step would count a fact row once per element",
                    hint="Select what it reads as a dimension too, or query it on one data source.",
                )

    def _leg_value(
        self, name: str, grain: TimeGrain | None
    ) -> tuple[str, str, TimeGrain | None, str | None]:
        """The value a leg returns for dimension *name* at *grain*: its column at
        that grain, and the dimension where it reads a timestamp in the query's
        zone, the model's ``queryTimezone`` or one its leg converts it to
        (:meth:`leg_zone`), itself or one its expression reads (the hour of
        one): its SQL is not the stored column's, which a join key reads."""
        dimension = self.model.dimensions[name]
        view, column = dimension.view, dimension.column
        with timestamps_in_zone(self.leg_zone({view})):
            shown = make_column_expr(self.model, view, column)
        zoned = shown != make_column_expr(self.model, view, column, in_query_timezone=False)
        return (view, column, grain, name if zoned else None)

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

    def reads_periods(self) -> bool:
        """Whether a metric of the query reads its values across periods.

        A cumulative, period-over-period or window metric orders, offsets or
        frames the periods of its time dimension at the grain the query
        selects. The combine step computes it, as one database does, over
        the combined values: the legs return each dimension at its own grain,
        and the combine step applies the query's grains.
        """
        return any(rm.is_cumulative or rm.is_pop or rm.is_window for rm in self.resolved.measures)

    def stores_dates(self) -> bool:
        """Whether the legs return date dimensions as stored, the combine step their grains.

        Beside a metric across periods (:meth:`reads_periods`); with ``fill``,
        whose periods the combine step spans; and for a dimension the query
        selects at two grains (``Order Date`` beside ``Order Date:month``),
        which one stored column holds both of; and beside a reaggregate
        metric per a date (:meth:`per_dates`), or carrying values forward in
        time (:meth:`carries`).
        """
        refs = set(_dimension_refs(self.query))
        two_grains = len(refs) != len({DimensionRef.parse(d).name for d in refs})
        periods = self.reads_periods() or self.query.fill or two_grains
        return periods or self.per_dates() or self.carries()

    def per(self) -> list[str]:
        """The dimensions the query's reaggregate metrics group their first stage by."""
        return _per_dimensions(self.resolved, self.model)

    def per_refs(self) -> list[DimensionRef]:
        """Each ``per`` entry of the query's reaggregate metrics, at its grain."""
        return [
            reaggregate_per_ref(self.model, entry)
            for name in _reaggregates(self.resolved, self.model)
            for entry in self.model.metrics[name].per
        ]

    def carries(self) -> bool:
        """Whether a reaggregate metric of the query carries values forward in time."""
        return any(
            self.model.metrics[name].carry_forward
            for name in _reaggregates(self.resolved, self.model)
        )

    def per_dates(self) -> bool:
        """Whether a reaggregate metric groups its first stage by a date dimension.

        Its ``per`` grain may be finer than the query's (days, by month), so
        the legs return the dates as stored (:meth:`stores_dates`), and the
        combine step applies both grains.
        """
        return any(self.stored_type(name) is not None for name in self.per())

    def check_per(self, islands: list[set[str]]) -> None:
        """Refuse a ``per`` dimension the legs do not return as one database groups by it.

        The combine step groups a reaggregate metric's first stage by it over
        the legs' rows, which join it only many-to-one from the base object
        (:func:`_per_steps`). One database's first stage is a query of its own:

        - whose base object may be another, the orders for the customers'
          count per order date, which no such join reaches;
        - whose required join limits that stage alone, where a leg the query
          or another metric's first stage reads too would drop their rows:
          the fact leg, or the leg of a source either joins. The combine step
          joins the leg of a source only the first stage reads only where it
          reads it, as one database does;
        - whose members, with ``population: dimension``, are all of the
          dimension's: a leg lists them so only where it reads nothing else,
          not the fact leg, which holds those with rows, nor one reading them
          through another object, which holds those its rows reach.
        """
        reached = _joined_objects(self.resolved) | _step_objects(self.join_steps)
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        fact = island_of.get(self.resolved.base_object)
        # The islands whose legs the query reads beside the first stages.
        shared = {island_of[o] for o in _joined_objects(self.resolved) if o in island_of}
        reaggregates = _reaggregates(self.resolved, self.model)
        reads = {
            name: {
                o
                for entry in self.model.metrics[name].per
                for o in self._dimension_objects(reaggregate_per_ref(self.model, entry).name)
            }
            for name in reaggregates
        }
        for name in reaggregates:
            metric = self.model.metrics[name]
            refs = [reaggregate_per_ref(self.model, entry).name for entry in metric.per]
            members = reads[name]
            # And beside the other metrics' first stages, by what they read.
            others = {o for other in reaggregates if other != name for o in reads[other]}
            others |= _step_objects(_steps_to(self.resolved, self.model, others))
            beside = shared | {island_of[o] for o in others if o in island_of}
            for per in refs:
                objects = self._dimension_objects(per)
                limiting = sorted(
                    step.to_object
                    for step in _steps_to(self.resolved, self.model, objects)
                    if (step.join_type == JoinType.INNER or step.nested)
                    and island_of.get(step.to_object) in beside
                )
                homes = {island_of.get(o) for o in objects}
                why = None
                if objects - reached:
                    why = (
                        f"of {', '.join(sorted(objects - reached))}, which no many-to-one "
                        "join reaches from the rows of its measure"
                    )
                elif limiting:
                    why = (
                        f"reached by a required join to {', '.join(limiting)} the query "
                        "does not make: across data sources it would limit the query's "
                        "other measures and metrics too"
                    )
                elif metric.population == ReaggregatePopulation.DIMENSION and fact in homes:
                    why = (
                        "on the data source of its measure: across data sources its "
                        "population would hold only the members with rows of the measure"
                    )
                elif metric.population == ReaggregatePopulation.DIMENSION and any(
                    not islands[i] <= members for i in homes if i is not None
                ):
                    others = {o for i in homes if i is not None for o in islands[i]} - members
                    why = (
                        f"read beside {', '.join(sorted(others))} on its data source: across "
                        "data sources its population would hold only the members their "
                        "rows reach"
                    )
                if why is not None:
                    raise self.refuse(
                        "UNSUPPORTED_METRIC",
                        f"Metric '{name}' groups its first stage by '{per}', {why}",
                        hint="Query it on one data source.",
                    )

    def _metric_time_dimensions(self) -> list[str]:
        """The time dimensions of the query's metrics across periods, in the model."""
        names = {
            m.cumulative_time_dimension or m.pop_time_dimension or m.window_time_dimension
            for m in self.resolved.measures
            if m.is_cumulative or m.is_pop or m.is_window
        }
        return sorted(n for n in names if n in self.model.dimensions)

    def lookback_axes(self) -> set[tuple[str, str]]:
        """The ``(data object, column)`` of each time dimension a metric looks back along.

        A filter on one says which periods a cumulative or period-over-period
        metric shows, not which it reads (``compiler.time_lookback``): it runs
        in the combine step, which computes the metric over the periods
        before it too. With ``fill``, a filter on the time dimension bounds
        the periods filled, which the combine step reads off it as well. So
        does a reaggregate metric's ``carryForward`` along the query's dates:
        a member without a row in February takes its January value.
        """
        names = {
            m.cumulative_time_dimension if m.is_cumulative else m.pop_time_dimension
            for m in self.resolved.measures
            if m.is_cumulative or m.is_pop
        }
        if self.query.fill:
            names |= {
                DimensionRef.parse(d).name
                for d in _dimension_refs(self.query)
                if DimensionRef.parse(d).grain
                or self.model.dimensions[DimensionRef.parse(d).name].time_grain
            }
        if self.carries():
            names |= {
                DimensionRef.parse(d).name
                for d in _dimension_refs(self.query)
                if self.stored_type(DimensionRef.parse(d).name) is not None
            }
        dims = [self.model.dimensions.get(n or "") for n in names]
        return {(d.view, d.column) for d in dims if d is not None}

    def atomic_measures(self) -> set[str]:
        """The measures the fact leg computes: selected, and those metrics read."""
        effective = self.model.effective_measures
        # By name, not ``is_expression``: a measure written as an expression
        # is one too, and only a metric is left out.
        names = {m.name for m in self.resolved.measures if m.name in effective}
        names |= {name for name in self.resolved.metric_components if name in effective}
        # A reaggregate metric's first stage, a query of its own in the
        # combine step, computes its measure and those of its ``having``.
        names |= {
            leaf
            for name in _reaggregates(self.resolved, self.model)
            for leaf in metric_leaf_measures(self.model, name)
            if leaf in effective
        }
        for item in self.query.having:
            names |= {f for f in _filter_fields(item) if f in effective}
        names |= {o.field for o in self.query.order_by if o.field in effective}
        return names

    def contexts(self) -> list[FilterContext]:
        """The filter contexts of the measures the query computes."""
        effective = self.model.effective_measures
        return [
            context
            for name in sorted(self.atomic_measures())
            if (context := effective[name].filter_context) is not None
        ]

    def kept_everywhere(self, fields: frozenset[str]) -> bool:
        """Whether every measure's filter context keeps a filter reading *fields*.

        Only such a filter runs in a leg. One a context drops runs in the
        combine step, over the legs' unfiltered groups, where that measure's
        own scan leaves it out, as one database's does (``filter_wrap``).
        """
        return all(kept_by_context(context, [fields], lambda f: f) for context in self.contexts())

    def deferred_static_filters(self, fact_island: set[str]) -> list[ModelFilter]:
        """The static filters of the fact island a measure's filter context leaves out.

        On one database a context leaves out the ``relevant`` ones it does
        not keep, never the ``enforce: always`` ones. The fact leg returns
        their columns, and the combine step applies them, where that
        measure's scan leaves them out.
        """
        return [
            f
            for f in self.model.filters
            if f.data_object in fact_island - self.in_subqueries
            and f.enforce == FilterEnforcement.RELEVANT
            and not self.kept_everywhere(frozenset({f.column}))
        ]

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
        effective = self.model.effective_measures
        names = sorted(self.atomic_measures())
        for name in names:
            measure = effective[name]
            context = measure.filter_context
            for item in context.include if context is not None else []:
                if item.field not in self.model.dimensions:
                    raise self.refuse(
                        "UNSUPPORTED_FILTER",
                        f"Measure '{name}' includes a filter on '{item.field}' in its "
                        "filterContext, which is not a dimension",
                        hint="Filter on a dimension over the column.",
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
            if name not in self.leg_parents
            and any(obj not in fact_island for obj, _, _ in _read_columns(effective[name]))
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
        # A time span reads the measure over the rows in the span, not over
        # periods: only a measure the leg's groups add up to there, a sum, a
        # count, a minimum or a maximum, combines; any other from rows.
        spans = {
            m.cumulative_measure
            for m in self.resolved.measures
            if m.is_cumulative and m.cumulative_window_span is not None
        }
        # A measure of another island's object is computed from its own leg's
        # rows (:meth:`check_leg_parents`); one allowing fan-out, as one
        # database does, once per fact row its row joins, from those rows.
        return [
            name
            for name in names
            if (name in self.leg_parents and effective[name].allow_fan_out)
            or name not in self.leg_parents
            and (
                name in across
                or not self.combines(effective[name], fact_island)
                or (name in spans and not _adds_up(effective[name]))
            )
        ]

    def combines(self, measure: Measure, fact_island: set[str]) -> bool:
        """Whether *measure* combines from the fact leg's partial results (:func:`_leg_measures`).

        Sums, counts, minima, maxima and any value re-aggregate; an average
        divides its summed sums by its summed counts; a count of the distinct
        values of one column counts them across the groups they reach. A
        variance or standard deviation combines from its moments when its
        values are integers or decimals the leg squares exactly
        (:meth:`squares_exactly`). None of them with a total or a grain
        override but the re-aggregated ones, nor any other aggregation.
        """
        apart = measure.total or measure.grain is not None
        if _counts_distinct(measure):
            return _counts_distinct_column(measure) and not apart
        if measure.distinct:
            return False
        if measure.aggregation in _REAGGREGATE:
            return True
        if measure.aggregation == AggregationType.AVG:
            return not apart
        if measure.aggregation in _VARIANCES:
            return not apart and self.squares_exactly(measure, fact_island)
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

    def check_rows(self, reason: str, fact_island: set[str]) -> None:
        """Refuse what the combine step cannot compute from the fact leg's rows.

        The fact leg returns the rows of the query's base object grouped by
        everything the combine step reads of them, each group with the
        number of rows it stands for, and the combine step computes every
        measure from that many copies (:func:`_row_measures`). What does not
        survive it is a value the leg's engine compares otherwise than
        DuckDB, where a measure compares it (:func:`_compared_columns`): the
        combine step compares it as DuckDB does (:meth:`Dialect.equality_key`).
        A count of the distinct values of one column is counted as its engine
        counts, as when the leg combines partial results, and a measure's
        filters may run in the fact leg, in its engine (:meth:`filters_in_leg`).

        A measure of an object read through the fact rows (:meth:`parents`)
        is computed from that object's rows once each: what it reads must have
        one value per row of it, as columns of the object and of those it joins
        many-to-one have; but for its filters, which may read the fact rows
        too (:meth:`reads_fact_rows`).

        *reason* is the measure that needs the rows.
        """
        effective = self.model.effective_measures
        for parent, names in self.parents().items():
            side = self._one_row_each(parent)
            for name in names:
                on_rows = self.reads_fact_rows(name, parent, fact_island)
                for obj, column, own in _read_columns(effective[name]):
                    if (obj, column, own) in on_rows:
                        continue
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
            in_leg = self.filters_in_leg(name, fact_island)
            for obj, column, counted in _compared_columns(measure, filters=not in_leg):
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

    def reads_fact_rows(
        self, name: str, parent: str, fact_island: set[str]
    ) -> set[tuple[str, str, bool]]:
        """The columns measure *name*, of *parent*, reads from the fact rows, as read.

        Such a measure is computed from its object's rows once each
        (:meth:`parents`), but one database filters the rows of the query's
        joins before it counts each of its object's rows once per group: a
        customer's ages over the sales of a product count the customers with
        such a sale (``grain_dedup``). The combine step's own does the same,
        where a filter reads the fact rows' column rather than the object's:
        a plain column of the fact's island its row has several values of.
        """
        side = self._one_row_each(parent)
        reads: set[tuple[str, str, bool]] = set()
        for obj, column in _filter_columns(self.model.effective_measures[name].filters):
            declared = self.model.data_objects[obj].columns[column]
            if obj in fact_island and obj not in side and not declared.expression:
                reads.add((obj, column, False))
        return reads

    def filters_in_leg(self, name: str, fact_island: set[str]) -> bool:
        """Whether measure *name*'s filters run in the fact leg, when computed from rows.

        One database evaluates a measure's filters in its own engine; the
        combine step would in DuckDB, which compares some values otherwise: a
        MySQL string by bytes, not by its collation, so ``'Paid'`` would not
        equal ``'paid'``. Where a filter reads such a value, the fact leg
        evaluates them all and returns the outcome (:func:`_leg_predicates`).
        It can where they read only columns the leg returns as they are
        stored: of its own island, neither computed nor a date, which it
        returns in the query's time. A measure read through the fact rows
        (:meth:`parents`) is read from its object's rows, which do not.

        The leg evaluates them over its groups, one per value its engine
        calls equal: a comparison with values of the column's own type holds
        for every value of such a group or for none; a pattern, or a value
        of another type, may not. MySQL groups ``'a'`` with ``'a '`` under
        ``utf8mb4_general_ci``, and ``LIKE 'a'`` matches the first alone
        (:data:`_PATTERN_OPERATORS`); it groups ``'1'`` with ``'１'`` under
        ``utf8mb4_0900_ai_ci``, and ``= 1`` converts them to numbers, which
        tells them apart. Such filters stay refused.
        """
        measure = self.model.effective_measures[name]
        if not measure.filters or any(name in names for names in self.parents().values()):
            return False
        dialect = self.leg_dialect(fact_island)
        compares = False
        for leaf in _filter_leaves(measure.filters):
            ref = leaf.column
            if ref is None or not ref.view or not ref.column:
                return False
            declared = self.model.data_objects[ref.view].columns[ref.column]
            if (
                ref.view not in fact_island
                or declared.expression
                or declared.abstract_type in DATE_BEARING_TYPES
            ):
                return False
            kind = declared.abstract_type.value
            if dialect.equality_key(ColumnRef(name=ref.column), kind, counted=False) is None:
                continue
            compares = True
            if leaf.operator.lower() in _PATTERN_OPERATORS or any(
                value.data_type != declared.abstract_type for value in leaf.values
            ):
                return False
        return compares

    def repeats(
        self,
        edges: list[_Edge],
        fact_dims: list[str],
        own_grain: Iterable[str],
        deferred: Iterable[ModelFilter],
    ) -> list[str]:
        """The measures counted once per row of their object that partial results repeat.

        One database aggregates a measure of an object the query's joins repeat
        (customers' ages over their sales) over that object's distinct rows,
        per group of the query's dimensions (``grain_dedup``). The fact leg
        does the same, but per group of what *it* groups by, which is more:
        the federation keys, the dimensions only a filter after the join reads
        (*fact_dims* beyond the selected ones, and those selected at a time
        grain it reads at their own, *own_grain*), the columns of the static
        filters it leaves to the combine step (*deferred*), and the values a
        count of distinct values counts (:func:`_leg_measures`). The selected ones
        count too where the combine step adds groups across them: a
        ``rollup`` or ``cube`` subtotal, a measure's ``total`` or grain. A customer
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
        selected = {DimensionRef.parse(d).name for d in _dimension_refs(self.query)}

        # The objects each value the fact leg groups by beyond the query's
        # selected dimensions is read from.
        always = [
            set().union(*(self._column_objects(edge.from_object, c) for c in edge.from_columns))
            for edge in edges
        ]
        fact_names = [DimensionRef.parse(d).name for d in fact_dims]
        always += [self._dimension_objects(name) for name in fact_names if name not in selected]
        always += [self._dimension_objects(name) for name in own_grain]
        always += [self._column_objects(f.data_object, f.column) for f in deferred]
        effective = model.effective_measures
        for name in self.atomic_measures():
            if _counts_distinct_column(effective[name]):
                ref = effective[name].columns[0]
                always.append(self._column_objects(ref.view or "", ref.column or ""))

        # A measure with a filter context is deduplicated in a scan of its own
        # on one database, so the query's plan leaves it out
        # (``detect_replication``); the fact leg computes it beside the others.
        plan = detect_dedup_measures(self.resolved, model)
        targets = {**plan.measures, **plan.components, **self.resolved.dedup_targets}
        repeated = []
        for name, target in sorted(targets.items()):
            if name in self.leg_parents:
                continue
            measure = effective[name]
            if measure.distinct or measure.aggregation in MULTIPLICITY_SAFE_AGGREGATIONS:
                continue
            groupings = list(always)
            if self.query.grouping is not None or measure.total or measure.grain is not None:
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
        without sales. Not an object of another island, whose measures the
        combine step computes from its leg (:attr:`leg_parents`).
        """
        base = self.resolved.base_object
        effective = self.model.effective_measures
        parents: dict[str, list[str]] = {}
        for name in sorted(self.atomic_measures() - self.leg_parents.keys()):
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
        of_dimensions: dict[str, str],
        static: dict[str, tuple[str, str]],
    ) -> dict[str, str]:
        """The fact leg's dimensions and keys read through an object measures read, each to it.

        Its other columns of a dimension too, at its own grain or a condition's
        outcome on it (*of_dimensions*: each column's name with the
        dimension's), and the columns of the static filters the combine step
        applies (*static*: each column's name with the data object and column
        it reads).

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
        for column, name in of_dimensions.items():
            if owner := through(self._dimension_objects(name), f"dimension '{name}'"):
                read[column] = owner
        for edge, names in zip(edges, key_names, strict=True):
            objects = set().union(
                *(self._column_objects(edge.from_object, c) for c in edge.from_columns)
            )
            what = f"the key of the join from '{edge.from_object}' to '{edge.to_object}'"
            if owner := through(objects, what):
                read.update(dict.fromkeys(names, owner))
        for name, (obj, column) in static.items():
            what = f"the static filter on '{obj}.{column}'"
            if owner := through(self._column_objects(obj, column), what):
                read[name] = owner
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
            for step in self.join_steps:
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
        atomic = sorted(self.atomic_measures() - self.leg_parents.keys())
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

        A condition a measure's filter context drops runs in the combine step
        too (:meth:`kept_everywhere`).
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        legs: dict[int, list[QueryFilterItem]] = {}
        combine: list[QueryFilterItem] = []
        absent: list[_Absent] = []
        axes = self.lookback_axes()
        for item in self.query.where:
            if (
                axes
                and not any(True for _ in _exists_filters([item]))
                and self._on_axes(item, axes)
            ):
                combine.append(item)
                continue
            if not self.kept_everywhere(query_item_fields(item)):
                if any(True for _ in _exists_filters([item])):
                    raise self.refuse(
                        "UNSUPPORTED_FILTER",
                        "An exists filter a measure's filterContext leaves out is not "
                        "supported yet",
                        hint="Keep the exists filter in every measure's filterContext.",
                    )
                combine.append(item)
                continue
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
            combine.append(item)
        return legs, combine, absent

    def tested_by_keys(
        self,
        items: list[QueryFilterItem],
        islands: list[set[str]],
        fact: int,
        taken: set[str],
    ) -> tuple[list[QueryFilterItem], list[_Absent]]:
        """*items* the combine step runs, each ``exists`` in them a test of a leg's keys.

        The combine step runs no subquery. An ``exists`` or ``nonexists`` on a
        dimension island, in an ``or`` with a condition on another data source
        or in a group true where its subject is missing, tests the keys a leg of
        that island returns where its subquery holds (:class:`_Absent`): set for
        ``exists``, not for ``nonexists``, as a fact row with no row there reads
        them. One on the fact island is refused: the fact leg's groups are not
        its rows.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        held: list[_Absent] = []

        def tested(item: QueryFilterItem) -> QueryFilterItem:
            if isinstance(item, QueryFilterGroup):
                return item.model_copy(update={"filters": [tested(f) for f in item.filters]})
            if item.subquery is None:
                return item
            home = island_of.get(_exists_subject(item, self.model) or "")
            if home is None or home == fact:
                raise self.refuse(
                    "UNSUPPORTED_FILTER",
                    "An exists filter on the facts' data source that runs after the join "
                    "(beside a condition on another data source, or in a group true where "
                    "its subject is missing) is not supported yet",
                    hint="Filter with exists apart, or beside conditions of its subject's "
                    "data source.",
                )
            name = _unused(f"__ex{len(held)}", taken)
            held.append(_Absent(home, item, name))
            op = (
                FilterOperator.IS_NOT_NULL
                if item.op == FilterOperator.EXISTS
                else FilterOperator.IS_NULL
            )
            return QueryFilter(field=name, op=op)

        return [tested(item) for item in items], held

    def _on_axes(self, item: QueryFilterItem, axes: set[tuple[str, str]]) -> bool:
        """Whether *item* reads a time dimension's own column a metric looks back along."""
        for field in _filter_fields(item):
            dimension = self.model.dimensions.get(DimensionRef.parse(field).name)
            if dimension is not None and (dimension.view, dimension.column) in axes:
                return True
        return False

    def raw_filter_objects(self, item: QueryFilterItem) -> set[str]:
        """The data objects a raw-mode filter reads: a dimension's, or an ``Object.Column``'s.

        With those its column is computed from; an ``exists``'s subject.
        """
        if isinstance(item, QueryFilterGroup):
            return {o for sub in item.filters for o in self.raw_filter_objects(sub)}
        if item.subquery is not None:
            return {_exists_subject(item, self.model) or ""}
        dimension = self.model.dimensions.get(item.field)
        if dimension is not None:
            return {dimension.view} | self.model.dimension_join_objects(item.field)
        obj, _, column = (part.strip() for part in item.field.partition("."))
        return {obj} | self.model.column_reference_objects(obj, column)

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
                hint="Filter on a dimension over the column.",
            )
        return {dimension.view or ""}

    def dimensions_by_island(
        self,
        islands: list[set[str]],
        fact: int,
        edges: list[_Edge],
        filtered: Iterable[str],
    ) -> tuple[list[str], list[list[str]], dict[int, list[str]]]:
        """The dimensions each leg returns: selected, or *filtered* in the combine step.

        A leg returns a dimension once, at the grain the query selects it. A
        combine filter on one the query selects at a time grain compares it
        at its own grain, as one database does: its leg returns it so too, a
        column of its own. The third value holds those dimensions, by island.

        Beside a metric across periods (:meth:`reads_periods`) the third
        value holds every date dimension instead, which its leg returns as
        stored, and the combine step applies its grains: the dimension's own
        and the query's.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        wanted = _dimension_refs(self.query)
        grains: dict[str, set[TimeGrain | None]] = {}
        for d in wanted:
            grains.setdefault(DimensionRef.parse(d).name, set()).add(DimensionRef.parse(d).grain)
        # A reaggregate metric's ``per`` grain truncates its date too.
        for ref in self.per_refs():
            grains.setdefault(ref.name, set()).add(ref.grain)
        periods = self.stores_dates()
        if periods:
            wanted = [DimensionRef.parse(d).name for d in wanted]
        # Once each: a coalesce group's member may be selected beside it too.
        wanted = list(dict.fromkeys(wanted))
        selected = {DimensionRef.parse(d).name for d in wanted}
        at_grain = {DimensionRef.parse(d).name for d in wanted if DimensionRef.parse(d).grain}
        regrained: dict[int, list[str]] = {}
        for field in filtered:
            if field in at_grain:
                island = island_of[self.model.dimensions[field].view]
                if field not in regrained.get(island, []):
                    regrained.setdefault(island, []).append(field)
            elif field not in selected:
                wanted.append(field)
                selected.add(field)
        if periods:
            # A metric reads its time dimension's dates whether or not the
            # query selects it: a running total by region alone sums up to
            # the last of them.
            for name in self._metric_time_dimensions():
                if name not in selected:
                    wanted.append(name)
                    selected.add(name)
            dated = [d for d in wanted if self.stored_type(d) is not None]
            for name in dated:
                self._check_stored(name, grains.get(name, set()))
                island = island_of[self.model.dimensions[name].view]
                regrained.setdefault(island, []).append(name)
            wanted = [d for d in wanted if d not in dated]
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

    def own_column(self, name: str, column: str) -> DataObjectColumn:
        """The leg column of dimension *name* under a name of its own: typed as
        the date its column holds, beside a metric across periods."""
        own = _dimension_column(self.model, name, column)
        held = self.stored_type(name) if self.stores_dates() else None
        return own.model_copy(update={"abstract_type": held}) if held else own

    def stored_type(self, name: str) -> DataType | None:
        """The date type dimension *name*'s column holds, or ``None`` for another.

        The column's, not the dimension's: a dimension over a date may
        declare, or default to, a string, and its grain is still a period.
        """
        dimension = self.model.dimensions[name]
        column = self.model.data_objects[dimension.view].columns.get(dimension.column)
        held = column.abstract_type if column is not None and column.abstract_type else None
        held = held or dimension.result_type
        return held if held in DATE_BEARING_TYPES else None

    def _check_stored(self, name: str, grains: Collection[TimeGrain | None]) -> None:
        """Refuse a date dimension the combine step would read in its own time zone.

        Returned as stored, a timestamp with a time zone stays one, and the
        combine step's truncation, or cast to a date, reads it in DuckDB's
        zone rather than the one its source read it in: an order of Los
        Angeles' New Year's Eve moves into January. *grains* are every one
        the query asks it at: one of them truncates it.
        """
        dimension = self.model.dimensions[name]
        column = self.model.data_objects[dimension.view].columns.get(dimension.column)
        zoned = DataType.TIMESTAMP_TZ in {dimension.result_type, column and column.abstract_type}
        reshaped = dimension.time_grain is not None or any(g is not None for g in grains)
        if zoned and (reshaped or dimension.result_type != DataType.TIMESTAMP_TZ):
            raise self.refuse(
                "UNSUPPORTED_METRIC"
                if self.reads_periods() or self.per_dates()
                else "UNSUPPORTED_QUERY",
                f"Dimension '{name}' reads a timestamp with a time zone, which the combine "
                "step would truncate or cast in its own time zone, returned as stored (beside "
                "a metric across periods or reaggregating per a date, with fill, or at two "
                "grains)",
                hint="Select it as a timestamp with a time zone, without a grain.",
            )

    def check_zone_after_join(self, name: str) -> None:
        """Refuse a filter after the join on a date dimension its leg returns with a zone.

        In the model's ``queryTimezone``, a leg returns a timestamp with a
        time zone as that zone's wall-clock time, which the combine step
        truncates as the source does. Without one it comes back zoned, and the
        combine step's truncation, or cast to a date, reads it in DuckDB's
        zone, not the source's: an order placed at 00:30 UTC on New Year's Day
        is December's in Los Angeles and January's in UTC.
        """
        if self.model.settings is not None and self.model.settings.query_timezone:
            return
        dimension = self.model.dimensions[name]
        column = self.model.data_objects[dimension.view].columns.get(dimension.column)
        zoned = DataType.TIMESTAMP_TZ in {dimension.result_type, column and column.abstract_type}
        if zoned and (
            dimension.time_grain is not None or dimension.result_type != DataType.TIMESTAMP_TZ
        ):
            raise self.refuse(
                "UNSUPPORTED_FILTER",
                f"Dimension '{name}' truncates or casts a timestamp with a time zone, which "
                "a filter after the join would do in the combine step's time zone, not its "
                "data source's",
                hint="Set the model's queryTimezone, or query it on one data source.",
            )

    # -- legs ---------------------------------------------------------------

    def island_model(
        self,
        island: set[str],
        keys: dict[str, tuple[str, str]],
        measures: dict[str, Measure],
        *,
        own_zone: Iterable[str] = (),
        own_grain: dict[str, str] | None = None,
        after_join: Collection[ModelFilter] = (),
    ) -> SemanticModel:
        """The model a leg compiles against: the island alone, keys as dimensions.

        It keeps the static filters of the island's objects but *after_join*;
        a dimension leg leaves some to the combine step as well
        (:func:`static_filters_after_join`).
        Each of *own_grain* (a column's name, a dimension's) is that dimension
        under the column's name, at its own grain; beside a metric across
        periods, as stored (:meth:`dimensions_by_island`).
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
        # A role's object (``models.roles``) is joined from its ``via``: one on
        # another island is the edge into this one, which the combine step
        # joins, so the leg reads the role's object as any other.
        dimensions = {
            name: dim
            if dim.via is None or dim.via in island
            else dim.model_copy(update={"via": None})
            for name, dim in model.dimensions.items()
            if dim.view in island
        }
        periods = self.stores_dates()
        for column, name in (own_grain or {}).items():
            # Beside a metric across periods, the date as stored.
            stored = {"time_grain": None, "result_type": self.stored_type(name)} if periods else {}
            dimensions[column] = dimensions[name].model_copy(update={"name": column, **stored})
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
                "filters": [
                    f for f in model.filters if f.data_object in island and f not in after_join
                ],
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

    def compared_in_leg(
        self, island: set[str], kind: DataType | None, op: str, value: Any, *, grouped: bool
    ) -> bool:
        """Whether a condition after the join on a value of *kind* runs in *island*'s leg.

        The combine step compares values in DuckDB, which compares strings
        bytewise; MySQL compares one by its column's collation, so ``Region
        = 'eu'`` holds for ``'EU'`` there (:meth:`Dialect.equality_key`) and
        not after the join. The leg of such a value evaluates the condition
        in its own engine instead, and returns its outcome (:func:`_outcome`),
        which the combine step tests. A null test compares no value.

        A leg that *grouped* the rows it returns (the fact leg's partial
        results, or its rows) returns one of the values its engine calls
        equal for all of them: only a comparison with values of the column's
        own type holds for every one of them or for none (as
        :meth:`filters_in_leg` evaluates a measure's). A pattern, or a
        number, stays in the combine step. A dimension leg returns one value
        per key.
        """
        if kind is None:
            return False
        dialect = self.leg_dialect(island)
        if dialect.equality_key(ColumnRef(name=""), kind.value, counted=False) is None:
            return False
        operator = FilterOperator(op)
        if operator in _VALUELESS_OPERATORS:
            return False
        if not grouped:
            return True
        values = value if isinstance(value, list) else [value]
        return operator in _COMPARISON_OPERATORS and all(isinstance(v, str) for v in values)

    def static_outcome(
        self, island: set[str], name: str, f: ModelFilter, *, grouped: bool
    ) -> Expr | None:
        """Static filter *f*'s outcome over the leg column *name* of its value,
        where its leg evaluates it (:meth:`compared_in_leg`), or ``None``."""
        declared = self.model.data_objects[f.data_object].columns.get(f.column)
        value = f.value if f.value is not None else (f.values or None)
        if declared is None or not self.compared_in_leg(
            island, declared.abstract_type, f.operator, value, grouped=grouped
        ):
            return None
        condition = build_filter_expr(
            ColumnRef(name=name, table=_LEG_ALIAS),
            QueryFilter(field=name, op=f.operator, value=value),
            [],
        )
        return None if condition is None else _outcome(condition, f.operator)

    def matched_types(self, edge: _Edge, parent: set[str], child: set[str]) -> list[str | None]:
        """How *edge*'s legs return each of its key columns: as stored (``None``),
        or as their engine matches it (:meth:`Dialect.match_key`), by its type.

        The legs are those of *parent* and *child*, on one engine; a key of
        one type on both sides. Legs on two engines match keys bytewise:
        neither's equality is one database's.
        """
        dialect = self.leg_dialect(parent)
        if dialect.name != self.leg_dialect(child).name:
            return [None] * len(edge.from_columns)
        objects = self.model.data_objects
        types: list[str | None] = []
        for from_column, to_column in zip(edge.from_columns, edge.to_columns, strict=True):
            kind = objects[edge.from_object].columns[from_column].abstract_type.value
            other = objects[edge.to_object].columns[to_column].abstract_type.value
            matched = kind == other and dialect.match_key(ColumnRef(name=""), kind) is not None
            types.append(kind if matched else None)
        return types

    def match_keys(
        self, edge: _Edge, parent: set[str], child: set[str], names: list[str]
    ) -> dict[str, Expr]:
        """*edge*'s keys its legs return as their engine matches them (:meth:`matched_types`).

        By the leg column of each, *names* naming the edge's key columns in
        order, its expression over that column.
        """
        dialect = self.leg_dialect(parent)
        matched: dict[str, Expr] = {}
        for name, kind in zip(names, self.matched_types(edge, parent, child), strict=True):
            if kind is not None:
                key = dialect.match_key(ColumnRef(name=name, table=_LEG_ALIAS), kind)
                assert key is not None
                matched[name] = key
        return matched

    def register_keys(self, edge: _Edge, names: Iterable[str]) -> None:
        """Note *names*, leg columns holding *edge*'s keys, for :attr:`FederatedLeg.joins`."""
        label = self.join_label(edge)
        for name in names:
            joins = self.key_joins.setdefault(name, [])
            if label not in joins:
                joins.append(label)

    def join_label(self, edge: _Edge) -> str:
        """*edge* as an error names it: its key columns, and the data sources it joins."""

        def side(obj: str, columns: tuple[str, ...]) -> str:
            return f"{obj}.{columns[0]}" if len(columns) == 1 else f"{obj}.({', '.join(columns)})"

        sources = (
            source_name(self.model, edge.from_object),
            source_name(self.model, edge.to_object),
        )
        between = " and ".join(f"'{s}'" if s else "no data source" for s in sources)
        return (
            f"{side(edge.from_object, edge.from_columns)} -> "
            f"{side(edge.to_object, edge.to_columns)} (between {between})"
        )

    def leg_dialect(self, island: set[str]) -> Dialect:
        """The dialect the leg of *island* is written in."""
        source = source_name(self.model, next(iter(sorted(island))))
        return DialectRegistry.get(self.source_dialect(source) or self.dialect_name)

    def place_nulls(self, facts: list[set[str]]) -> None:
        """Sort NULL where the engine of the facts, of the islands *facts*, sorts it.

        One database leaves NULL's place in a sort that does not say to its
        engine (:attr:`~orionbelt.dialect.base.Dialect.nulls_last_ascending`),
        also in the sort it gives a limited query naming none: by its
        dimensions, or its fields. The combine step sorts in DuckDB, which
        puts NULL last either way, and is told the facts' engine's place.
        Facts on engines placing it apart leave it to DuckDB. Under a rollup
        or cube one database sorts NULL first, as the combine step does.
        """
        if self.query.grouping is not None:
            return
        places = {
            (d.nulls_last_ascending, d.nulls_last_descending) for d in map(self.leg_dialect, facts)
        }
        if len(places) != 1:
            return
        ascending, descending = places.pop()
        order = self.query.order_by
        if not order and self.query.limit is not None:
            fields = self.query.select.fields or [d.name for d in self.resolved.dimensions]
            order = [QueryOrderBy(field=f) for f in fields]

        def placed(o: QueryOrderBy) -> QueryOrderBy:
            if o.nulls is not None:
                return o
            last = descending if o.direction is SortDirection.DESC else ascending
            return o.model_copy(
                update={"nulls": NullsPosition.LAST if last else NullsPosition.FIRST}
            )

        self.query = self.query.model_copy(update={"order_by": [placed(o) for o in order]})

    def sort(self) -> list[QueryOrderBy]:
        """The sort the combine step applies, as one database's resolution has it.

        The query's; under a rollup or cube NULL first where it does not say,
        and by the dimensions where it names no sort, so the totals lead.
        """
        order = self.query.order_by
        if self.query.grouping is None:
            return order
        if not order:
            order = [QueryOrderBy(field=d.name) for d in self.resolved.dimensions]
        first = {"nulls": NullsPosition.FIRST}
        return [o if o.nulls else o.model_copy(update=first) for o in order]

    def shown(self) -> set[str]:
        """The names of the columns the query returns, which a sort field names
        before a position (``resolve_order_by_field``): its dimensions' columns
        and measures, or in raw mode its fields."""
        select = self.query.select
        if select.is_raw:
            return {f.alias for f in self.resolved.fields}
        return {
            *(d.name for d in self.resolved.dimensions),
            *(d.alias for d in select.dimensions if isinstance(d, CoalesceDimension)),
            *select.measures,
        }

    def ranked_sorts(self, islands: list[set[str]], taken: set[str]) -> dict[str, tuple[int, str]]:
        """The strings sorted by the rank a leg gives them: by column, its island and rank.

        DuckDB sorts strings by their bytes, ``'C'`` before ``'b'``, where an
        engine sorts them by their collation: PostgreSQL's ``en_US.utf8`` puts
        ``'b'`` first, and ignores punctuation, MySQL's ``utf8mb4_0900_ai_ci``
        puts ``'é'`` before ``'f'``. A string column the query sorts by, a
        dimension or a raw-mode field, is ranked by the leg returning it,
        ``DENSE_RANK() OVER (ORDER BY ...)`` in its own engine, and the
        combine step sorts by that rank (:func:`_sorted_by_ranks`). A leg on
        DuckDB sorts as the combine step does, and a column of two legs (a
        coalesce dimension) or named by its position is sorted by its bytes.
        ``{}`` where the query is sorted by a measure it does not select, which
        the combine query sorts by itself.

        Each rank column takes a name none of *taken* (casefolded) is, and is
        added to it.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        strings: dict[str, str] = {}
        if self.query.select.is_raw:
            for f in self.resolved.fields:
                column = self.model.data_objects[f.object_name].columns.get(f.column_name)
                if column is not None and column.abstract_type is DataType.STRING:
                    strings[f.alias] = f.object_name
        else:
            for d in self.resolved.dimensions:
                if self.model.dimensions[d.dimension].result_type is DataType.STRING:
                    strings[d.name] = d.object_name
        order = self.sort()
        shown = self.shown()
        if any(o.field not in shown and not o.field.isdigit() for o in order):
            return {}
        ranked: dict[str, tuple[int, str]] = {}
        for o in order:
            home = island_of.get(strings.get(o.field, ""))
            if home is None or o.field in ranked:
                continue
            if self.leg_dialect(islands[home]).name == COMBINE_DIALECT:
                continue
            ranked[o.field] = (home, _unused(f"__or{len(ranked)}", taken))
        return ranked

    def leg_zone(self, island: set[str]) -> LegZone | None:
        """The zone the leg of *island* reads its timestamps without a zone in.

        One per data source, which looks its zone up once.
        """
        if self.zone is None or self.source_timezone is None:
            return None
        source = source_name(self.model, next(iter(sorted(island))))
        dialect = self.leg_dialect(island).name
        if (source, dialect) not in self.leg_zones:
            lookup = self.source_timezone
            self.leg_zones[source, dialect] = LegZone(self.zone, lambda: lookup(source, dialect))
        return self.leg_zones[source, dialect]

    def warn_unknown_zone(self, source: str | None, dialect: str) -> None:
        """Warn, once, that *source*'s timestamps are combined in a zone not known."""
        named = f"Data source '{source}'" if source is not None else f"The {dialect} data source"
        message = (
            f"{named} runs its sessions in a time zone not known here, so the timestamps "
            f"without a zone it returns are combined as it returns them, not converted to "
            f"{self.zone}"
        )
        if any(w.message == message for w in self.resolved.warnings):
            return
        self.resolved.warnings.append(
            warning(
                code=WarningCode.UNDECLARED_TIMESTAMP_ZONE,
                message=message,
                hint=(
                    "Set DB_<NAME>_TIMEZONE for the data source (DB_TIMEZONE for every "
                    "source) to the zone its timestamps are in."
                ),
                context={"dataSource": source, "dialect": dialect, "timezone": self.zone},
            )
        )

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
        predicates: dict[str, Expr] | None = None,
        raw: bool = False,
    ) -> FederatedLeg:
        """One leg; *rekeyed* and *kept* as :func:`_rekeyed` returns them, the
        measure filters it evaluates, *predicates* (:func:`_leg_predicates`),
        and *raw* for a leg of a query of fields."""
        source = source_name(self.model, next(iter(sorted(island))))
        dialect = self.leg_dialect(island)
        zone = self.leg_zone(island)
        with timestamps_in_zone(zone):
            sql = compile_leg(query, model, dialect.name)
        if zone is not None and zone.unknown:
            self.warn_unknown_zone(source, dialect.name)
        if rekeyed or predicates:
            sql = _rewrite_leg(dialect, sql, columns, rekeyed or {}, kept or {}, predicates)
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
            joins=tuple(
                dict.fromkeys(join for c in columns for join in self.key_joins.get(c.name, ()))
            ),
            raw=raw,
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


def _maps_key(
    model: SemanticModel, edge: _Edge, reads: set[tuple[str, str]], dialect: Dialect
) -> bool:
    """Whether *edge*'s key is one the fact leg may join a map of (:class:`KeyMap`).

    One column, an integer on both sides: an integer groups and matches alike
    on every engine, where a string compared in the fact engine's collation
    would match a map row of each key that collation calls equal to it. Of a
    type the fact leg's *dialect* joins a map's keys to (:meth:`Dialect.maps_key`).
    And one no other column of the fact leg reads (*reads*, ``(data object,
    column)`` pairs): that column would group the leg by the key anyway.
    """
    if _injectable_key_type(model, edge) != DataType.INT.value:
        return False
    key = model.data_objects[edge.from_object].columns[edge.from_columns[0]]
    if not dialect.maps_key(key.sql_type):
        return False
    return (edge.from_object, edge.from_columns[0]) not in reads


def _physical_tables(model: SemanticModel, objects: Iterable[str]) -> tuple[str, ...]:
    """The distinct ``DATABASE.SCHEMA.CODE`` tables *objects* read."""
    tables: dict[str, None] = {}
    for name in sorted(objects):
        obj = model.data_objects[model.unnest_root(name)]
        parts = [str(p) for p in (obj.database, obj.schema_name, obj.code) if p]
        if parts:
            tables[".".join(parts)] = None
    return tuple(tables)


def _paths_within(query: QueryObject, island: set[str]) -> list[UsePathName]:
    """The secondary join paths *query* picks between two objects of *island*.

    A path between two islands is the edge the combine step joins, which
    the resolver already took it for; one within an island is the leg's.
    """
    return [p for p in query.use_path_names if p.source in island and p.target in island]


def _reserved_names(model: SemanticModel) -> set[str]:
    """The names, casefolded, a leg column the model does not declare may not take.

    Every dimension, measure and metric, and every column: a key is a
    dimension of its leg, and a copy of its column under its own name
    (:meth:`_Planner.island_model`).
    """
    return {
        name.casefold() for name in (*model.effective_measures, *model.dimensions, *model.metrics)
    } | {c.casefold() for obj in model.data_objects.values() for c in obj.columns}


def _zone_pairs(
    columns: dict[str, tuple[str, str]], names: Iterable[str]
) -> frozenset[tuple[str, str]]:
    """*names*, leg columns of *columns*, as :func:`columns_in_own_zone` takes them.

    Each is a copy of its column on its data object, under its own name
    (:meth:`_Planner.island_model`).
    """
    return frozenset((columns[name][0], name) for name in names)


def _combine_settings(model: SemanticModel) -> ModelSettings:
    """What types and buckets the combined values.

    Not the query's time zone, which the legs read their timestamps in already.
    """
    return ModelSettings(default_dialect=COMBINE_DIALECT).model_copy(
        update=(
            {
                "week_start": model.settings.week_start,
                "default_numeric_data_type": model.settings.default_numeric_data_type,
            }
            if model.settings
            else {}
        )
    )


def _dimension_refs(query: QueryObject) -> list[str]:
    """The dimensions *query* selects as written, a coalesce group's members in its place."""
    refs: list[str] = []
    for d in query.select.dimensions:
        refs.extend(d.coalesce if isinstance(d, CoalesceDimension) else [d])
    return refs


def _adds_up(measure: Measure) -> bool:
    """Whether *measure* over a group's rows is that over its partial results' rows."""
    return measure.aggregation in _REAGGREGATE and not measure.distinct


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
    predicates: dict[str, Expr] | None = None,
) -> str:
    """*sql* wrapped so its *rekeyed* columns compare in DuckDB as in their engine.

    And with the columns of *predicates*, each evaluated there over the
    leg's own columns (:func:`_leg_predicates`).
    """
    projection: list[tuple[str, Expr]] = []
    for column in columns:
        if predicates and column.name in predicates:
            projection.append((column.name, predicates[column.name]))
            continue
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
    an input to the combined aggregate), no ``total``, grain or filter context.
    A sum is also left uncast while the fact leg compiles (``partial_sums``).
    """
    return measure.model_copy(
        update={
            "total": False,
            "grain": None,
            "filter_context": None,
            "default_value": None,
            "data_type": None,
        }
    )


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


def _ranks(model: SemanticModel, names: set[str], taken: set[str]) -> dict[str, str]:
    """The fact leg column ranking each minimum or maximum of strings of *names*.

    A minimum of minima is the minimum, as one database's, only where the
    combine step orders the values as their engine does, and DuckDB orders
    strings by their bytes, where an engine orders them by their collation:
    PostgreSQL's ``en_US.utf8`` puts ``'a'`` before ``'B'``, MySQL's
    ``utf8mb4_0900_ai_ci`` ``'é'`` before ``'f'``. The fact leg ranks each
    group's value among its groups', ``DENSE_RANK() OVER (ORDER BY ...)`` in
    its own engine, and the combine step takes the value at the least (the
    greatest) rank (:func:`_combine_measure`). Values their engine calls
    equal share a rank, and the combine step takes the bytewise least (the
    greatest) of them, which is one of the minima (the maxima) one database
    may answer.

    Each column takes a name none of *taken* (casefolded) is, and is added to
    it.
    """
    effective = model.effective_measures
    ranked = sorted(
        name
        for name in names
        if effective[name].aggregation in (AggregationType.MIN, AggregationType.MAX)
        and not effective[name].distinct
        and effective[name].result_type == DataType.STRING
    )
    return {name: _unused(f"__rank{n}", taken) for n, name in enumerate(ranked)}


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
    filtered: str | None = None,
    on_rows: dict[tuple[str, str, bool], str] | None = None,
) -> Measure:
    """The measure as the combine step computes it from the fact leg's rows.

    The measure itself, reading each column it reads from *view*'s column of
    it (:func:`_row_measures`): the fact leg's, or, for a measure counted
    once per row of its object, that object's rows once each. A column of
    another data source it reads from the dimension leg *elsewhere* names,
    which the combine step joins to the fact rows. It keeps its type,
    default, ``total``, grain and filter context. A count of rows counts
    *view*'s rows. Its filters, where the fact leg evaluates them, read the
    leg's column of their outcome, *filtered* (:func:`_leg_predicates`); a
    filter of a measure counted once per row of its object reads the fact
    rows' columns in *on_rows* from them (:meth:`_Planner.reads_fact_rows`).
    """
    measure = model.effective_measures[name]

    rows = on_rows or {}

    def at(ref: DataColumnRef, own_zone: bool = False) -> DataColumnRef:
        if not ref.column:
            return DataColumnRef(view=view)
        read = (ref.view or "", ref.column, own_zone)
        return DataColumnRef(view=elsewhere.get(read, rows.get(read, view)), column=columns[read])

    update: dict[str, Any] = {
        "columns": [at(ref) for ref in measure.columns],
        "filters": [_filter_at(item, at) for item in measure.filters],
        "anchor": None,
    }
    if filtered is not None:
        update["filters"] = [
            MeasureFilter(
                column=DataColumnRef(view=view, column=filtered),
                operator="equals",
                values=[FilterValue(data_type=DataType.INT, value_int=1)],
            )
        ]
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


def _condition_key(field: str, op: str, value: Any) -> tuple[str, str, str]:
    """A condition as compared: its field, operator and value."""
    leaf = QueryFilter(field=field, op=op, value=value)
    return (leaf.field, leaf.op.value, repr(leaf.value))


def _outcome_dimension(column: str, leg: str) -> Dimension:
    """The combine step's dimension of *leg*'s outcome *column* (:func:`_outcome`)."""
    return Dimension(name=column, view=leg, column=column, result_type=DataType.INT)


def _static_after_join(f: ModelFilter, leg: str, column: str, *, tested: bool) -> ModelFilter:
    """Static filter *f* as the combine step runs it, on *column* of *leg*: as
    declared, or the test of its outcome where the leg evaluated it."""
    update: dict[str, Any] = {"data_object": leg, "column": column}
    if tested:
        test, value = _test(f.operator)
        update |= {"operator": test.value, "value": value, "values": []}
    return f.model_copy(update=update)


def _outcome(condition: Expr, op: str) -> Expr:
    """The leg column of *condition*, with operator *op*, the combine step tests (:func:`_test`).

    ``1`` where it holds, ``0`` where it fails, NULL where it is unknown, as
    is the condition on the NULL of a row the join leaves unmatched. One that
    holds for NULL, or fails for it (:data:`_NULL_TESTS`), is never unknown:
    its column is ``1`` where it fails, or holds, and NULL elsewhere.
    """
    test = _NULL_TESTS.get(FilterOperator(op))
    if test is not None:
        flagged = (
            UnaryOp(op="NOT", operand=condition) if test == FilterOperator.IS_NULL else condition
        )
        return CaseExpr(when_clauses=[(flagged, Literal(value=1))])
    return CaseExpr(
        when_clauses=[
            (condition, Literal(value=1)),
            (UnaryOp(op="NOT", operand=condition), Literal(value=0)),
        ]
    )


def _test(op: str) -> tuple[FilterOperator, int | None]:
    """The combine step's test of a leg's outcome of a condition with operator
    *op* (:func:`_outcome`), and the value it tests for."""
    test = _NULL_TESTS.get(FilterOperator(op))
    return (FilterOperator.EQUALS, 1) if test is None else (test, None)


def _tested(item: QueryFilterItem, outcome: Callable[[QueryFilter], str | None]) -> QueryFilterItem:
    """*item* with each condition a leg evaluates testing the leg's column of it.

    *outcome* names that column, or ``None`` for a condition the combine step
    evaluates itself. The test (:func:`_test`) is unknown where the condition
    is, so a negated group stays unknown where one database's is.
    """
    if isinstance(item, QueryFilterGroup):
        return item.model_copy(update={"filters": [_tested(f, outcome) for f in item.filters]})
    column = outcome(item)
    if column is None:
        return item
    test, value = _test(item.op)
    return QueryFilter(field=column, op=test, value=value)


def _leg_predicates(
    model: SemanticModel,
    names: list[str],
    row_values: dict[str, tuple[str, str]],
    taken: set[str],
) -> tuple[dict[str, str], dict[str, Expr]]:
    """The fact leg's columns of the measure filters it evaluates (:meth:`_Planner.filters_in_leg`).

    Each measure's filters as one database writes them, over the leg's
    columns of what they read (*row_values*), in its engine: ``1`` where they
    hold, else ``0``, as a filter keeps no row it does not hold for. Returns
    the column of each measure in *names*, and the expression of each
    column, which takes a name none of *taken* (casefolded) is.
    """
    leg_of = {read: column for column, read in row_values.items()}
    outcome: dict[str, str] = {}
    predicates: dict[str, Expr] = {}
    for name in names:
        measure = model.effective_measures[name]
        condition = build_measure_filter_condition(measure.filters, model, [])
        assert condition is not None
        mapping: dict[tuple[str, str | None], ColumnRef] = {
            (model.data_objects[obj].columns[col].code, obj): ColumnRef(
                name=leg_of[(obj, col)], table=_LEG_ALIAS
            )
            for obj, col in _filter_columns(measure.filters)
        }
        column = _unused(f"__mf{len(outcome)}", taken)
        predicates[column] = CaseExpr(
            when_clauses=[(rewrite_column_refs(condition, mapping), Literal(value=1))],
            else_clause=Literal(value=0),
        )
        outcome[name] = column
    return outcome, predicates


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


def _compared_columns(measure: Measure, *, filters: bool = True) -> list[tuple[str, str, bool]]:
    """The columns whose values *measure* compares, each with whether it counts them.

    Its filters (unless *filters* is false: the fact leg evaluates them),
    its ordering and its expression compare what they read; its arguments
    are compared by an aggregation that orders them, or counts equal ones
    once (:data:`_ORDERING`, :data:`_COUNTING`, ``distinct``). Counting
    compares equality alone, which an engine may judge otherwise than order
    (:meth:`Dialect.equality_key`).
    """
    counts = measure.distinct or measure.aggregation in _COUNTING
    compared = (
        [(obj, column, False) for obj, column in _filter_columns(measure.filters)]
        if filters
        else []
    )
    ordering = measure.within_group.column if measure.within_group is not None else None
    if ordering is not None and ordering.column:
        compared.append((ordering.view or "", ordering.column, False))
    if measure.expression:
        compared += [(obj, col, counts) for obj, col in find_qualified_refs(measure.expression)]
    if counts or measure.aggregation in _ORDERING:
        compared += [(ref.view or "", ref.column, counts) for ref in measure.columns if ref.column]
    return compared


def _unnest_place(obj: str, origin: str) -> str:
    """How a dimension or column reads *obj*, through the unnest of *origin*."""
    if obj == origin:
        return f"unnests '{obj}'"
    return f"reads '{obj}' through the unnest of '{origin}'"


def _repeats_rows(step: JoinStep) -> bool:
    """Whether *step* can repeat the rows it joins from: walked from the one side
    of a many-to-one join, or many-to-many. A one-to-one join does not."""
    if step.cardinality == Cardinality.ONE_TO_ONE:
        return False
    return step.reversed or step.cardinality != Cardinality.MANY_TO_ONE


def _filter_leaves(items: Iterable[MeasureFilterItem]) -> list[MeasureFilter]:
    """The conditions of measure filters *items*, those in groups too."""
    leaves: list[MeasureFilter] = []
    for item in items:
        if isinstance(item, MeasureFilterGroup):
            leaves += _filter_leaves(item.filters)
        else:
            leaves.append(item)
    return leaves


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
    return _ahead(sql, relations)


def _reached_rows(
    sql: str, dialect: Dialect, fact: str, reached: dict[str, tuple[str, list[str]]]
) -> str:
    """*sql* reading each relation of *reached*: its leg's rows the *fact* leg's keys reach.

    By relation, the dimension leg and the key columns both legs return.
    Common table expressions ahead of any the combine query has, as in
    :func:`_with_rows`.
    """
    q = dialect.quote_identifier
    alias = q(_LEG_ALIAS)
    relations = [
        f"{q(name)} AS (\nSELECT {alias}.*\nFROM {q(leg)} AS {alias}\nWHERE EXISTS (\n"
        f"SELECT 1\nFROM {q(fact)}\nWHERE "
        + " AND ".join(f"{q(fact)}.{q(key)} = {alias}.{q(key)}" for key in keys)
        + "\n)\n)"
        for name, (leg, keys) in reached.items()
    ]
    return _ahead(sql, relations)


def _ahead(sql: str, relations: list[str]) -> str:
    """*sql* with the common table expressions *relations* ahead of any it has."""
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
    model: SemanticModel,
    name: str,
    column: str | None = None,
    view: str = "leg_0",
    rank: str | None = None,
) -> Measure:
    """The measure re-aggregated from the fact leg's partial results.

    It carries the measure's type, default, ``total``, grain and filter
    context. A count is combined as a ``SUM`` of counts, which over no rows is
    NULL where a count is 0: it falls back to 0, whatever default it declares,
    since a SQL ``COUNT`` never applies one. An ``avg`` is the ``SUM`` of its
    leg *column* of sums, which the combine compile divides by its counts; a
    variance keeps its aggregation over its leg *column* of sums, which the
    combine compile reads with its counts and squares; and a count of
    distinct values counts its leg *column* of values (:func:`_leg_measures`),
    from *view* where the fact leg returns rows (:func:`_row_measures`). A
    minimum or maximum of strings is the value at the least or greatest of
    its leg column of *rank*s, a ``first`` or ``last`` (:func:`_ranks`).
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
    elif rank is not None:
        last = measure.aggregation == AggregationType.MAX
        aggregation = AggregationType.LAST if last else AggregationType.FIRST
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
        within_group=(
            WithinGroup(column=DataColumnRef(view=view, column=rank)) if rank is not None else None
        ),
        result_type=measure.result_type,
        data_type=measure.data_type
        or (resolved.render() if isinstance(resolved, DecimalType) else None),
        total=measure.total,
        grain=measure.grain,
        filter_context=measure.filter_context,
        default_value=default,
        format=measure.format,
    )


def _combine_context(
    measure: Measure,
    renamed: dict[str, str],
    static: dict[str, list[str]],
    tested: dict[tuple[str, str, str], str],
) -> Measure:
    """*measure* with its filter context naming the fields the combine step filters.

    The combine query reads some fields under a name of their own (*renamed*),
    and a static filter's column, or the outcome of a condition its leg
    evaluates, under a leg column's (*static*, by the column or field): the
    context leaves out, or keeps only, the filters on those as well, as one
    database's matches them by the field or column they read. Its included
    filters read the renamed fields, or test the outcome a leg returns of
    them (*tested*, by :func:`_condition_key`).
    """
    context = measure.filter_context
    if context is None:
        return measure

    def names(fields: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                name
                for field in fields
                for name in (field, renamed.get(field, field), *static.get(field, []))
            )
        )

    def included(item: FilterContextFilter) -> FilterContextFilter:
        column = tested.get(_condition_key(item.field, item.op, item.value))
        if column is None:
            return item.model_copy(update={"field": renamed.get(item.field, item.field)})
        test, value = _test(item.op)
        return FilterContextFilter(field=column, op=test.value, value=value)

    update = {
        "exclude": names(context.exclude),
        "keep_only": names(context.keep_only),
        "include": [included(item) for item in context.include],
    }
    return measure.model_copy(update={"filter_context": context.model_copy(update=update)})


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
