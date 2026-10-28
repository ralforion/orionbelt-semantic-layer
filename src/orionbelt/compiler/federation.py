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
  Derived metrics, filters on other islands, HAVING, ORDER BY and LIMIT run
  there, over the combined rows.

Only what splits exactly is planned. Everything else is refused with a named
``FEDERATION_*`` error rather than answered with a different number; see
``design/PLAN_federation.md`` §5.5 for which shapes come later.

This module plans and compiles; ``service/federated_executor.py`` runs the
plan.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from orionbelt.compiler.resolution import ResolutionError, ResolvedQuery
from orionbelt.compiler.type_resolver import (
    combined_averages,
    partial_sums,
    resolve_measure_cast_type,
)
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import (
    DimensionRef,
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
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
    Measure,
    MetricType,
    ModelFilter,
    ModelSettings,
    SemanticModel,
)
from orionbelt.models.types import DecimalType

COMBINE_DIALECT = "duckdb"

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
    """

    name: str
    data_source: str | None
    dialect: str
    sql: str
    data_objects: tuple[str, ...]
    columns: tuple[tuple[str, str], ...] = ()
    physical_tables: tuple[str, ...] = ()
    exact_sums: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class FederatedPlan:
    """The legs of a federated query and the DuckDB query combining them."""

    legs: tuple[FederatedLeg, ...]
    combine_sql: str
    combine_dialect: str = COMBINE_DIALECT


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


def query_objects(resolved: ResolvedQuery, query: QueryObject) -> set[str]:
    """Every data object a resolved query reads, ``exists`` subqueries included."""
    objects = set(resolved.required_objects) | {resolved.base_object}
    for step in resolved.join_steps:
        objects |= {step.from_object, step.to_object}
    objects |= {d.object_name for d in resolved.dimensions}
    objects |= set(resolved.measure_source_objects)
    for item in [*query.where, *query.having]:
        objects |= _subquery_objects(item)
    objects.discard("")
    return objects


def is_federated(resolved: ResolvedQuery, query: QueryObject, model: SemanticModel) -> bool:
    """Whether a query reads data objects of more than one data source."""
    sources = object_sources(model)
    return len({sources.get(o) for o in query_objects(resolved, query) if o in sources}) > 1


def query_source(resolved: ResolvedQuery, query: QueryObject, model: SemanticModel) -> str | None:
    """The data source a query on one source reads, for a model read from several.

    As the model writes it; ``None`` when the model reads one source, or when
    the query reads the objects that name none.
    """
    sources = object_sources(model)
    if len(set(sources.values())) < 2:
        return None
    read = sorted(o for o in query_objects(resolved, query) if o in sources)
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
    planner = _Planner(query, model, resolved, dialect_name)
    leg_limit = leg_row_limit + 1 if leg_row_limit is not None else None
    planner.check_query_shape()
    islands = planner.islands()
    fact = planner.fact_island(islands)
    edges = planner.federation_edges(islands, fact)
    planner.check_measures(islands[fact])
    leg_filters, combine_filters = planner.place_filters(islands, fact)

    fact_dims, edge_dims = planner.dimensions_by_island(islands, fact, edges, combine_filters)
    legs: list[FederatedLeg] = []
    combine_objects: dict[str, DataObject] = {}
    combine_dimensions: dict[str, Dimension] = {}
    combine_filters_static: list[ModelFilter] = []

    # The fact leg: the island's dimensions, the edge keys and the measures.
    fact_keys = {
        f"__fk{e}_{i}": (edge.from_object, column)
        for e, edge in enumerate(edges)
        for i, column in enumerate(edge.from_columns)
    }
    measures = planner.atomic_measures()
    leg_measures, averages, distinct = _leg_measures(model, measures)
    # The values a count of distinct values counts: the fact leg groups by them
    # too, so the combine step counts them across every group they reach.
    fact_values = {column: (obj, col) for column, obj, col in distinct.values()}
    fact_model = planner.island_model(islands[fact], {**fact_keys, **fact_values}, leg_measures)
    fact_query = QueryObject(
        select=QuerySelect(
            dimensions=[*fact_dims, *fact_keys, *fact_values], measures=sorted(leg_measures)
        ),
        where=leg_filters,
        limit=leg_limit,
    )
    fact_columns = [
        *[_dimension_column(model, DimensionRef.parse(d).name) for d in fact_dims],
        *[
            _key_column(model, name, obj, col)
            for name, (obj, col) in {**fact_keys, **fact_values}.items()
        ],
        *[
            DataObjectColumn(name=n, code=n, abstract_type=m.result_type)
            for n, m in sorted(leg_measures.items())
        ],
    ]
    with partial_sums():
        legs.append(
            planner.leg(
                "leg_0",
                islands[fact],
                fact_query,
                fact_model,
                fact_columns,
                compile_leg,
                source_dialect,
                exact_sums=tuple(
                    (sums, scale) for sums, _, scale in averages.values() if scale is not None
                ),
            )
        )
    fact_joins: list[DataObjectJoin] = []
    for d in fact_dims:
        name = DimensionRef.parse(d).name
        combine_dimensions[name] = _combine_dimension(model, name, "leg_0")

    # One leg per dimension island: its keys and the dimensions read there.
    for e, edge in enumerate(edges, start=1):
        leg_name = f"leg_{e}"
        keys = {
            f"__fk{e - 1}_{i}": (edge.to_object, column) for i, column in enumerate(edge.to_columns)
        }
        dims = edge_dims[e - 1]
        island = islands[edge.island]
        # A static model filter on this island runs in the combine step, on
        # the column this leg returns for it, as it runs on a left-joined
        # object in one database: there it drops the fact rows whose object
        # fails it, and keeps an unmatched one when the predicate holds for
        # NULL. Applied in the leg instead, a fact row whose object it
        # removed would come back NULL-joined, and pass ``IS NULL``.
        static = {
            f"__sf{e - 1}_{n}": (f.data_object, f.column, f)
            for n, f in enumerate(model.filters)
            if f.data_object in island
        }
        static_columns = {name: (obj, col) for name, (obj, col, _) in static.items()}
        dim_model = planner.island_model(
            island, {**keys, **static_columns}, {}, static_filters=False
        )
        dim_query = QueryObject(
            select=QuerySelect(dimensions=[*dims, *keys, *static_columns]), limit=leg_limit
        )
        dim_columns = [
            *[_dimension_column(model, DimensionRef.parse(d).name) for d in dims],
            *[
                _key_column(model, name, obj, col)
                for name, (obj, col) in {**keys, **static_columns}.items()
            ],
        ]
        legs.append(
            planner.leg(
                leg_name, island, dim_query, dim_model, dim_columns, compile_leg, source_dialect
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
        for d in dims:
            name = DimensionRef.parse(d).name
            combine_dimensions[name] = _combine_dimension(model, name, leg_name)
        fact_joins.append(
            DataObjectJoin(
                join_type=Cardinality.MANY_TO_ONE,
                join_to=leg_name,
                columns_from=list(keys),
                columns_to=list(keys),
                required=edge.required,
            )
        )

    combine_objects["leg_0"] = DataObject(
        name="leg_0",
        code="leg_0",
        database="",
        schema_name="",
        columns={c.name: c for c in fact_columns},
        joins=fact_joins,
        countable=False,
    )
    combine_model = SemanticModel(
        name=model.name,
        settings=ModelSettings(default_dialect=COMBINE_DIALECT).model_copy(
            update={"week_start": model.settings.week_start} if model.settings else {}
        ),
        data_objects=combine_objects,
        dimensions=combine_dimensions,
        measures={
            m: _combine_measure(
                model,
                m,
                averages[m][0] if m in averages else distinct[m][0] if m in distinct else None,
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
            "where": combine_filters,
            "order_by": [
                QueryOrderBy(
                    field=DimensionRef.parse(o.field).name, direction=o.direction, nulls=o.nulls
                )
                for o in query.order_by
            ],
            "use_path_names": [],
        }
    )
    with combined_averages(
        DialectRegistry.get(COMBINE_DIALECT),
        {m: (counts, scale) for m, (_, counts, scale) in averages.items()},
    ):
        combine_sql = compile_leg(combine_query, combine_model, COMBINE_DIALECT)
    return FederatedPlan(legs=tuple(legs), combine_sql=combine_sql)


@dataclass(frozen=True)
class _Edge:
    """A many-to-one join from the fact island into a dimension island."""

    from_object: str
    to_object: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    island: int
    required: bool


class _Planner:
    def __init__(
        self,
        query: QueryObject,
        model: SemanticModel,
        resolved: ResolvedQuery,
        dialect_name: str,
    ) -> None:
        self.query = query
        self.dialect_name = dialect_name
        self.model = model
        self.resolved = resolved
        self.sources = object_sources(model)
        self.objects = query_objects(resolved, query)

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
        if not q.select.measures:
            raise self.refuse(
                "NO_MEASURES",
                "A query with no measures across data sources is not supported yet",
                hint="Select a measure, or query the dimensions of one data source.",
            )
        names = [DimensionRef.parse(d).name for d in q.select.dimensions if isinstance(d, str)]
        if len(names) != len(set(names)):
            raise self.refuse(
                "UNSUPPORTED_QUERY",
                "A federated query cannot select one dimension at two grains",
            )
        for item in [*q.where, *q.having]:
            if _subquery_objects(item):
                raise self.refuse("UNSUPPORTED_QUERY", "A federated query cannot use exists")
        for name in names:
            if self.model.dimensions[name].via:
                raise self.refuse(
                    "UNSUPPORTED_QUERY",
                    f"Dimension '{name}' plays a role (via), which a federated query "
                    "does not support yet",
                )

    # -- islands and edges --------------------------------------------------

    def islands(self) -> list[set[str]]:
        """The query's objects grouped by source and joined within it."""
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
        groups: dict[str, set[str]] = {}
        for o in sorted(self.objects):
            groups.setdefault(find(o), set()).add(o)
        return list(groups.values())

    def fact_island(self, islands: list[set[str]]) -> int:
        """The island of the measures, which must also hold the base object."""
        measure_objects = set(self.resolved.measure_source_objects)
        owners = {i for i, island in enumerate(islands) if island & measure_objects}
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
        base = next(i for i, island in enumerate(islands) if self.resolved.base_object in island)
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
        names = {m.name for m in self.resolved.measures if not m.is_expression}
        names |= set(self.resolved.metric_components)
        effective = self.model.effective_measures
        for item in self.query.having:
            names |= {f for f in _filter_fields(item) if f in effective}
        names |= {o.field for o in self.query.order_by if o.field in effective}
        return names

    def check_measures(self, fact_island: set[str]) -> None:
        for rm in self.resolved.measures:
            if rm.is_cumulative or rm.is_pop or rm.is_window or rm.is_reaggregate:
                raise self.refuse(
                    "UNSUPPORTED_METRIC",
                    f"Metric '{rm.name}' is not a derived metric, which is all a "
                    "federated query supports yet",
                )
        effective = self.model.effective_measures
        for name in sorted(self.atomic_measures()):
            measure = effective[name]
            counts_distinct = _counts_distinct(measure)
            combinable = counts_distinct or (
                not measure.distinct
                and (
                    measure.aggregation in _REAGGREGATE
                    or measure.aggregation == AggregationType.AVG
                )
            )
            if not combinable:
                aggregation = (
                    f"{measure.aggregation} distinct" if measure.distinct else measure.aggregation
                )
                raise self.refuse(
                    "UNSUPPORTED_AGGREGATION",
                    f"Measure '{name}' aggregates with {aggregation}, which cannot be "
                    "combined from per-data-source partial results",
                    hint=(
                        "sum, count, count_distinct, avg, min, max and any_value combine "
                        "across data sources."
                    ),
                )
            if measure.total and (counts_distinct or measure.aggregation == AggregationType.AVG):
                raise self.refuse(
                    "UNSUPPORTED_AGGREGATION",
                    f"Measure '{name}' aggregates with {measure.aggregation} and total: true, "
                    "which a federated query does not combine yet",
                )
            if counts_distinct and (
                measure.expression
                or measure.filters
                or len(measure.columns) != 1
                or not measure.columns[0].column
            ):
                raise self.refuse(
                    "UNSUPPORTED_AGGREGATION",
                    f"Measure '{name}' counts the distinct values of an expression, of "
                    "several columns or under filters, which a federated query does not "
                    "combine yet",
                    hint="A count of the distinct values of one column combines.",
                )
            if measure.filter_context is not None or measure.grain is not None:
                raise self.refuse(
                    "UNSUPPORTED_AGGREGATION",
                    f"Measure '{name}' sets filterContext or grain, which a federated "
                    "query does not support yet",
                )
            outside = self.model.measure_join_objects(name) - fact_island
            if outside:
                raise self.refuse(
                    "MEASURE_ACROSS_SOURCES",
                    f"Measure '{name}' reads {', '.join(sorted(outside))}, of another "
                    "data source than its own",
                )

    # -- filters ------------------------------------------------------------

    def place_filters(
        self, islands: list[set[str]], fact: int
    ) -> tuple[list[QueryFilterItem], list[QueryFilterItem]]:
        """Split ``where`` into what the fact leg runs and what the combine step runs.

        A condition on the fact island's dimensions alone runs in the fact
        leg. Any other runs only in the combine step, over the joined rows:
        pushed into a dimension leg too, a predicate true for NULL (``IS
        NULL``) would also match the fact rows whose key that leg dropped.
        """
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        leg: list[QueryFilterItem] = []
        combine: list[QueryFilterItem] = []
        for item in self.query.where:
            fields = _filter_fields(item)
            owners = set()
            for field in fields:
                name = DimensionRef.parse(field).name
                dimension = self.model.dimensions.get(name)
                if dimension is None:
                    raise self.refuse(
                        "UNSUPPORTED_FILTER",
                        f"The filter on '{field}' is not on a dimension",
                    )
                owners.add(island_of.get(dimension.view))
            if owners == {fact}:
                leg.append(item)
                continue
            if _uses_relative(item):
                raise self.refuse(
                    "UNSUPPORTED_FILTER",
                    "A relative date filter on another data source is not supported yet",
                )
            if any(":" in f for f in fields):
                raise self.refuse(
                    "UNSUPPORTED_FILTER",
                    "A filter at a time grain on another data source is not supported yet",
                )
            combine.append(item)
        return leg, combine

    def dimensions_by_island(
        self,
        islands: list[set[str]],
        fact: int,
        edges: list[_Edge],
        combine_filters: list[QueryFilterItem],
    ) -> tuple[list[str], list[list[str]]]:
        """The dimensions each leg returns: selected, or read by a combine filter."""
        island_of = {o: i for i, island in enumerate(islands) for o in island}
        wanted = [d for d in self.query.select.dimensions if isinstance(d, str)]
        selected = {DimensionRef.parse(d).name for d in wanted}
        at_grain = {DimensionRef.parse(d).name for d in wanted if DimensionRef.parse(d).grain}
        for item in combine_filters:
            for field in _filter_fields(item):
                if field in at_grain:
                    raise self.refuse(
                        "UNSUPPORTED_FILTER",
                        f"The filter on '{field}' runs after the join, where '{field}' is "
                        "only available at the grain the query selects",
                        hint="Filter it in a condition of its own data source alone, or "
                        "select it without a grain.",
                    )
                if field not in selected:
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
        return fact_dims, edge_dims

    # -- legs ---------------------------------------------------------------

    def island_model(
        self,
        island: set[str],
        keys: dict[str, tuple[str, str]],
        measures: dict[str, Measure],
        *,
        static_filters: bool = True,
    ) -> SemanticModel:
        """The model a leg compiles against: the island alone, keys as dimensions.

        *measures* are partial results (:func:`_leg_measures`): no ``total``,
        no ``defaultValue`` (an empty group's default would become an input to
        the combined aggregate), and a sum at a width that does not round
        before the partials are added (``partial_sums``).
        """
        model = self.model
        objects = {
            name: obj.model_copy(update={"joins": [j for j in obj.joins if j.join_to in island]})
            for name, obj in model.data_objects.items()
            if name in island
        }
        dimensions = {name: dim for name, dim in model.dimensions.items() if dim.view in island}
        for key, (obj, column) in keys.items():
            col = model.data_objects[obj].columns[column]
            dimensions[key] = Dimension(
                name=key, view=obj, column=column, result_type=col.abstract_type
            )
        return model.model_copy(
            update={
                "data_objects": objects,
                "dimensions": dimensions,
                "measures": measures,
                "metrics": {},
                "filters": (
                    [f for f in model.filters if f.data_object in island] if static_filters else []
                ),
                "rules": {},
                "expose_counts": False,
            }
        )

    def leg(
        self,
        name: str,
        island: set[str],
        query: QueryObject,
        model: SemanticModel,
        columns: list[DataObjectColumn],
        compile_leg: Callable[[QueryObject, SemanticModel, str], str],
        source_dialect: Callable[[str | None], str | None],
        *,
        exact_sums: tuple[tuple[str, int], ...] = (),
    ) -> FederatedLeg:
        source = source_name(self.model, next(iter(sorted(island))))
        dialect = source_dialect(source) or self.dialect_name
        return FederatedLeg(
            name=name,
            data_source=source,
            dialect=dialect,
            sql=compile_leg(query, model, dialect),
            data_objects=tuple(sorted(island)),
            columns=tuple((c.name, c.abstract_type.value) for c in columns),
            physical_tables=_physical_tables(self.model, island),
            exact_sums=exact_sums,
        )


