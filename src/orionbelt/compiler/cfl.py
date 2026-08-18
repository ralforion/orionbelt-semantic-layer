"""CFL (Composite Fact Layer) planner: conformed dimensions + fact stitching."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    Literal,
    OrderByItem,
    Select,
    UnionAll,
)
from orionbelt.compiler import cfl_exclude, cfl_projection
from orionbelt.compiler.anchored import (
    ConformedFact,
    conformed_join_type,
    plan_conformed_facts,
)
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.grain_dedup import detect_dedup_measures
from orionbelt.compiler.graph import JoinGraph, JoinStep
from orionbelt.compiler.resolution import (
    ResolutionError,
    ResolvedDimension,
    ResolvedMeasure,
    ResolvedQuery,
    make_column_expr,
)
from orionbelt.compiler.star import CflLegInfo, QueryPlan, _grouping_flag_alias, _nulls_last
from orionbelt.compiler.type_resolver import (
    cast_measure_to_resolved_type,
    resolve_metric_data_type,
)
from orionbelt.dialect.base import Dialect, UnsupportedAggregationError
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import (
    DataObject,
    SemanticModel,
)


class WithinGroupNotSupportedInCFLError(UnsupportedAggregationError):
    """Raised when an ordered aggregate's sort key is out of its leg's reach.

    A ``withinGroup`` clause is the aggregate's ``ORDER BY``, and the CFL outer
    query re-aggregates over ``composite_01``. The ordering survives because the
    leg owning the measure projects the sort column as a column of its own
    (:func:`cfl_projection.composite_aliases`), sibling legs NULL-pad it,
    and the rebuilt aggregate orders by that alias.

    That only works where the sort column's object is reachable from the leg's
    own fact. Where it is not - typically a sort key sitting on a *different*
    fact - the leg has nothing to project, and the aggregate would come back in
    an arbitrary order: values right, sequence wrong, which is worse than a
    failure for a measure whose whole point is its sequence. Those refuse.
    """

    def __init__(self, measure_name: str, object_name: str) -> None:
        self.measure_name = measure_name
        self.dialect = "cfl"
        self.aggregation = "listagg"
        Exception.__init__(
            self,
            f"Measure '{measure_name}' is ordered by a column on '{object_name}' "
            f"(withinGroup), but this query combines measures from more than one "
            f"fact table and '{object_name}' is not reachable from the fact that "
            f"owns '{measure_name}', so the multi-fact plan cannot carry the "
            f"ordering column. The result would come back in an arbitrary order. "
            f"Order '{measure_name}' by a column its own fact can reach, or query "
            f"it on its own.",
        )


class UnsupportedAggregationForCFLError(UnsupportedAggregationError):
    """Raised when a multi-column aggregate straddles independent facts.

    Some aggregates read several columns *of one row*: ``corr`` / ``covar_*`` /
    ``regr_*`` correlate a pair, and a multi-column ``count_distinct`` counts
    observed tuples. In a multi-fact plan that is fine as long as one leg owns
    every argument: it projects them as separate columns and the outer query
    rebuilds the aggregate over them.

    It is not fine when the arguments sit on data objects no single leg reaches.
    UNION ALL stacks the facts rather than joining them, so each leg supplies one
    column and NULL-pads the rest, and no row ever carries a complete set. The
    statistics come back NULL having seen no pairs; the tuple count concatenates
    a NULL into every row and returns 0. No outer expression can recover the
    pairing the union destroyed, and a confidently wrong 0 is worse than an
    error, so this refuses.

    Inherits ``UnsupportedAggregationError`` so existing router catch
    sites surface the same 422 response shape. The ``dialect`` slot
    carries the planner identifier (``"cfl"``) rather than a SQL
    dialect — kept for response compatibility.
    """

    def __init__(self, measure_name: str, aggregation: str) -> None:
        self.measure_name = measure_name
        # Skip parent ``__init__`` (which formats a dialect-flavored
        # message) and set the same fields directly with our CFL-specific
        # message so routers and tests still get the structured
        # ``.dialect`` / ``.aggregation`` attributes.
        self.dialect = "cfl"
        self.aggregation = aggregation
        Exception.__init__(
            self,
            f"Measure '{measure_name}' aggregates columns that must be read from "
            f"the same row ({aggregation.upper()}). They sit on data objects that "
            "no single fact table can reach together, and this query stacks "
            "independent facts, so each row can only ever carry one of the "
            "values. Point every argument at columns one fact table can reach, "
            "or combine the facts with a metric over per-fact measures instead "
            "of pairing their rows.",
        )


__all__ = [
    "CFLPlanner",
    "FanoutError",
    "UnsupportedAggregationForCFLError",
    "WithinGroupNotSupportedInCFLError",
]


def _expand_cfl_measure_refs(expr: Expr, measure_exprs: dict[str, Expr]) -> Expr:
    """Replace bare ColumnRef aliases in HAVING with their full aggregate expressions.

    Thin delegator to :func:`cfl_projection.expand_cfl_measure_refs`.
    """
    return cfl_projection.expand_cfl_measure_refs(expr, measure_exprs)


class CFLPlanner:
    """Plans Composite Fact Layer queries: conformed dimensions + fact stitching.

    Uses a UNION ALL strategy:
    1. Each fact leg SELECTs conformed dimensions + its own measures (NULL for others)
    2. UNION ALL combines the legs into a single CTE
    3. Outer query aggregates over the union, grouping by conformed dimensions
    """

    def plan(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
        union_by_name: bool = False,
        dialect: Dialect | None = None,
    ) -> QueryPlan:
        """Plan a CFL query."""
        self._validate_fanout(resolved, model)

        # dimensionsExclude: EXCEPT-based anti-join pattern
        if resolved.dimensions_exclude:
            return self._plan_dimensions_exclude(resolved, model, qualify_table)

        # Group measures by their source object
        measures_by_object, cross_fact = self._group_measures_by_object(resolved, model)

        # Dimension-only CFL: no measures but dimensions on independent branches.
        # Create leg groupings from connecting fact tables.
        if not measures_by_object and not cross_fact and resolved.requires_cfl:
            measures_by_object = self._group_dimensions_into_legs(resolved, model)

        if len(measures_by_object) <= 1 and not cross_fact:
            # Single fact — delegate to star schema. Resolution let this query
            # past the reachability check because it looked multi-fact, and a
            # star has to serve every dimension from one root, so check now
            # that the leg left standing can.
            self._reject_unreachable_dimensions(measures_by_object, resolved, model)
            from orionbelt.compiler.star import StarSchemaPlanner

            return StarSchemaPlanner().plan(
                resolved, model, qualify_table=qualify_table, dialect=dialect
            )

        # Every multi-column aggregate reads its arguments from one row: a
        # two-column statistic (CORR/COVAR_*/REGR_*) correlates a pair, a
        # multi-column COUNT DISTINCT counts observed tuples. A leg that owns
        # all the arguments carries them as separate columns and the outer
        # query rebuilds the aggregate over them, so a multi-fact query costs
        # such a measure nothing.
        #
        # What none of them survives is having their arguments on data objects
        # no single leg reaches — the definition of ``cross_fact``. UNION ALL
        # stacks facts rather than joining them, so each leg supplies one
        # column and NULL-pads the rest, and no row ever carries a complete
        # set. The statistics then return NULL over zero pairs, and the tuple
        # count concatenates a NULL into every row and returns 0 — a wrong
        # answer rather than a failure, which is the reason to refuse here
        # rather than let it compile.
        #
        # Metric components count: a metric is planned by inlining its
        # components' aggregates, so ``{[Cross Corr]}`` reaches the same
        # rebuild — it just used to arrive there without passing the guard.
        cross_fact_names = {m.name for m in cross_fact} if cross_fact else set()
        for measure in (*resolved.measures, *resolved.metric_components.values()):
            if measure.name in cross_fact_names and self._is_multi_field(measure):
                agg = measure.aggregation.lower() if measure.aggregation else ""
                raise UnsupportedAggregationForCFLError(measure.name, agg)

        # Ordered aggregates now carry their sort key through the union as a
        # column of its own, so the outer re-aggregation can order by it. That
        # only works where the leg owning the measure can actually reach the
        # sort column's object — otherwise the leg has nothing to project, and
        # the aggregate would come back in an arbitrary order. Refuse those.
        #
        # A cross-fact measure has no single owning leg, so an ordering on one
        # is refused outright. Metric components are covered because they are
        # planned into legs like any other measure.
        self._validate_ordered_aggregates(resolved, model, measures_by_object, cross_fact)

        # Multi-fact: UNION ALL strategy
        return self._plan_union_all(
            resolved,
            model,
            measures_by_object,
            cross_fact,
            qualify_table=qualify_table,
            union_by_name=union_by_name,
            dialect=dialect,
        )

    def _validate_ordered_aggregates(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        measures_by_object: dict[str, list[ResolvedMeasure]],
        cross_fact: list[ResolvedMeasure] | None,
    ) -> None:
        """Refuse ordered aggregates whose sort key their own leg cannot reach."""
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)
        owner: dict[str, str] = {}
        for obj_name, measures in measures_by_object.items():
            for measure in measures:
                owner[measure.name] = obj_name

        candidates = list(measures_by_object.values())
        if cross_fact:
            candidates.append(cross_fact)
        for measures in candidates:
            for measure in measures:
                item = self._within_group_item(measure)
                if item is None:
                    continue
                sort_objects: set[str] = set()
                cfl_projection.collect_table_refs(item.expr, sort_objects)
                if not sort_objects:
                    continue
                leg_object = owner.get(measure.name)
                reachable: set[str] = (
                    graph.descendants(leg_object) | {leg_object}
                    if leg_object is not None
                    else set()
                )
                unreachable = sort_objects - reachable
                if unreachable:
                    raise WithinGroupNotSupportedInCFLError(measure.name, sorted(unreachable)[0])

    def _validate_fanout(self, resolved: ResolvedQuery, model: SemanticModel) -> None:
        """Validate that grain is compatible and no fanout will occur."""
        errors: list[str] = []

        for dim in resolved.dimensions:
            if dim.object_name not in model.data_objects:
                errors.append(
                    f"Dimension '{dim.name}' references unknown data object '{dim.object_name}'"
                )

        if errors:
            raise FanoutError("; ".join(errors))

    def _group_measures_by_object(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
    ) -> tuple[dict[str, list[ResolvedMeasure]], list[ResolvedMeasure]]:
        """Group measures by their primary source object."""
        return cfl_projection.group_measures_by_object(self, resolved, model)

    @staticmethod
    def _reject_unreachable_dimensions(
        measures_by_object: dict[str, list[ResolvedMeasure]],
        resolved: ResolvedQuery,
        model: SemanticModel,
    ) -> None:
        """Refuse a dimension the one remaining leg cannot produce.

        A query looks multi-fact while its measures span facts, and resolution
        skips the reachability check on that basis - the union answers it, each
        leg projecting the dimensions it reaches and NULL-padding the rest. When
        the legs collapse to one there is no union to do that, and the star this
        delegates to would project a column from a table it does not select
        from. The same refusal resolution would have raised.
        """
        root = next(iter(measures_by_object), resolved.base_object)
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)
        reachable = graph.descendants(root) | {root}
        unreachable = sorted(
            {dim.object_name for dim in resolved.dimensions if dim.object_name not in reachable}
        )
        if not unreachable:
            return
        raise ResolutionError(
            [
                SemanticError(
                    code="UNREACHABLE_REQUIRED_OBJECT",
                    message=(
                        f"Data object '{name}' is required by the query but cannot be "
                        f"reached from base '{root}' via directed joins. Many-to-one joins "
                        f"are forward-only; reverse traversal would inflate row counts. Add "
                        f"an explicit join from '{root}' (or an intermediate object) to "
                        f"'{name}', or split the query so each fact is queried "
                        f"independently."
                    ),
                    path="select",
                )
                for name in unreachable
            ]
        )

    @staticmethod
    def _group_dimensions_into_legs(
        resolved: ResolvedQuery,
        model: SemanticModel,
    ) -> dict[str, list[ResolvedMeasure]]:
        """Group dimensions into CFL legs for dimension-only queries."""
        return cfl_projection.group_dimensions_into_legs(resolved, model)

    @staticmethod
    def _is_multi_field(measure: ResolvedMeasure) -> bool:
        """Check if a measure has multiple field args (e.g. COUNT(a, b))."""
        return cfl_projection.is_multi_field(measure)

    @staticmethod
    def _resolve_union_alignment_type(
        measure: ResolvedMeasure,
        model: SemanticModel,
        dialect: Dialect | None = None,
    ) -> str | None:
        """The type every UNION leg agrees on for *measure*'s column."""
        return cfl_projection.resolve_union_alignment_type(measure, model, dialect)

    def _resolve_owning_leg_cast_type(
        self,
        measure: ResolvedMeasure,
        model: SemanticModel,
        dialect: Dialect | None = None,
    ) -> str | None:
        return cfl_projection.resolve_owning_leg_cast_type(measure, model, dialect)

    @staticmethod
    def _resolve_null_type_for_field(
        measure: ResolvedMeasure,
        field_idx: int,
        model: SemanticModel,
        dialect: Dialect | None = None,
    ) -> str | None:
        """Resolve the SQL type for NULL padding in CFL UNION ALL legs."""
        return cfl_projection.resolve_null_type_for_field(measure, field_idx, model, dialect)

    @staticmethod
    def _unwrap_aggregation(measure: ResolvedMeasure) -> Expr:
        """Extract the inner expression from an aggregated measure."""
        return cfl_projection.unwrap_aggregation(measure)

    def _build_outer_metric_expr(
        self,
        metric: ResolvedMeasure,
        resolved: ResolvedQuery,
        cte_name: str,
    ) -> Expr:
        """Build the outer query expression for a metric."""
        return cfl_projection.build_outer_metric_expr(self, metric, resolved, cte_name)

    def _substitute_outer_refs(self, expr: Expr, resolved: ResolvedQuery, cte_name: str) -> Expr:
        """Recursively substitute measure refs with outer aggregations."""
        return cfl_projection.substitute_outer_refs(self, expr, resolved, cte_name)

    @staticmethod
    def _collect_table_refs(expr: Expr, tables: set[str]) -> None:
        """Recursively collect table names from ColumnRef nodes."""
        cfl_projection.collect_table_refs(expr, tables)

    @staticmethod
    def _leg_projects_argument(
        measure: ResolvedMeasure,
        arg: Expr,
        obj_name: str,
        this_measure_names: set[str],
    ) -> bool:
        """Whether this leg supplies *arg* of a multi-field measure, or NULL-pads it.

        A leg that **owns** the measure projects every argument, full stop.
        Grouping already put the measure here because one root reaches all the
        objects its arguments read (``_single_leg_root``), and this leg is that
        root, so a joined column (``corr(Returns.Qty, Calendar.Month)``), a
        computed column expanding to one, and a computed column that reads no
        column at all (``One: {expression: '1'}``) are each as projectable here
        as a bare own-table reference. Nothing else projects them, so any test
        this applies can only take an argument away from the one leg that could
        have supplied it.

        Two narrower rules were tried and both lost arguments this way. Matching
        a bare ``ColumnRef`` on this exact object dropped joined and computed
        columns; also demanding the argument reference *some* table dropped
        constant expressions, whose reference set is empty. In both cases the
        owning leg NULL-padded its own measure's argument, so the tuple count
        counted a column of NULLs and returned 0, and a two-column statistic -
        NULL unless every argument is present - returned NULL, on the dialects
        that pad explicitly; the ones using ``UNION ALL BY NAME`` failed to bind
        instead. A wrong number is the worse half of that.

        A **cross-fact** measure is the other case: no single leg reaches all its
        arguments, so each leg takes the ones rooted in its own fact and the rest
        are padded. A conformed dimension is reachable from every leg, so the
        stricter own-object rule is what keeps two legs from both claiming it.
        """
        if measure.name in this_measure_names:
            return True
        return isinstance(arg, ColumnRef) and arg.table == obj_name

    @staticmethod
    def _within_group_item(measure: ResolvedMeasure) -> OrderByItem | None:
        """The sort key a leg must carry, or ``None`` if it need not carry one."""
        return cfl_projection.within_group_item(measure)

    @staticmethod
    def _remap_cfl_order_by(expr: Expr, resolved: ResolvedQuery, model: SemanticModel) -> Expr:
        """Remap ORDER BY expressions to use CTE aliases for the outer query."""
        return cfl_projection.remap_cfl_order_by(expr, resolved, model)

    def _plan_union_all(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        measures_by_object: dict[str, list[ResolvedMeasure]],
        cross_fact: list[ResolvedMeasure] | None = None,
        qualify_table: Callable[[DataObject], str] | None = None,
        union_by_name: bool = False,
        dialect: Dialect | None = None,
    ) -> QueryPlan:
        """UNION ALL strategy: stack fact legs with NULL padding, aggregate outside.

        When *union_by_name* is True (DuckDB, Snowflake) each leg only emits
        the columns it actually has — the database fills missing columns with
        NULL automatically via ``UNION ALL BY NAME``.
        """
        graph = JoinGraph(model, use_path_names=resolved.use_path_names or None)

        def qualify(obj: DataObject) -> str:
            return qualify_table(obj) if qualify_table else obj.qualified_code

        # Internal composite columns (multi-field arguments, ordered-aggregate
        # sort keys), allocated once so the legs that project them, the legs
        # that NULL-pad them and the outer re-aggregation all agree — and so
        # none of them shadows a column the composite already carries under a
        # user-facing name.
        aliases = cfl_projection.composite_aliases(resolved)

        # Anchored measures are conformed the same way the star planner does
        # it, but the subqueries are joined inside the leg that owns the
        # measure rather than into one shared FROM.
        conformed_facts, conformed_exprs = plan_conformed_facts(resolved, model, qualify)
        facts_by_measure: dict[str, list[ConformedFact]] = {}
        for fact in conformed_facts:
            facts_by_measure.setdefault(fact.measure_name, []).append(fact)

        # Collect all measures across all objects + cross-fact measures
        all_measures: list[ResolvedMeasure] = []
        for measures in measures_by_object.values():
            all_measures.extend(measures)
        if cross_fact:
            all_measures.extend(cross_fact)

        # Collect data objects referenced by WHERE filters — each leg
        # must join these tables so the filter predicates are valid.
        filter_objects: set[str] = set()
        for wf in resolved.where_filters:
            self._collect_table_refs(wf.expression, filter_objects)
            # An EXISTS body correlates to an outer table, which the walk above
            # cannot see: the body is a Select, not an expression. Each leg
            # emits that body in its own WHERE, so each leg has to join it.
            cfl_projection.collect_correlated_tables(wf.expression, filter_objects)

        # Build one SELECT per fact object group.
        # Each leg computes its own LCA (least common ancestor) as the lead
        # table — the graph-central node that can reach all dimension objects
        # and the measure's source object with minimal hops.
        union_legs: list[Select] = []
        leg_infos: list[CflLegInfo] = []
        dedup_offenders: dict[str, str] = {}
        for obj_name, measures in measures_by_object.items():
            leg_builder = QueryBuilder()
            this_measure_names = {m.name for m in measures}

            # Compute reachability from this leg's fact object upfront
            reachable = graph.descendants(obj_name) | {obj_name}

            # Collect table references from this leg's own-measure
            # expressions. A measure like ``Electronics Sales`` is
            # defined as ``SUM(CASE WHEN Products.productcat = …
            # THEN Sales.salesamount END)`` — the CASE condition
            # references Products, which must be joined into this
            # leg's FROM. Without this, the generated SQL emits
            # ``"Products"."productcat"`` against a FROM clause that
            # only has Sales + Clients, and the database raises
            # "missing FROM-clause entry for table Products".
            measure_expr_objects: set[str] = set()
            for m in measures:
                self._collect_table_refs(m.expression, measure_expr_objects)
            # A conformed fact is reached by a GROUP BY subquery joined below,
            # not by a join from this leg's lead, so it must not become a join
            # requirement: doing so would join the raw fact and fan the leg out.
            for m in measures:
                for fact in facts_by_measure.get(m.name, ()):
                    measure_expr_objects.discard(fact.object_name)
            if cross_fact:
                for m in cross_fact:
                    if m.name in this_measure_names:
                        self._collect_table_refs(m.expression, measure_expr_objects)

            # A leg's FROM is its *lead*, not its key: the common root of the
            # key and what that key reaches. Where the key is a measure's source
            # on the one side of a join - ``Products``, reaching nothing - the
            # lead is the ``Sales`` that reaches both, and dimensions the lead
            # can produce were being NULL-padded on the grounds that the key
            # could not. Every row of the leg then collapsed into one NULL
            # group. Widen the reachability to the lead where a lead covering
            # the query's dimensions exists at all; where none does, the facts
            # really are independent and the padding below is right.
            wanted = (
                {dim.object_name for dim in resolved.dimensions}
                | {obj_name}
                | filter_objects
                | measure_expr_objects
            )
            wide_lead = graph.find_common_root(wanted)
            if wide_lead and obj_name in (graph.descendants(wide_lead) | {wide_lead}):
                reachable = graph.descendants(wide_lead) | {wide_lead}

            # SELECT conformed dimensions — only emit real column refs for
            # dimensions reachable from this leg's fact AND whose `via:`
            # waypoint (if any) is also reachable from this leg's fact.
            # Role-playing dimensions tied to a different fact via `via:`
            # are NULL-padded so each leg only projects the values that
            # belong to its own fact.
            for dim in resolved.dimensions:
                via_ok = dim.via is None or dim.via in reachable
                if dim.object_name in reachable and via_ok:
                    col: Expr = make_column_expr(model, dim.object_name, dim.column_name)
                    if dim.grain and dialect:
                        col = dialect.render_time_grain(col, dim.grain)
                    leg_builder.select(AliasedExpr(expr=col, alias=dim.name))
                elif not union_by_name:
                    model_dim = model.dimensions.get(dim.name)
                    dim_type = model_dim.result_type.value if model_dim else None
                    col = Cast(Literal.null(), type_name=dim_type) if dim_type else Literal.null()
                    leg_builder.select(AliasedExpr(expr=col, alias=dim.name))

            # SELECT this fact's measures (raw expressions, no aggregation).
            # When union_by_name is True, skip NULL padding for other facts'
            # measures — the database fills them automatically.
            for m in all_measures:
                if self._is_multi_field(m):
                    # The aggregate, not the expression: a declared default
                    # wraps it in a COALESCE whose second argument is the
                    # default itself, which is not one of the aggregate's.
                    assert isinstance(m.aggregate, FunctionCall)
                    for i, arg in enumerate(m.aggregate.args):
                        alias = aliases.multi_field[m.name][i]
                        if self._leg_projects_argument(m, arg, obj_name, this_measure_names):
                            leg_builder.select(AliasedExpr(expr=arg, alias=alias))
                        elif not union_by_name:
                            null_type = self._resolve_null_type_for_field(m, i, model)
                            null_expr: Expr = (
                                Cast(Literal.null(), type_name=null_type)
                                if null_type
                                else Literal.null()
                            )
                            leg_builder.select(AliasedExpr(expr=null_expr, alias=alias))
                elif m.name in this_measure_names:
                    # Cast the own-measure column to the same type used for
                    # NULL padding in sibling legs, so every leg's column
                    # agrees on a single type. Without this, strict-typed
                    # engines (ClickHouse with UNION ALL) produce a Variant
                    # type that SUM can't aggregate ("ILLEGAL_TYPE_OF_ARGUMENT
                    # Variant(Decimal, Float64)").
                    own_expr: Expr = self._unwrap_aggregation(
                        replace(m, expression=conformed_exprs[m.name])
                        if m.name in conformed_exprs
                        else m
                    )
                    # The leg that owns the measure projects it **uncast**, in
                    # the source's own type, exactly as the star path does.
                    #
                    # Only the NULL pads below carry a declared type, and that
                    # is enough to settle the union: measured on DuckDB,
                    # Postgres and ClickHouse, a typed pad beside an uncast
                    # column resolves to the *column's* type, in any leg order.
                    # Casting this side as well is what rounded pre-aggregation
                    # rows to the declared output type (#305), and then, once
                    # the alignment was widened to carry the scale, overflowed
                    # a value the source column held quite legally: a
                    # DECIMAL(38, 20) leaves only 18 integer digits, so a
                    # DECIMAL(38, 15) source failed on Postgres and DuckDB
                    # under CFL while succeeding alone (#311). Not casting
                    # cannot do either, because there is no second type.
                    #
                    # The exception is an alignment that *converts* rather than
                    # widens - LISTAGG over an integer column pads with text -
                    # where an uncast leg would meet a pad of another type and
                    # Postgres would refuse the union.
                    own_type_name = self._resolve_owning_leg_cast_type(m, model, dialect)
                    if own_type_name:
                        own_expr = Cast(expr=own_expr, type_name=own_type_name)
                    leg_builder.select(AliasedExpr(expr=own_expr, alias=m.name))
                    # An ordered aggregate's sort key rides along as its own
                    # column so the outer re-aggregation can order by it.
                    wg_item = self._within_group_item(m)
                    if wg_item is not None:
                        leg_builder.select(
                            AliasedExpr(expr=wg_item.expr, alias=aliases.within_group[m.name])
                        )
                elif not union_by_name:
                    model_measure = model.measures.get(m.name)
                    null_type_name = self._resolve_union_alignment_type(m, model, dialect)
                    if null_type_name is None and model_measure:
                        null_type_name = model_measure.result_type.value
                    null_expr = (
                        Cast(Literal.null(), type_name=null_type_name)
                        if null_type_name
                        else Literal.null()
                    )
                    leg_builder.select(AliasedExpr(expr=null_expr, alias=m.name))
                    # Pad the sort-key column too, so every leg agrees on the
                    # union's column list.
                    if self._within_group_item(m) is not None:
                        leg_builder.select(
                            AliasedExpr(expr=Literal.null(), alias=aliases.within_group[m.name])
                        )

            # Determine the common root for this leg:
            # the deepest directed ancestor that can reach all dimension
            # objects, measure's source object, filter-referenced objects,
            # and any objects referenced by this leg's measure expressions.
            # Only include dimensions reachable from this leg's fact object.
            leg_required = {
                dim.object_name for dim in resolved.dimensions if dim.object_name in reachable
            }
            leg_required.add(obj_name)
            leg_required.update(filter_objects)
            # Include objects referenced by measure expressions, but only
            # those reachable from this leg's fact — cross-fact filter
            # tables would otherwise pull unrelated facts into the leg.
            leg_required.update(measure_expr_objects & reachable)
            lead = graph.find_common_root(leg_required)
            lead_obj = model.data_objects.get(lead)

            # FROM: the lead (LCA) table
            if lead_obj:
                leg_builder.from_(qualify(lead_obj), alias=lead)

            # Conformed facts for the anchored measures this leg owns: one row
            # per shared key, so many-to-one and no fanout onto the leg's grain.
            for m in measures:
                for fact in facts_by_measure.get(m.name, ()):
                    leg_builder.join(
                        table=fact.select,
                        on=fact.on,
                        join_type=conformed_join_type(),
                        alias=fact.alias,
                    )

            # JOINs: all required objects reachable from the lead
            join_targets = leg_required - {lead}
            steps: list[JoinStep] = []
            if join_targets:
                steps = graph.find_join_path(
                    {lead},
                    leg_required,
                    via_constraints=resolved.via_constraints or None,
                )
                # Dedupe by alias so a dim reachable through multiple
                # paths within one leg emits only one JOIN — postgres
                # rejects "table specified more than once" when two
                # role-played dims resolve to the same target object.
                joined_aliases: set[str] = {lead}
                for step in steps:
                    if step.to_object in joined_aliases:
                        continue
                    target_object = model.data_objects.get(step.to_object)
                    if target_object:
                        on_expr = graph.build_join_condition(step)
                        leg_builder.join(
                            table=qualify(target_object),
                            on=on_expr,
                            join_type=step.join_type,
                            alias=step.to_object,
                        )
                        joined_aliases.add(step.to_object)

            # A measure sourced from an object this leg's own joins replicate
            # is summed once per row of the many side. Resolution cannot see
            # that: its join steps are the base object's, and the step that
            # replicates lives inside a leg. The union has no per-leg grain to
            # deduplicate at either - the legs project the values to aggregate
            # rather than aggregating them - so this is refused rather than
            # answered with a plausible number in the right group.
            leg_dedup = detect_dedup_measures(
                replace(resolved, join_steps=steps, base_object=lead, measures=measures),
                model,
            )
            dedup_offenders.update(leg_dedup.measures)
            dedup_offenders.update(leg_dedup.components)

            # Capture leg info for explain
            leg_join_strs = (
                [f"{s.from_object} → {s.to_object}" for s in steps] if join_targets else []
            )
            if lead == obj_name:
                leg_reason = (
                    f'"{lead}" is the measure source — '
                    f"all required dimension objects are reachable from it"
                )
            else:
                leg_reason = (
                    f'"{lead}" is the deepest common root that can reach '
                    f'measure source "{obj_name}" and all reachable dimension objects'
                )
            leg_infos.append(
                CflLegInfo(
                    measure_source=obj_name,
                    common_root=lead,
                    reason=leg_reason,
                    measures=[m.name for m in measures],
                    joins=leg_join_strs,
                )
            )

            # Apply WHERE filters to each leg
            for wf in resolved.where_filters:
                leg_builder.where(wf.expression)

            union_legs.append(leg_builder.build())

        if dedup_offenders:
            listed = ", ".join(f"'{name}'" for name in sorted(dedup_offenders))
            raise ResolutionError(
                [
                    SemanticError(
                        code="INCOMPATIBLE_COMBINATION",
                        message=(
                            f"Measure(s) {listed} are sourced from an object whose rows this "
                            f"query's joins replicate, so they must be aggregated over "
                            f"deduplicated rows. This query spans facts that cannot be "
                            f"joined, so it is planned as a UNION ALL whose legs project the "
                            f"values to aggregate rather than aggregating them, leaving no "
                            f"per-leg grain to deduplicate at."
                        ),
                        path="select.measures",
                        hint=(
                            "Query the measure without the measures from the other fact, or "
                            "set allowFanOut: true to aggregate the duplicated rows as-is."
                        ),
                        context={"measures": sorted(dedup_offenders)},
                    )
                ]
            )

        # Create the UNION ALL CTE
        cte_name = "composite_01"
        union_cte = CTE(name=cte_name, query=UnionAll(queries=union_legs))
        # All ColumnRefs that resolve to raw CTE columns inside outer-query
        # aggregate functions are qualified with *cte_name*. ClickHouse otherwise
        # resolves bare identifiers to sibling SELECT aliases first — when those
        # aliases are themselves aggregates (the case for measures and metrics
        # in the outer SELECT), it rejects the resulting nested aggregate as
        # ``ILLEGAL_AGGREGATION``. The qualification is harmless on dialects
        # that resolve column-first.

        # Build outer query: aggregate over the composite CTE
        outer_builder = QueryBuilder()

        # SELECT dimensions.  Coalesce groups emit COALESCE(d1, d2, ...) once
        # under the alias; plain dims keep their original column reference.
        emitted_coalesce_aliases: set[str] = set()
        coalesce_groups: dict[str, list[str]] = {}
        for d in resolved.dimensions:
            if d.coalesce_alias:
                coalesce_groups.setdefault(d.coalesce_alias, []).append(d.name)
        for dim in resolved.dimensions:
            if dim.coalesce_alias:
                if dim.coalesce_alias in emitted_coalesce_aliases:
                    continue
                emitted_coalesce_aliases.add(dim.coalesce_alias)
                outer_builder.select(
                    AliasedExpr(
                        expr=FunctionCall(
                            name="COALESCE",
                            args=[
                                ColumnRef(name=member)
                                for member in coalesce_groups[dim.coalesce_alias]
                            ],
                        ),
                        alias=dim.coalesce_alias,
                    )
                )
            else:
                outer_builder.select(
                    AliasedExpr(
                        expr=ColumnRef(name=dim.name),
                        alias=dim.name,
                    )
                )

        # SELECT aggregated measures and metrics
        # First, aggregate every measure from the UNION ALL legs. This
        # includes component measures pulled in only to feed a metric
        # (e.g. Total Returns / Total Purchases behind Return Rate /
        # Gross Margin). We still compute their aggregate expression and
        # record it in ``outer_measure_exprs`` so HAVING can reference any
        # measure, but we only PROJECT the measures the caller actually
        # requested — otherwise the result carries extra columns the
        # consumer never asked for, which Postgres-federation clients
        # (Dremio) reject as an unexpected dataset shape.
        settings = model.settings
        requested_measure_names = {rm.name for rm in resolved.measures}
        seen_measure_names: set[str] = set()
        outer_measure_exprs: dict[str, Expr] = {}
        for m in all_measures:
            seen_measure_names.add(m.name)
            # Shared with the metric projection so the two cannot drift: this
            # picks the rebuild that matches how the legs projected the measure
            # (concatenated argument columns, or its own single column) and
            # reapplies DISTINCT, the LISTAGG separator and any withinGroup
            # ordering over the sort key the legs carried.
            agg_expr: Expr = cfl_projection.build_outer_measure_expr(m, cte_name, aliases)
            # Apply CAST for resolved data_type (effective_measures so
            # multi-fact synthesized counts get the same integer CAST as
            # declared count measures).
            model_measure = model.effective_measures.get(m.name)
            if model_measure and dialect:
                # Same path as the star planner. Here the argument is the union
                # column rather than the source, which is still the right thing
                # to average: the legs project pre-aggregation rows.
                agg_expr = cast_measure_to_resolved_type(agg_expr, model_measure, settings, dialect)
            if m.name in requested_measure_names:
                outer_builder.select(AliasedExpr(expr=agg_expr, alias=m.name))
            outer_measure_exprs[m.name] = agg_expr

        # Then, add metric expressions that combine component measures
        for m in resolved.measures:
            if m.component_measures and m.name not in seen_measure_names:
                metric_expr: Expr = self._build_outer_metric_expr(m, resolved, cte_name)
                metric = model.metrics.get(m.name)
                if metric and dialect:
                    resolved_type = resolve_metric_data_type(metric, settings)
                    if resolved_type:
                        metric_expr = dialect.cast_to_obml_type(metric_expr, resolved_type)
                outer_builder.select(AliasedExpr(expr=metric_expr, alias=m.name))
                outer_measure_exprs[m.name] = metric_expr

        # Recorded for the wrappers that run after planning. Each of them
        # re-projects some measure's aggregate into a CTE of its own, and every
        # such CTE selects from the composite below - where the fact tables the
        # resolved expressions name are not in scope, so rebuilding from those
        # produces SQL that does not bind.
        resolved.projected_expressions = dict(outer_measure_exprs)
        resolved.composite_cte = cte_name

        outer_builder.from_(cte_name, alias=cte_name)

        # GROUP BY dimensions.  Coalesce groups group by the COALESCE expression
        # itself (most dialects accept either the alias or the expression; the
        # expression is portable across all eight supported dialects).
        grouped_coalesce_aliases: set[str] = set()
        for dim in resolved.dimensions:
            if dim.coalesce_alias:
                if dim.coalesce_alias in grouped_coalesce_aliases:
                    continue
                grouped_coalesce_aliases.add(dim.coalesce_alias)
                outer_builder.group_by(
                    FunctionCall(
                        name="COALESCE",
                        args=[
                            ColumnRef(name=member) for member in coalesce_groups[dim.coalesce_alias]
                        ],
                    )
                )
            else:
                outer_builder.group_by(ColumnRef(name=dim.name))

        # GROUPING() flag columns + grouping modifier (rollup/cube) — outer query only
        # so subtotal rows compose correctly over the unioned facts (the
        # individual UNION ALL legs stay at detail grain).
        if resolved.grouping is not None and resolved.dimensions:
            outer_builder.grouping(resolved.grouping.value)
            flag_aliases: list[str] = []
            for dim in resolved.dimensions:
                alias_name = dim.coalesce_alias or dim.name
                if alias_name in flag_aliases:
                    continue
                flag_aliases.append(alias_name)
            for alias in flag_aliases:
                flag_col = FunctionCall(name="GROUPING", args=[ColumnRef(name=alias)])
                outer_builder.select(AliasedExpr(expr=flag_col, alias=_grouping_flag_alias(alias)))

        # HAVING — expand alias references to actual CAST'd aggregate expressions.
        # A predicate on a measure a later wrapper finishes with a window
        # function is withheld, exactly as ``star.py`` withholds it: only the
        # pre-window aggregate exists here, so evaluating it would filter the
        # wrong value, and ``PASS_HAVING_WINDOW`` applies it over the windowed
        # rows instead. CFL is picked by the planner before any pass runs, so
        # the window pass lands on ``composite_01`` just as it lands on a
        # wrapper's CTE - this is the multi-fact half of the same rule.
        from orionbelt.compiler.having_hoist import windowed_aliases

        deferred = windowed_aliases(resolved)
        for hf in resolved.having_filters:
            if hf.referenced_fields & deferred:
                continue
            outer_builder.having(_expand_cfl_measure_refs(hf.expression, outer_measure_exprs))

        # ORDER BY and LIMIT — remap to CTE aliases
        for expr, desc, nulls in resolved.order_by_exprs:
            outer_builder.order_by(
                self._remap_cfl_order_by(expr, resolved, model),
                desc=desc,
                nulls_last=_nulls_last(nulls),
            )
        if resolved.limit is not None:
            outer_builder.limit(resolved.limit)
        if resolved.offset is not None:
            outer_builder.offset(resolved.offset)

        outer_select = outer_builder.build()

        # Attach CTE
        final = Select(
            columns=outer_select.columns,
            from_=outer_select.from_,
            joins=outer_select.joins,
            where=outer_select.where,
            group_by=outer_select.group_by,
            having=outer_select.having,
            order_by=outer_select.order_by,
            limit=outer_select.limit,
            offset=outer_select.offset,
            ctes=[union_cte],
            grouping=outer_select.grouping,
        )

        return QueryPlan(ast=final, cfl_legs=leg_infos)

    # -- dimensionsExclude: EXCEPT-based anti-join ----------------------------

    def _plan_dimensions_exclude(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
    ) -> QueryPlan:
        """Plan a dimensionsExclude query using EXCEPT pattern."""
        return cfl_exclude.plan_dimensions_exclude(self, resolved, model, qualify_table)

    @staticmethod
    def _partition_dimensions(
        resolved: ResolvedQuery,
        graph: JoinGraph,
    ) -> list[list[ResolvedDimension]]:
        """Partition dimensions into groups on independent branches."""
        return cfl_exclude.partition_dimensions(resolved, graph)

    @staticmethod
    def _build_group_distinct_select(
        dims: list[ResolvedDimension],
        model: SemanticModel,
        graph: JoinGraph,
        qualify: Callable[[DataObject], str],
        via_constraints: dict[str, str] | None = None,
    ) -> Select:
        """Build SELECT DISTINCT (via GROUP BY) for a group of dimensions."""
        return cfl_exclude.build_group_distinct_select(
            dims, model, graph, qualify, via_constraints=via_constraints
        )

    def _build_existing_pairs_select(
        self,
        resolved: ResolvedQuery,
        model: SemanticModel,
        graph: JoinGraph,
        qualify: Callable[[DataObject], str],
    ) -> Select:
        """Build SELECT for existing dimension combinations via fact-table joins."""
        return cfl_exclude.build_existing_pairs_select(self, resolved, model, graph, qualify)