def _physical_tables(model: SemanticModel, objects: Iterable[str]) -> tuple[str, ...]:
    """The distinct ``DATABASE.SCHEMA.CODE`` tables *objects* read."""
    tables: dict[str, None] = {}
    for name in sorted(objects):
        obj = model.data_objects[model.unnest_root(name)]
        parts = [str(p) for p in (obj.database, obj.schema_name, obj.code) if p]
        if parts:
            tables[".".join(parts)] = None
    return tuple(tables)


def _dimension_column(model: SemanticModel, name: str) -> DataObjectColumn:
    return DataObjectColumn(name=name, code=name, abstract_type=model.dimensions[name].result_type)


def _key_column(model: SemanticModel, name: str, obj: str, column: str) -> DataObjectColumn:
    col = model.data_objects[obj].columns[column]
    return DataObjectColumn(name=name, code=name, abstract_type=col.abstract_type)


def _combine_dimension(model: SemanticModel, name: str, leg: str) -> Dimension:
    dimension = model.dimensions[name]
    return Dimension(
        name=name,
        view=leg,
        column=name,
        result_type=dimension.result_type,
        format=dimension.format,
    )


def _partial_measure(measure: Measure) -> Measure:
    """The measure as a leg computes it: a partial result to combine later.

    The output type and the default are the combined measure's to apply once:
    no ``dataType``, no ``defaultValue`` (an empty group's default would become
    an input to the combined aggregate), no ``total``. A sum is also left
    uncast while the fact leg compiles (``partial_sums``).
    """
    return measure.model_copy(update={"total": False, "default_value": None, "data_type": None})


def _leg_measures(
    model: SemanticModel, names: set[str]
) -> tuple[
    dict[str, Measure],
    dict[str, tuple[str, str, int | None]],
    dict[str, tuple[str, str, str]],
]:
    """The fact leg's partial measures for *names*, and how the others split.

    A measure is computed as itself (:func:`_partial_measure`), except:

    * an ``avg``: an average of per-group averages is not the average, so the
      leg returns its argument's ``SUM`` and ``COUNT`` instead, under the same
      filters, and the combine step divides their totals
      (:func:`~orionbelt.compiler.type_resolver.combined_averages`). Both skip
      NULLs, so they cover exactly the rows ``AVG`` would.
    * a count of distinct values: counts of distinct values do not add up, so
      the leg returns no measure for it but groups by the column it counts,
      and the combine step counts that column's distinct values across all
      the groups they reach. The other measures are still exact at that finer
      grain, since each combines from partial results.

    Returns the leg's measures by name; per ``avg`` the leg columns of its sums
    and its counts, with the scale the sums are read at (0 for an integer
    measure, :data:`_AVERAGE_SCALE` for a decimal one, ``None`` for a
    ``float`` one, whose sums are doubles and divide in floating point); and
    per count of distinct values the leg column of its values, with the data
    object and column they are read from.
    """
    effective = model.effective_measures
    # A generated name must not be one the model declares: a declared measure
    # named like a partial would replace it in the leg. Compared without case,
    # as DuckDB compares identifiers, quoted or not.
    taken = {name.casefold() for name in (*effective, *model.dimensions)}
    measures: dict[str, Measure] = {}
    averages: dict[str, tuple[str, str, int | None]] = {}
    distinct: dict[str, tuple[str, str, str]] = {}
    for name in sorted(names):
        measure = _partial_measure(effective[name])
        if _counts_distinct(measure):
            ref = measure.columns[0]
            column = _unused(f"__distinct{len(distinct)}", taken)
            distinct[name] = (column, ref.view or "", ref.column or "")
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
    return measures, averages, distinct


def _counts_distinct(measure: Measure) -> bool:
    """Whether *measure* counts distinct values: ``count_distinct``, or a distinct ``count``."""
    return measure.aggregation == AggregationType.COUNT_DISTINCT or (
        measure.aggregation == AggregationType.COUNT and measure.distinct
    )


def _unused(name: str, taken: set[str]) -> str:
    """*name*, prefixed with underscores until it is none of *taken*, which are casefolded."""
    while name.casefold() in taken:
        name = f"_{name}"
    taken.add(name.casefold())
    return name


def _combine_measure(model: SemanticModel, name: str, column: str | None = None) -> Measure:
    """The measure re-aggregated from the fact leg's partial results.

    It carries the measure's type, default and ``total``. A count is combined
    as a ``SUM`` of counts, which over no rows is NULL where a count is 0: it
    falls back to 0, whatever default it declares, since a SQL ``COUNT`` never
    applies one. An ``avg`` is the ``SUM`` of its leg *column* of sums, which
    the combine compile divides by its counts, and a count of distinct values
    counts its leg *column* of values (:func:`_leg_measures`).
    """
    measure = model.effective_measures[name]
    counts_distinct = _counts_distinct(measure)
    default = measure.default_value
    if measure.aggregation in (AggregationType.COUNT, AggregationType.COUNT_DISTINCT):
        default = 0
    if counts_distinct:
        aggregation = AggregationType.COUNT_DISTINCT
    elif column is not None:
        aggregation = AggregationType.SUM
    else:
        aggregation = _REAGGREGATE[measure.aggregation]
    averages = column is not None and not counts_distinct
    # The type the one database's query casts to, widened for wide sources. An
    # average is typed for the dialect that divides it: an integer one takes
    # the widened type every engine's exact integer AVG casts to (#316), where
    # typed without a dialect it would take the narrow default, too small for
    # an average of 64-bit integers.
    dialect = DialectRegistry.get(COMBINE_DIALECT) if averages else None
    resolved = resolve_measure_cast_type(measure, model.settings, dialect, model)
    return Measure(
        name=name,
        columns=[DataColumnRef(view="leg_0", column=column or name)],
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


def _uses_relative(item: QueryFilterItem) -> bool:
    if isinstance(item, QueryFilterGroup):
        return any(_uses_relative(sub) for sub in item.filters)
    return item.op == FilterOperator.RELATIVE


def _subquery_objects(item: QueryFilterItem) -> set[str]:
    if isinstance(item, QueryFilterGroup):
        return {o for sub in item.filters for o in _subquery_objects(sub)}
    if isinstance(item, QueryFilter) and item.subquery is not None:
        return {item.subquery.data_object}
    return set()
