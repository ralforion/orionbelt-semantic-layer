"""Phase 1: Resolve semantic references to physical expressions."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CaseExpr,
    Cast,
    ColumnRef,
    Expr,
    FunctionCall,
    InTimeZone,
    Literal,
    NestedField,
    OrderByItem,
)
from orionbelt.compiler import (
    filter_resolution,
    metric_resolution,
    raw_resolution,
)
from orionbelt.compiler.expr_parser import (
    parse_expression,
    tokenize_measure_expression,
)
from orionbelt.compiler.expr_rewrite import map_nodes
from orionbelt.compiler.filters import (
    build_measure_filter_condition,
    collect_measure_filter_objects,
)
from orionbelt.compiler.graph import JoinGraph, JoinStep, path_overrides
from orionbelt.compiler.type_resolver import resolve_measure_data_type
from orionbelt.models.errors import SemanticError
from orionbelt.models.expressions import find_qualified_refs, substitute_placeholders
from orionbelt.models.query import (
    CoalesceDimension,
    DimensionRef,
    Grouping,
    NullsPosition,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
    QueryObject,
    UsePathName,
)
from orionbelt.models.semantic import (
    CASTABLE_TEMPORAL_TYPES,
    DATE_BEARING_TYPES,
    SUB_DAY_GRAINS,
    AggregationType,
    CumulativeAggType,
    DataObject,
    DataObjectColumn,
    DataType,
    Dimension,
    FilterContext,
    GrainMode,
    GrainOverride,
    GrainToDate,
    Measure,
    Metric,
    MetricType,
    ModelFilter,
    ModelSettings,
    PeriodOverPeriodComparison,
    SemanticModel,
    TimeGrain,
    WindowFunctionKind,
    result_type_holds_grain,
)
from orionbelt.models.types import DecimalType, parse_data_type
from orionbelt.models.warnings import WarningCode, warning

if TYPE_CHECKING:
    from orionbelt.dialect.base import Dialect


def parse_column_expression(
    column: DataObjectColumn,
    obj: DataObject,
    model: SemanticModel,
) -> Expr:
    """Parse a computed column's ``expression`` into an AST.

    ``{name}`` placeholders are substituted with ``{[obj.name].[name]}`` so the
    measure-expression tokenizer resolves them to physical, table-qualified
    column refs.

    Raises whatever the tokenizer or parser raises. Shared with
    ``parser.validator``, which asks the same question at model load: a check
    that parsed the body its own way could answer differently from the compiler
    that has to build it, and then the load-time answer would mean nothing.
    """
    expr_str = column.expression or ""

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name in obj.columns:
            return f"{{[{obj.name}].[{name}]}}"
        return match.group(0)

    rewritten = substitute_placeholders(expr_str, _sub)
    return parse_expression(tokenize_measure_expression(rewritten, model))


def _build_computed_column_expr(
    column: DataObjectColumn,
    obj: DataObject,
    model: SemanticModel,
    *,
    in_query_timezone: bool = True,
) -> Expr:
    """The AST a computed column stands for, in the query's time zone.

    A body that does not parse is an error rather than a fallback. There used
    to be one - a reference to the column's own ``code``, which a computed
    column does not have, so it emitted the *display name* as though it were a
    physical column. The model loaded, the query compiled, ``sql_valid`` came
    back true, and the database rejected a statement naming an object that only
    exists in the model (#359). A metric whose formula does not parse has always
    been refused with ``INVALID_METRIC_EXPRESSION``; this is the same answer for
    the other declaration form.
    """
    try:
        parsed = parse_column_expression(column, obj, model)
    except Exception as exc:
        raise ResolutionError(
            [
                SemanticError(
                    code="INVALID_COLUMN_EXPRESSION",
                    message=(
                        f"Computed column '{column.name}' in data object "
                        f"'{obj.name}' has invalid expression: {exc}"
                    ),
                    path=f"dataObjects.{obj.name}.columns.{column.name}.expression",
                )
            ]
        ) from exc
    # A join key opts out of the timezone conversion for the same reason a plain
    # one does, and it has to be threaded this far: a computed key converted
    # while the plain key it is compared against is not would be an asymmetric
    # comparison, and that changes which rows join rather than merely costing an
    # index.
    if not in_query_timezone:
        return parsed
    return apply_query_timezone(parsed, model)


def _reads_a_number(measure: Measure, settings: ModelSettings | None) -> bool:
    """Whether this measure's value is read as a number.

    ``None`` means the measure passes its value through and emits no cast, so
    a boolean has nothing to arrive at.
    """
    declared = resolve_measure_data_type(measure, settings)
    if declared is None:
        return False
    return isinstance(declared, DecimalType) or declared.name in ("integer", "bigint", "double")


def _flag_as_number(expr: Expr) -> Expr:
    """Read *expr* as a number when it is itself a boolean column.

    Only for a measure whose output is a number. An engine that carries a type
    through an aggregate then hands a boolean to whatever the measure declares:
    ``MAX(flag)`` declared decimal reached ClickHouse's decimal conversion as
    'true' and raised, where ``SUM`` of the same column had always been a
    number and worked.

    **The value being aggregated, not every boolean inside it.** Rewriting each
    reference reached the ones a measure uses as a *predicate*, and a predicate
    is not read as a number: ``CASE WHEN {Flag} THEN {Amt} ELSE 0 END`` became
    ``CASE WHEN CAST(flag AS INTEGER)``, which PostgreSQL refuses outright
    ("argument of CASE/WHEN must be type boolean") and BigQuery with it. The
    same reference reached through a computed column's body, so a measure could
    break without naming a boolean at all.

    Keyed on the type the reference carries, which both spellings of a measure
    populate -- ``make_column_expr`` for ``columns:`` and the tokenizer for
    ``expression:`` -- so one rule serves either. Anything larger than a bare
    reference is left alone: its own shape decides what its parts mean, and
    this cannot read that.

    **A computed boolean column is the known gap, and it is older than this.**
    ``{expression: "{Amt} > 0", abstractType: boolean}`` inlines to the
    comparison itself, which carries no declared type, so a measure summing it
    emits ``SUM("ev"."amt" > 0)`` -- rejected by PostgreSQL, whose ``sum`` has
    no boolean overload, and by BigQuery. That is what ``main`` emits for the
    same model today, in both measure spellings: this rule leaves it exactly
    where it found it while fixing the bare-column case beside it. Closing it
    means carrying a declared type onto an inlined body, which is the same
    threading ``cast_to_obml_type`` wants and a piece of work in its own right.
    """
    # ColumnRef and NestedField are the two nodes that carry a declared type;
    # a field of an unnested element is as much a source as a column is.
    # Equality, not identity: the node carries the value as a plain string
    # where the model carries the enum, and ``is`` quietly matched neither.
    if isinstance(expr, ColumnRef | NestedField) and expr.abstract_type == DataType.BOOLEAN:
        return Cast(expr=expr, type_name="int")
    return expr


def make_dimension_expr(
    model: SemanticModel,
    dim: ResolvedDimension,
    dialect: Dialect | None = None,
) -> Expr:
    """The expression a dimension is projected and grouped by.

    :func:`make_column_expr`, plus the time grain when the dimension declares
    one, plus the cast that keeps the model's word: a dimension declaring
    ``resultType: date`` kept that type until a ``timeGrain`` was added, and
    then it became whatever the engine's truncation returns. That is not the
    same thing on every engine - ``DATE`` on ClickHouse, ``TIMESTAMP`` on
    DuckDB, ``timestamptz`` on PostgreSQL, whose ``date_trunc`` resolves to the
    timestamptz overload and hands back a value that carries the session's zone,
    so which month a row belongs to depended on who ran the query (#369).

    Only a *temporal* declaration is cast, which is the case that was measured
    and the one the grain can change. A dimension declaring ``resultType:
    string`` over a grain is asking for a label - MySQL's month grain is a
    ``DATE_FORMAT``, a string by construction - and casting that to ``CHAR``
    would add a cast that says nothing. ``timestamp_tz`` and ``time_tz`` are
    left alone too: OBML has no cast target for either, and inventing one would
    be a guess rather than the declaration.

    Every planner and wrapper renders a dimension through here, so the SELECT
    and the GROUP BY cannot drift apart, which is what eight copies of these
    three lines were free to do.
    """
    col = make_column_expr(model, dim.object_name, dim.column_name)
    if not dim.grain or dialect is None:
        return col
    col = dialect.render_time_grain(col, dim.grain)
    declared = model.dimensions.get(dim.name)
    if declared is None or declared.result_type not in CASTABLE_TEMPORAL_TYPES:
        return col
    return dialect.cast_to_obml_type(col, parse_data_type(declared.result_type.value))


def effective_anchor(
    model: SemanticModel,
    measure: Measure,
    use_path_names: list[UsePathName] | None = None,
) -> str | None:
    """The data object whose grain *measure*'s expression is evaluated at.

    Only a declared ``anchor:`` counts. There is deliberately no default.

    Guessing from the expression was tried and removed. The obvious rule, "the
    leftmost object the expression names", makes a *commutative rewrite* change
    the answer: ``{[Sales].[Qty]} * {[Returns].[Qty]}`` and
    ``{[Returns].[Qty]} * {[Sales].[Qty]}`` are the same product, but anchoring
    on whichever is written first gives ``AVG`` 22 against 29.33, and ``MIN`` 8
    against 6, because the two facts have different row counts per key. No
    modeller expects reordering a product to move a number. ``SUM`` happens to
    be immune - both readings come to ``Σ_key (left total x right total)`` - but
    a rule that is only safe for one aggregate is not a rule.

    Nothing else identifies the anchor either: the two facts are symmetric in
    the expression, and query-dependent choices (the query's base object) would
    make one measure mean different things in different queries. So a measure
    that needs an anchor has to say so, and :func:`unanchored_cross_fact_objects`
    is what refuses the ones that do not.

    Returns ``None`` when a single root reaches everything the measure reads:
    an ordinary join already puts those columns on one row at that root's grain,
    which is what the star planner does anyway.
    """
    return measure.anchor or None


def shared_key_anchor(
    model: SemanticModel,
    measure: Measure,
    use_path_names: list[UsePathName] | None = None,
) -> str | None:
    """The object both facts join to, for a cross-fact expression with no ``anchor:``.

    This is the default treatment, and it is deliberately not either fact. Every
    conformed dimension both facts join to is a candidate; the one they both join
    to *directly* is used, and its grain becomes the expression's.

    Choosing the shared key rather than a fact is what makes the default safe to
    apply without asking. It is symmetric, so a commutative rewrite cannot move
    the answer: conforming both sides gives ``AVG`` 44 for
    ``{[Sales].[Qty]} * {[Returns].[Qty]}`` and 44 for the operands swapped,
    where anchoring on whichever fact is written first gives 22 and 29.33.

    ``anchor:`` overrides it, and means something genuinely different: evaluate
    per row of *that fact* rather than per shared key. Both are well defined;
    only one can be picked without being told.

    Empty unless the measure needs it: it has to aggregate an ``expression:``
    reading two or more objects no single join path reaches together.
    Multi-argument aggregates (``corr(a, b)``, a two-column ``count_distinct``)
    are excluded - they read their arguments from one row by definition, and CFL
    refuses those with a message about pairing rather than quietly answering a
    different question.
    """
    candidates = conform_key_candidates(model, measure, use_path_names)
    return candidates[0] if len(candidates) == 1 else None


def needs_conforming(
    model: SemanticModel,
    measure: Measure,
    use_path_names: list[UsePathName] | None = None,
) -> bool:
    """Whether *measure* reads facts that no join path reaches together.

    True regardless of whether an ``anchor:`` is declared or a shared key can be
    found, so the caller can tell "does not need a grain" from "needs one and
    cannot have one" - the second has to be refused, and reporting it as the
    first is what let these compile into unbound SQL.
    """
    if measure.expression is None or len(measure.columns) > 1:
        return False
    referenced = measure.referenced_objects
    if len(referenced) < 2:
        return False
    graph = JoinGraph(model, use_path_names=use_path_names or None)
    root = graph.find_common_root(set(referenced))
    return not (root and set(referenced) <= (graph.descendants(root) | {root}))


def conform_key_candidates(
    model: SemanticModel,
    measure: Measure,
    use_path_names: list[UsePathName] | None = None,
) -> list[str]:
    """Objects that could serve as *measure*'s conform key, in name order.

    Empty when the measure does not need one. Exactly one is the case
    :func:`shared_key_anchor` uses. More than one means the model offers several
    equally valid groupings and the measure has to say which
    (``ANCHOR_REQUIRED_AMBIGUOUS_KEY``).
    """
    if measure.anchor or not needs_conforming(model, measure, use_path_names):
        return []
    return model.common_join_targets(measure.referenced_objects, path_overrides(use_path_names))


def anchored_conformed_objects(
    model: SemanticModel,
    measure: Measure,
    use_path_names: list[UsePathName] | None = None,
) -> set[str]:
    """Objects an anchored measure reads that its anchor cannot reach by joins.

    These are the independent facts the anchor exists to bring in: each one is
    aggregated to the key it shares with the anchor and joined on many-to-one,
    rather than stacked into a ``UNION ALL`` leg where its column would never
    share a row with the anchor's.

    Objects the anchor *can* reach are not conformed — an ordinary join already
    puts them on the same row, at the anchor's grain, which is the whole point.
    Returns empty for a measure without ``anchor:``, and for one whose anchor
    reaches everything it reads (the declaration is then a no-op that documents
    intent).

    Shared by resolution, which subtracts these from the join requirements, and
    by the planner, which builds a conformed subquery per object. Deriving it in
    one place keeps the two from disagreeing about which objects those are.
    """
    anchor = effective_anchor(model, measure, use_path_names) or shared_key_anchor(
        model, measure, use_path_names
    )
    if not anchor:
        return set()
    graph = JoinGraph(model, use_path_names=use_path_names or None)
    reachable = graph.descendants(anchor) | {anchor}
    # Anchored on a fact, its own columns are read directly and only the other
    # facts conform. Anchored on the shared key, *every* fact conforms - the key
    # object has none of their columns, which is what makes it symmetric.
    return measure.source_objects - reachable


def make_column_expr(
    model: SemanticModel,
    object_name: str,
    column_label: str,
    *,
    in_query_timezone: bool = True,
) -> Expr:
    """Build the AST expression that represents a column reference.

    For plain columns, returns ``ColumnRef(name=col.code, table=object_name)``.
    For computed columns (those with an ``expression``), parses and
    substitutes placeholders so the returned AST already inlines the
    expression. Used by planners and filter resolution alike — the
    single source of truth for "render this column reference as SQL".

    A column of a **nested** object is a :class:`NestedField` rather than a
    ``ColumnRef``, because the engines do not agree that it is one: six read
    ``L."Key"``, Snowflake needs ``L.value:"Key"::string`` and does not compile
    the column form at all. Every caller goes through here, so the accessor is
    chosen once and no planner, filter or wrapper can spell it a different way.
    """
    obj = model.data_objects.get(object_name)
    if obj is None:
        return ColumnRef(name=column_label, table=object_name)
    column = obj.columns.get(column_label)
    if column is None:
        return ColumnRef(name=column_label, table=object_name)
    if column.expression:
        return _build_computed_column_expr(column, obj, model, in_query_timezone=in_query_timezone)
    ref: Expr
    if obj.is_nested:
        ref = NestedField(
            alias=object_name,
            field=column.code,
            abstract_type=str(column.abstract_type) if column.abstract_type else None,
        )
    else:
        # Where the ``columns:`` form resolves a name against the model, so
        # where its type is recorded; the expression tokenizer does the same for
        # ``expression:``. Every planner and wrapper comes through here (see
        # this function's docstring), and refs they invent afterwards for CTE
        # aliases carry no type, which is correct: at that point there is
        # nothing to record.
        ref = ColumnRef(
            name=column.code,
            table=object_name,
            abstract_type=str(column.abstract_type) if column.abstract_type else None,
        )
    if not in_query_timezone:
        return ref
    return _in_query_timezone(ref, column, model)


#: Timestamp types carry an instant, so which day or week they fall in depends on
#: the zone they are read in. A DATE has no instant and a TIME no date, so
#: neither has a week to move between.
_CONVERTIBLE_TYPES = frozenset({DataType.TIMESTAMP, DataType.TIMESTAMP_TZ})


def _in_query_timezone(ref: Expr, column: DataObjectColumn, model: SemanticModel) -> Expr:
    """Read a timestamp column in the model's query zone, if it states one.

    Attached here, at the column, rather than around the expressions that use
    it: a conversion applied twice moves the value twice (measured on MySQL,
    00:30 becoming 02:30), and an author's own conversion — through the
    function catalog or as opaque vendor SQL the compiler cannot see into —
    would be exactly that second application. Converting at the leaf makes the
    query zone the frame every expression starts from, so an author's
    conversion moves a value within it rather than on top of it.
    """
    settings = model.settings
    if settings is None or not settings.query_timezone:
        return ref
    if column.abstract_type not in _CONVERTIBLE_TYPES:
        return ref
    # A naive column means nothing until the model says which zone it was
    # written in, which is what ``defaultTimezone`` states. Without that,
    # the engine's own reading is left alone rather than guessed at.
    from_zone = settings.default_timezone if column.abstract_type is DataType.TIMESTAMP else None
    if column.abstract_type is DataType.TIMESTAMP and from_zone is None:
        return ref
    return InTimeZone(expr=ref, zone=settings.query_timezone, from_zone=from_zone)


def apply_query_timezone(expr: Expr, model: SemanticModel) -> Expr:
    """Read every timestamp column *expr* names in the model's query zone.

    ``make_column_expr`` converts a column the moment a dimension or filter
    names one, but an expression body reaches its columns by another road: a
    computed column and a measure expression are parsed from text, and the
    tokenizer resolves ``{[Object].[Column]}`` straight to a physical
    ``ColumnRef``. Without this pass those refs stayed in the warehouse's zone
    while the model's own dimensions moved to the query zone, so the same
    column meant two different instants depending on how it was reached - which
    is exactly the frame the leaf-attachment rule exists to make single.

    A node already converted is returned untouched rather than descended into,
    so applying this twice cannot wrap a column twice.
    """
    settings = model.settings
    if settings is None or not settings.query_timezone:
        return expr

    def rewrite(node: Expr) -> Expr | None:
        if isinstance(node, InTimeZone):
            return node
        if isinstance(node, NestedField):
            table, name = node.alias, node.field
        elif isinstance(node, ColumnRef) and node.table is not None:
            table, name = node.table, node.name
        else:
            return None
        obj = model.data_objects.get(table)
        if obj is None:
            return node
        column = next((c for c in obj.columns.values() if c.code == name), None)
        if column is None:
            return node
        return _in_query_timezone(node, column, model)

    return map_nodes(expr, rewrite)


@dataclass
class ResolvedField:
    """A resolved raw-mode field reference: ``DataObject.Column`` → physical column.

    Raw mode (``select.fields``) bypasses the semantic dimension/measure layer
    and projects physical columns directly. The ``alias`` defaults to the
    original ``"DataObject.Column"`` reference so result columns are
    self-describing.
    """

    object_name: str  # logical data object name (table alias)
    column_name: str  # logical column name
    source_column: str  # physical column name in the source table
    alias: str  # output column name (defaults to "object_name.column_name")


@dataclass
class ResolvedDimension:
    """A resolved dimension with its physical column reference."""

    name: str
    object_name: str
    column_name: str
    source_column: str
    grain: TimeGrain | None = None
    via: str | None = None  # Role-playing waypoint (data object the join must traverse)
    coalesce_alias: str | None = None  # Set when this dim is part of a coalesce group


@dataclass
class ResolvedMeasure:
    """A resolved measure with its aggregate expression."""

    name: str
    aggregation: str
    expression: Expr
    is_expression: bool = False
    default_value: str | int | float | bool | None = None
    component_measures: list[str] = field(default_factory=list)
    total: bool = False
    # Grain override fields
    grain_override: GrainOverride | None = None
    effective_grain: list[str] | None = None
    # Filter context fields
    filter_context: FilterContext | None = None
    # Cumulative metric fields
    is_cumulative: bool = False
    cumulative_measure: str | None = None
    cumulative_time_dimension: str | None = None
    cumulative_type: CumulativeAggType = CumulativeAggType.SUM
    cumulative_window: int | None = None
    cumulative_grain_to_date: GrainToDate | None = None
    cumulative_partition_by: list[str] = field(default_factory=list)
    # Period-over-period metric fields
    is_pop: bool = False
    pop_base_measure: str | None = None
    pop_time_dimension: str | None = None
    pop_grain: TimeGrain | None = None
    pop_offset: int = -1
    pop_offset_grain: TimeGrain | None = None
    pop_comparison: PeriodOverPeriodComparison | None = None
    # Window metric fields (rank / lag / lead / ntile / first_value / last_value)
    is_window: bool = False
    window_function: WindowFunctionKind | None = None
    window_base_measure: str | None = None
    window_time_dimension: str | None = None
    window_partition_by: list[str] = field(default_factory=list)
    window_offset: int | None = None
    window_buckets: int | None = None
    window_order_direction: str = "desc"
    window_default_value: str | int | float | bool | None = None

    @property
    def aggregate(self) -> Expr:
        """The aggregate itself, with any declared empty-set default peeled off.

        ``defaultValue`` presents as ``COALESCE(<aggregate>, <default>)``,
        which is what should be *projected* but not what should be taken
        apart: code reading an aggregate's arguments — a multi-field COUNT
        spread across CFL legs, a two-column statistic — would otherwise count
        the default as an argument, and did, producing a union leg that
        selected a bare ``0`` and an outer query that concatenated it.

        Anything inspecting the shape of the aggregate wants this; anything
        emitting the measure's value wants :attr:`expression`.
        """
        expr = self.expression
        if self.default_value is None:
            return expr
        if (
            isinstance(expr, FunctionCall)
            and expr.name.upper() == "COALESCE"
            and len(expr.args) == 2
        ):
            return expr.args[0]
        return expr

    def with_default(self, expr: Expr) -> Expr:
        """Re-apply the declared default around a rebuilt aggregate."""
        if self.default_value is None:
            return expr
        return FunctionCall(name="COALESCE", args=[expr, Literal(value=self.default_value)])

    @property
    def is_derived_metric(self) -> bool:
        """A metric that is only an expression over other measures.

        The boundary
        :func:`~orionbelt.compiler.metric_expansion.expand_metric_expression`
        recurses through: a derived metric has no wrapper of its own, so its
        placeholders have to be expanded in place. Cumulative, window, and
        period-over-period metrics are computed by their wrapper and referenced
        by name instead.
        """
        return bool(self.component_measures) and not (
            self.is_cumulative or self.is_pop or self.is_window
        )


@dataclass
class ResolvedFilter:
    """A resolved filter with physical expression."""

    expression: Expr
    is_aggregate: bool = False
    referenced_fields: frozenset[str] = field(default_factory=frozenset)


@dataclass
class ResolvedQuery:
    """Result of query resolution — ready for SQL planning."""

    dimensions: list[ResolvedDimension] = field(default_factory=list)
    measures: list[ResolvedMeasure] = field(default_factory=list)
    fields: list[ResolvedField] = field(default_factory=list)
    is_raw: bool = False
    distinct: bool = False
    base_object: str = ""
    required_objects: set[str] = field(default_factory=set)
    join_steps: list[JoinStep] = field(default_factory=list)
    where_filters: list[ResolvedFilter] = field(default_factory=list)
    having_filters: list[ResolvedFilter] = field(default_factory=list)
    order_by_exprs: list[tuple[Expr, bool, NullsPosition | None]] = field(default_factory=list)
    limit: int | None = None
    offset: int | None = None
    warnings: list[SemanticError] = field(default_factory=list)
    requires_cfl: bool = False
    measure_source_objects: set[str] = field(default_factory=set)
    metric_components: dict[str, ResolvedMeasure] = field(default_factory=dict)
    use_path_names: list[UsePathName] = field(default_factory=list)
    via_constraints: dict[str, str] = field(default_factory=dict)
    dimensions_exclude: bool = False
    allow_fan_out: bool = False
    """The query said its joins' row duplication is understood and intended.

    Carried from ``QueryObject.allowFanOut``; silences the fan-out warning for
    a measure mixing base-grain and replicated columns."""
    coalesce_aliases: set[str] = field(default_factory=set)
    grouping: Grouping | None = None
    dedup_measures: dict[str, str] = field(default_factory=dict)
    """Measures that must aggregate over deduplicated rows, mapped to the
    replicated data object they are sourced from.

    Populated after planning by
    :func:`~orionbelt.compiler.grain_dedup.detect_dedup_measures`; consumed by
    the ``grain_dedup`` compiler pass. Empty for every query whose measures all
    sit at (or below) the base object's grain."""

    dedup_components: dict[str, str] = field(default_factory=dict)
    """Metric *component* measures that must aggregate over deduplicated rows.

    Same mapping as :attr:`dedup_measures`, but for measures the query reaches
    only through a metric's expression. The planner inlines a component's
    aggregate into the metric column; the ``grain_dedup`` pass splits those back
    out so the deduplicated ones can be computed in their own CTE and the metric
    recomputed from the results."""

    composite_cte: str | None = None
    """Name of the UNION ALL composite CTE, when the plan actually built one.

    ``requires_cfl`` says a multi-fact plan was *asked for*; this says one was
    *produced* - the CFL planner delegates back to the star planner whenever the
    measures turn out to reach a single leg. A pass that has to reason about
    what its own CTE can select from wants this one."""

    projected_expressions: dict[str, Expr] = field(default_factory=dict)
    """Measures and metric components, mapped to the expression the plan
    projects them as.

    Set by whichever planner ran, and read by every pass that *re-projects* a
    measure's aggregate into a CTE of its own rather than passing its column
    through. Those passes cannot use the resolved expression: it names the fact
    table the measure was resolved against, and the plan may not select from it.

    The star planner records the anchored measures, whose aggregate reads a
    conformed ``GROUP BY`` subquery in place of the foreign fact; it leaves the
    rest out, since a plain star's CTE reuses the planner's own FROM and joins
    and the resolved expression is exactly right there. The CFL planner records
    *every* measure, because its outer query reads the composite CTE the union
    legs feed and none of the fact tables are in scope at all."""

    anchored_measures: dict[str, str] = field(default_factory=dict)
    """Measures whose expression is evaluated at a declared object's grain,
    mapped to that anchor object.

    Populated for measures carrying ``anchor:`` that actually read an
    independent fact, i.e. one the anchor cannot reach by joins. Consumed by the
    star planner, which conforms each such fact to the key it shares with the
    anchor and joins it on many-to-one. The conformed objects are deliberately
    absent from :attr:`measure_source_objects` and :attr:`required_objects`:
    they are subquery sources, not join requirements, and counting them would
    flip the query into a CFL plan whose ``UNION ALL`` is exactly what the
    anchor exists to avoid."""

    subquery_objects: set[str] = field(default_factory=set)
    """Data objects read inside a correlated ``EXISTS`` body.

    The subquery's own target, the hops its correlation path walked through,
    and anything a subquery filter reached through the join graph. Deliberately
    absent from :attr:`required_objects` — none of them is in the outer
    FROM/JOIN chain — but the compiled SQL does read them, so the freshness
    cache has to key on them all the same."""

    having_only_measures: set[str] = field(default_factory=set)
    """Measures auto-included by HAVING (not in ``select.measures``).

    Tracked so a future planner pass can optionally drop these from the
    final SELECT projection. Today they appear in output as an extra
    column, which keeps the SQL valid and the user gets a visual hint
    that the HAVING filter referenced an additional measure."""

    @property
    def dedup_targets(self) -> dict[str, str]:
        """Every measure the ``grain_dedup`` pass rewrites, component or not."""
        return {**self.dedup_measures, **self.dedup_components}

    @property
    def fact_tables(self) -> list[str]:
        if self.measure_source_objects:
            return sorted(self.measure_source_objects)
        return [self.base_object] if self.base_object else []

    def _components_of(self, measure: ResolvedMeasure) -> list[ResolvedMeasure]:
        """Components a measure reads, following nested derived metrics.

        Imported lazily: ``metric_expansion`` needs ``ResolvedMeasure`` for its
        annotations, so importing it at module scope here would be a cycle.
        """
        from orionbelt.compiler.metric_expansion import metric_leaf_components

        return metric_leaf_components(measure, self.metric_components)

    @property
    def has_totals(self) -> bool:
        """Check if any measure (direct or metric component) uses total or grain override."""
        for m in self.measures:
            if m.total or m.grain_override is not None:
                return True
            for comp in self._components_of(m):
                if comp.total or comp.grain_override is not None:
                    return True
        return False

    @property
    def has_grain_overrides(self) -> bool:
        """Check if any measure (direct or metric component) uses grain override."""
        for m in self.measures:
            if m.grain_override is not None:
                return True
            for comp in self._components_of(m):
                if comp.grain_override is not None:
                    return True
        return False

    @property
    def has_filter_context(self) -> bool:
        """Check if any measure (direct or metric component) has a filter context.

        Components count for the same reason they do in :attr:`has_totals`: a
        metric inlines their aggregates, so a context declared on one is a
        context this query has to honour. ``filter_wrap`` isolates only the
        directly selected ones, which is why a metric over such a component is
        refused in ``compiler.passes`` rather than compiled - reporting False
        here instead would skip the pass and answer the query-filtered number
        under the unfiltered measure's name.
        """
        if any(m.filter_context is not None for m in self.measures):
            return True
        # Every component, not only the leaves a metric inlines:
        # ``metric_leaf_components`` stops at a cumulative / window /
        # period-over-period metric, because that one is computed by its own
        # wrapper rather than substituted into the formula. Its *base measure*
        # is still a measure this query has to compute, and a context declared
        # on it is still one to honour - a derived metric over a window metric
        # over a filter-contexted measure hid one exactly there, and the
        # window's CTE was built under the query's WHERE.
        return any(c.filter_context is not None for c in self.metric_components.values())

    @property
    def has_cumulative(self) -> bool:
        """Check if any selected metric is cumulative."""
        return any(m.is_cumulative for m in self.measures)

    @property
    def has_pop(self) -> bool:
        """Check if any selected metric is period-over-period."""
        return any(m.is_pop for m in self.measures)

    @property
    def has_window(self) -> bool:
        """Check if any selected metric is a window (rank/lag/lead/ntile/...)."""
        return any(m.is_window for m in self.measures)


@dataclass
class _ResolutionContext:
    """Mutable state accumulated during query resolution."""

    model: SemanticModel
    errors: list[SemanticError] = field(default_factory=list)
    global_columns: dict[str, tuple[str, str]] = field(default_factory=dict)
    result: ResolvedQuery = field(default_factory=ResolvedQuery)
    joined_objects: set[str] = field(default_factory=set)
    graph: JoinGraph | None = None
    # Dialect-aware qualifier used by the EXISTS filter operator to render
    # its correlated subquery's FROM clause. ``None`` falls back to
    # ``obj.qualified_code`` (unquoted ``database.schema.code``), which is
    # only safe for engines that tolerate unquoted three-part identifiers
    # (e.g. DuckDB) — production code paths thread the dialect's
    # ``format_table_ref``.
    qualify_table: Callable[[DataObject], str] | None = None


def _resolve_effective_grain(grain: GrainOverride, query_dims: list[str]) -> list[str]:
    """Compute the effective grain dimensions for a measure grain override."""
    if grain.mode == GrainMode.FIXED:
        if grain.keep_only:
            return [d for d in grain.keep_only if d in query_dims]
        return list(grain.include)
    # RELATIVE mode
    result = [d for d in query_dims if d not in grain.exclude]
    result.extend(d for d in grain.include if d not in result)
    return result


class QueryResolver:
    """Resolves a QueryObject + SemanticModel into a ResolvedQuery."""

    def resolve(
        self,
        query: QueryObject,
        model: SemanticModel,
        qualify_table: Callable[[DataObject], str] | None = None,
    ) -> ResolvedQuery:
        ctx = _ResolutionContext(
            model=model,
            result=ResolvedQuery(
                limit=query.limit,
                offset=query.offset,
                use_path_names=list(query.use_path_names),
                allow_fan_out=query.allow_fan_out,
                is_raw=query.select.is_raw,
                distinct=query.select.distinct,
                grouping=query.grouping,
            ),
            qualify_table=qualify_table,
        )

        # Build global column lookup: col_name → (object_name, source_column)
        for obj_name, obj in model.data_objects.items():
            for col_name, col_obj in obj.columns.items():
                ctx.global_columns[col_name] = (obj_name, col_obj.code)

        if query.select.is_raw:
            # Raw mode: project physical columns, no aggregation.
            for ref in query.select.fields:
                self._resolve_raw_field(ctx, ref)
        else:
            # Aggregate mode (default).
            # 1. Resolve dimensions (string or coalesce group).
            # Coalesce groups expand into their constituent dimensions, each
            # tagged with the same coalesce_alias so the CFL outer wrapper can
            # emit COALESCE(d1, d2, ...) AS <alias>.
            for dim_entry in query.select.dimensions:
                if isinstance(dim_entry, CoalesceDimension):
                    self._resolve_coalesce_dimension(ctx, dim_entry, ctx.result.coalesce_aliases)
                else:
                    self._append_resolved_dimension(ctx, dim_entry)

            # 2. Resolve measures and track their source objects
            for measure_name in query.select.measures:
                resolved_meas = self._resolve_measure(ctx, measure_name)
                if resolved_meas:
                    ctx.result.measures.append(resolved_meas)
                    source_objs = self._get_measure_source_objects(ctx, measure_name)
                    ctx.result.measure_source_objects.update(source_objs)
                    ctx.result.required_objects.update(source_objs)
                    ctx.result.required_objects.update(
                        self._get_measure_join_objects(ctx, measure_name)
                    )

            # 2.5. Auto-include measures referenced by HAVING but not by SELECT.
            # Without this, codegen emits a HAVING clause that references an
            # alias for a column the SELECT doesn't project — every database
            # rejects the SQL with a "must appear in GROUP BY" binder error.
            # Routing this through the regular measure-resolution path also
            # updates ``measure_source_objects`` so the multi-fact CFL trigger
            # below sees the HAVING-only measure's source.
            existing_measure_names = {m.name for m in ctx.result.measures}
            for ref in self._collect_having_measure_refs(query, model):
                if ref in existing_measure_names:
                    continue
                resolved_meas = self._resolve_measure(ctx, ref)
                if resolved_meas is None:
                    continue
                ctx.result.measures.append(resolved_meas)
                ctx.result.having_only_measures.add(ref)
                existing_measure_names.add(ref)
                source_objs = self._get_measure_source_objects(ctx, ref)
                ctx.result.measure_source_objects.update(source_objs)
                ctx.result.required_objects.update(source_objs)
                ctx.result.required_objects.update(self._get_measure_join_objects(ctx, ref))

        # 3. Determine base object (the one with most joins / most measures).
        # WHERE filters are resolved much later, so the objects they reference
        # are collected up front — the base has to be able to reach them, or
        # the filter is silently dropped as unreachable further down.
        where_filter_objects = self._collect_where_filter_objects(query, model)
        ctx.result.base_object = self._select_base_object(ctx, where_filter_objects)
        if ctx.result.base_object:
            ctx.result.required_objects.add(ctx.result.base_object)

        # An anchored measure pins the base, which bypasses the re-anchoring that
        # normally makes a filter's data object reachable. A predicate on a fact
        # the anchor only reaches by conforming cannot be honoured where it
        # stands: the fact is aggregated to the shared key before the expression
        # is evaluated, so the predicate would compare a per-key total rather
        # than choose rows. Restricting it properly means a WHERE inside the
        # conformed subquery, which is not built.
        #
        # Refused rather than dropped. Filters on an unreachable object are
        # skipped silently elsewhere, which is tolerable when the object is
        # merely absent - here the query names a real fact the plan does read,
        # and skipping returned unfiltered totals with nothing to say so.
        # Static model filters count, and count for more: they are documented
        # as applied to every query, so dropping one silently widens every
        # result the model ever returns. Collected here rather than in
        # ``_collect_where_filter_objects`` so base-object selection keeps the
        # behaviour it has for models with no anchored measure.
        self._reject_filters_on_conformed_objects(
            ctx,
            where_filter_objects | {mf.data_object for mf in model.filters},
        )

        # Detect multi-fact: CFL is needed only when measure source objects
        # span multiple independent fact tables.
        if len(ctx.result.measure_source_objects) > 1:
            graph = JoinGraph(model, use_path_names=query.use_path_names or None)
            reachable = graph.descendants(ctx.result.base_object)
            unreachable = ctx.result.measure_source_objects - reachable - {ctx.result.base_object}
            if unreachable:
                ctx.result.requires_cfl = True

        # Dimension-only queries: when dimensions span independent branches,
        # join through intermediate bridge/fact tables (no CFL needed).
        # Add intermediate tables from the join steps to required_objects
        # so the star schema planner includes them.
        if not ctx.result.measure_source_objects and ctx.result.dimensions:
            dim_objects = {d.object_name for d in ctx.result.dimensions}
            if not dim_objects <= {ctx.result.base_object}:
                graph = JoinGraph(model, use_path_names=query.use_path_names or None)
                steps = graph.find_join_path(
                    {ctx.result.base_object},
                    dim_objects,
                    via_constraints=ctx.result.via_constraints or None,
                )
                for step in steps:
                    ctx.result.required_objects.add(step.from_object)
                    ctx.result.required_objects.add(step.to_object)

        # Raw mode: detect multi-fact (fields span objects unreachable from
        # the base via directed joins). The pipeline rejects this case for
        # now — raw CFL is a planned follow-up.
        if ctx.result.is_raw and ctx.result.base_object:
            field_objects = {f.object_name for f in ctx.result.fields}
            if len(field_objects) > 1:
                graph = JoinGraph(model, use_path_names=query.use_path_names or None)
                reachable = graph.descendants(ctx.result.base_object)
                unreachable = field_objects - reachable - {ctx.result.base_object}
                if unreachable:
                    ctx.result.requires_cfl = True

        # Validate dimensionsExclude constraints
        if query.dimensions_exclude:
            if query.select.measures:
                ctx.errors.append(
                    SemanticError(
                        code="DIMENSIONS_EXCLUDE_WITH_MEASURES",
                        message="dimensionsExclude cannot be combined with measures",
                        path="select",
                    )
                )
            elif len(ctx.result.dimensions) < 2:
                ctx.errors.append(
                    SemanticError(
                        code="DIMENSIONS_EXCLUDE_INSUFFICIENT",
                        message="dimensionsExclude requires at least 2 dimensions",
                        path="select.dimensions",
                    )
                )
            else:
                ctx.result.dimensions_exclude = True

        # Every object this *query* names, including the ones only a predicate
        # does. A WHERE filter is resolved much later, so a guard reading
        # ``required_objects`` alone sees none of them - and a filter is exactly
        # how a nested object reaches a query that projects nothing from it.
        #
        # A static model filter is deliberately not counted. It is a property of
        # the model rather than of the query, and one naming an object this plan
        # cannot reach is documented as skipped rather than fatal
        # (``test_unreachable_filter_silently_ignored``). Counting them made a
        # single nested static filter refuse every multi-fact query in the
        # model, including the ones that never go near it.
        self._reject_unsupported_nested_shapes(
            ctx, ctx.result.required_objects | where_filter_objects
        )

        # 4. Validate usePathNames before building join graph
        self._validate_use_path_names(ctx, query.use_path_names)

        # 5. Resolve join paths
        ctx.graph = JoinGraph(model, use_path_names=query.use_path_names or None)
        if ctx.result.base_object and len(ctx.result.required_objects) > 1:
            ambiguous: dict[str, list[list[str]]] = {}
            ctx.result.join_steps = ctx.graph.find_join_path(
                {ctx.result.base_object},
                ctx.result.required_objects,
                via_constraints=ctx.result.via_constraints or None,
                ambiguous=ambiguous,
            )
            # A dimension the query reaches by two equally close routes is two
            # different roles of one data object, and they select different
            # rows. Refused rather than picked — the same stance the filter
            # path takes, since a projected dimension is no more guessable.
            for object_name, routes in sorted(ambiguous.items()):
                names = (
                    ", ".join(
                        f"'{dim.name}'"
                        for dim in ctx.result.dimensions
                        if dim.object_name == object_name
                    )
                    or f"'{object_name}'"
                )
                ctx.errors.append(
                    SemanticError(
                        code="AMBIGUOUS_JOIN_PATH",
                        message=(
                            f"{names} is on '{object_name}', which this query reaches "
                            f"equally well by more than one route "
                            f"({', '.join(f'via {path[-2]!r}' for path in routes)}). "
                            f"Those are different roles of the same data object and "
                            f"they select different rows."
                        ),
                        path="select.dimensions",
                        hint=(
                            "Say which one is meant: declare a data object per role "
                            "over the same table and select from that, or give the "
                            "dimension a 'via:' waypoint naming the object the join "
                            "must traverse."
                        ),
                    )
                )

        # Build set of all objects present in the query's join graph
        if ctx.result.base_object:
            ctx.joined_objects.add(ctx.result.base_object)
        for step in ctx.result.join_steps:
            ctx.joined_objects.add(step.to_object)

        # Detect required objects that the star-schema planner cannot reach.
        # Many-to-one joins are forward-only (reverse traversal would inflate
        # the base table), so a required object that's only reachable via a
        # reverse m-to-1 hop is unreachable.  Raise a clear error rather than
        # silently producing wrong SQL.  CFL legs are validated separately.
        if ctx.result.base_object and not ctx.result.requires_cfl:
            unreachable = ctx.result.required_objects - ctx.joined_objects
            for unreachable_name in sorted(unreachable):
                ctx.errors.append(
                    SemanticError(
                        code="UNREACHABLE_REQUIRED_OBJECT",
                        message=(
                            f"Data object '{unreachable_name}' is required by the query but "
                            f"cannot be reached from base '{ctx.result.base_object}' via "
                            f"directed joins. Many-to-one joins are forward-only; reverse "
                            f"traversal would inflate row counts. Add an explicit join from "
                            f"'{ctx.result.base_object}' (or an intermediate object) to "
                            f"'{unreachable_name}', or split the query so each fact is "
                            f"queried independently."
                        ),
                        path="select",
                    )
                )

        # 5b. Inject static model filters — always applied as WHERE conditions
        static_exprs: list[Expr] = []
        for mf in model.filters:
            static_filter = self._resolve_static_filter(ctx, mf)
            if static_filter:
                ctx.result.where_filters.append(static_filter)
                static_exprs.append(static_filter.expression)

        # 6. Classify filters — skip query-time duplicates of static filters
        for qfi in query.where:
            resolved_filter = self._resolve_filter_item(ctx, qfi, is_having=False)
            if resolved_filter and resolved_filter.expression not in static_exprs:
                ctx.result.where_filters.append(resolved_filter)

        for qfi in query.having:
            resolved_filter = self._resolve_filter_item(ctx, qfi, is_having=True)
            if resolved_filter:
                ctx.result.having_filters.append(resolved_filter)

        # 7. Resolve order by — must reference a dimension or measure in SELECT
        select_count = len(ctx.result.dimensions) + len(ctx.result.measures)
        for ob in query.order_by:
            expr = self._resolve_order_by_field(ctx, ob.field, select_count)
            if expr:
                ctx.result.order_by_exprs.append((expr, ob.direction == "desc", ob.nulls))

        # 8. ROLLUP / CUBE: backfill NULLS FIRST on any explicit ORDER BY entry
        # that didn't specify a NULLs position. Subtotal and grand-total rows
        # carry NULLs in the rolled-up group-by columns, and BI tools expect
        # those totals at the top of the result — not interleaved with details.
        if ctx.result.grouping is not None and ctx.result.order_by_exprs:
            ctx.result.order_by_exprs = [
                (expr, desc, NullsPosition.FIRST if nulls is None else nulls)
                for expr, desc, nulls in ctx.result.order_by_exprs
            ]

        # 9. Auto-order — when no explicit ORDER BY, append ORDER BY over all
        # SELECT dimensions (or raw fields) under two conditions:
        #   (a) LIMIT is set: cache hashes on compiled SQL; without ORDER BY
        #       ``LIMIT N`` returns any N rows, freezing one arbitrary slice.
        #   (b) ROLLUP / CUBE: subtotal layout is otherwise unpredictable.
        # ROLLUP / CUBE defaults to NULLS FIRST (totals at the top).
        # Aggregate-only queries (no dims, no fields) are already single-row
        # deterministic — skip.
        needs_auto_order = not ctx.result.order_by_exprs and (
            ctx.result.limit is not None or ctx.result.grouping is not None
        )
        if needs_auto_order:
            nulls_default = NullsPosition.FIRST if ctx.result.grouping is not None else None
            if ctx.result.is_raw and ctx.result.fields:
                for f in ctx.result.fields:
                    ctx.result.order_by_exprs.append(
                        (ColumnRef(name=f.alias), False, nulls_default)
                    )
            elif ctx.result.dimensions:
                for dim in ctx.result.dimensions:
                    ctx.result.order_by_exprs.append(
                        (ColumnRef(name=dim.name), False, nulls_default)
                    )

        if ctx.errors:
            raise ResolutionError(ctx.errors)

        return ctx.result

    # -- raw mode fields -----------------------------------------------------

    def _resolve_raw_field(self, ctx: _ResolutionContext, ref: str) -> None:
        """Resolve a ``DataObject.Column`` reference for raw-mode projection.

        Errors are accumulated in the resolution context (raised at the end).
        """
        raw_resolution.resolve_raw_field(self, ctx, ref)

    # -- dimensions ----------------------------------------------------------

    def _append_resolved_dimension(
        self,
        ctx: _ResolutionContext,
        dim_str: str,
        coalesce_alias: str | None = None,
    ) -> ResolvedDimension | None:
        """Resolve a single dimension string and append it to the result."""
        dim_ref = DimensionRef.parse(dim_str)
        resolved_dim = self._resolve_dimension(ctx, dim_ref)
        if resolved_dim is None:
            return None
        dim_def = ctx.model.dimensions.get(dim_ref.name)
        if dim_def and dim_def.via:
            resolved_dim.via = dim_def.via
            ctx.result.required_objects.add(dim_def.via)
            ctx.result.via_constraints[resolved_dim.object_name] = dim_def.via
        if coalesce_alias is not None:
            resolved_dim.coalesce_alias = coalesce_alias
        ctx.result.dimensions.append(resolved_dim)
        ctx.result.required_objects.add(resolved_dim.object_name)
        # A computed column may read a column of another data object, which the
        # plan then has to join — the expression is inlined into the SELECT list
        # and would otherwise name an alias nothing in the FROM chain binds.
        ctx.result.required_objects.update(
            ctx.model.column_reference_objects(resolved_dim.object_name, resolved_dim.column_name)
        )
        return resolved_dim

    def _resolve_coalesce_dimension(
        self,
        ctx: _ResolutionContext,
        coalesce: CoalesceDimension,
        seen_aliases: set[str],
    ) -> None:
        """Expand a coalesce group into its constituent resolved dimensions.

        Validates: at least 2 members, alias is unique within the query and
        does not collide with an existing dimension/measure name, all members
        resolve to the same abstract column type.
        """
        alias = coalesce.alias
        if not alias:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_MISSING_ALIAS",
                    message="Coalesce dimension requires a non-empty 'as' alias",
                    path="select.dimensions",
                )
            )
            return
        if alias in seen_aliases:
            ctx.errors.append(
                SemanticError(
                    code="DUPLICATE_COALESCE_ALIAS",
                    message=f"Duplicate coalesce alias '{alias}' in this query",
                    path="select.dimensions",
                )
            )
            return
        if alias in ctx.model.dimensions or alias in ctx.model.effective_measures:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_ALIAS_COLLISION",
                    message=(
                        f"Coalesce alias '{alias}' collides with an existing "
                        f"model dimension or measure name"
                    ),
                    path="select.dimensions",
                )
            )
            return
        if len(coalesce.coalesce) < 2:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_TOO_FEW_MEMBERS",
                    message=(
                        f"Coalesce '{alias}' requires at least 2 dimensions "
                        f"(got {len(coalesce.coalesce)})"
                    ),
                    path="select.dimensions",
                )
            )
            return
        seen_aliases.add(alias)

        # Resolve each member with the alias tag; verify type compatibility.
        member_types: set[str] = set()
        for member in coalesce.coalesce:
            resolved = self._append_resolved_dimension(ctx, member, coalesce_alias=alias)
            if resolved:
                dim_def = ctx.model.dimensions.get(member)
                if dim_def:
                    member_types.add(dim_def.result_type.value)
        if len(member_types) > 1:
            ctx.errors.append(
                SemanticError(
                    code="COALESCE_TYPE_MISMATCH",
                    message=(
                        f"Coalesce '{alias}' members have incompatible result types: "
                        f"{sorted(member_types)}"
                    ),
                    path="select.dimensions",
                )
            )

    def _resolve_dimension(
        self, ctx: _ResolutionContext, ref: DimensionRef
    ) -> ResolvedDimension | None:
        """Resolve a dimension reference to its physical column."""
        dim = ctx.model.dimensions.get(ref.name)
        if dim is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_DIMENSION",
                    message=f"Unknown dimension '{ref.name}'",
                    path="select.dimensions",
                )
            )
            return None

        obj_name = dim.view
        col_name = dim.column
        obj = ctx.model.data_objects.get(obj_name)
        if obj is None:
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_DATA_OBJECT",
                    message=f"Dimension '{ref.name}' references unknown data object '{obj_name}'",
                )
            )
            return None

        vf = obj.columns.get(col_name)
        source_col = vf.code if vf else col_name

        grain = ref.grain or dim.time_grain
        if grain is not None and not self._grain_fits_the_column(ctx, ref, dim, vf, grain):
            return None
        if grain is not None and not self._grain_survives_the_cast(ctx, ref, dim, grain):
            return None

        return ResolvedDimension(
            name=ref.name,
            object_name=obj_name,
            column_name=col_name,
            source_column=source_col,
            grain=grain,
        )

    @staticmethod
    def _grain_fits_the_column(
        ctx: _ResolutionContext,
        ref: DimensionRef,
        dim: Dimension,
        column: DataObjectColumn | None,
        grain: TimeGrain,
    ) -> bool:
        """Refuse a grain over a column that carries no date to truncate.

        A grain compiles to ``date_trunc(grain, column)``, which the engine
        refuses over text: measured on DuckDB, ``"Name:hour"`` over a string
        column compiled and then died in the binder with "No function matches
        the given name and argument types 'date_trunc(STRING_LITERAL,
        VARCHAR)'". The model validator refuses a declared ``timeGrain`` over
        such a column for exactly that reason, and a query can name a grain the
        model never declared, so the same rule is read here - an error naming
        the dimension and the column beats one naming generated SQL.

        A column the model does not define is left alone: its type is unknown
        here, and the reference itself is reported elsewhere.
        """
        if column is None or column.abstract_type in DATE_BEARING_TYPES:
            return True
        ctx.errors.append(
            SemanticError(
                code="TIME_GRAIN_ON_NON_TEMPORAL",
                message=(
                    f"Dimension '{ref.name}' is asked for at grain '{grain.value}' "
                    f"but underlying column '{dim.view}.{dim.column}' has "
                    f"abstractType '{column.abstract_type.value}'. A time grain "
                    f"requires the column to be date, timestamp, or timestamp_tz. "
                    f"Ask for the dimension without a grain, fix the column's "
                    f"abstractType, or define a computed column with to_date()."
                ),
                path="select.dimensions",
            )
        )
        return False

    @staticmethod
    def _grain_survives_the_cast(
        ctx: _ResolutionContext, ref: DimensionRef, dim: Dimension, grain: TimeGrain
    ) -> bool:
        """Refuse a grain the dimension's declared type cannot hold.

        ``make_dimension_expr`` casts a grained dimension to its declared
        ``resultType``, in the GROUP BY as well as the projection, so a
        declaration narrower than the grain merges buckets and changes the
        measures rather than relabelling the column. The model validator refuses
        that combination at load, but a query writes its own: ``Occurred:hour``
        names a grain the dimension never declared, so a dimension declaring
        ``date`` -- perfectly valid, with no ``timeGrain`` of its own -- answered
        two rows where three were asked for, the two hours of one day summed
        into one. The model is not at fault there and cannot be checked for it;
        the query is, and this is where it is read.

        ``time_tz`` is refused here too, and it is the one case that merges
        nothing: OBML has no cast target for it, so the value is left alone and
        only the label is wrong. Refused all the same, because a grain always
        carries a date and that declaration cannot describe one.
        """
        declared = dim.result_type
        if result_type_holds_grain(grain, declared):
            return True
        keeps = "timestamp" if grain in SUB_DAY_GRAINS else "date or timestamp"
        asked = "asked for at" if ref.grain is not None else "grouped by"
        if declared in CASTABLE_TEMPORAL_TYPES:
            cost = (
                "The cast is applied in the GROUP BY as well, so buckets would "
                "merge and the measures would change without an error."
            )
        else:
            cost = (
                f"A grain always carries a date, and OBML has no cast target for "
                f"'{declared.value}', so the dimension would answer a date-bearing "
                f"value under a label for a time."
            )
        ctx.errors.append(
            SemanticError(
                code="RESULT_TYPE_LOSES_GRAIN",
                message=(
                    f"Dimension '{ref.name}' is {asked} grain '{grain.value}' but "
                    f"declares resultType '{declared.value}', which cannot hold it. "
                    f"{cost} Declare {keeps}, or ask for the grain the type implies."
                ),
                path="select.dimensions",
            )
        )
        return False

    # -- measures & metrics --------------------------------------------------

    def _resolve_measure(self, ctx: _ResolutionContext, name: str) -> ResolvedMeasure | None:
        """Resolve a measure name to its aggregate expression."""
        measure = ctx.model.effective_measures.get(name)
        if measure is None:
            metric = ctx.model.metrics.get(name)
            if metric:
                return self._resolve_metric(ctx, name, metric)
            ctx.errors.append(
                SemanticError(
                    code="UNKNOWN_MEASURE",
                    message=f"Unknown measure '{name}'",
                    path="select.measures",
                )
            )
            return None

        expr = self._build_measure_expr(ctx, measure)
        grain_override = measure.grain
        effective_grain: list[str] | None = None
        if grain_override is not None:
            query_dim_names = [d.name for d in ctx.result.dimensions]
            effective_grain = _resolve_effective_grain(grain_override, query_dim_names)
            if effective_grain is not None and not set(effective_grain) <= set(query_dim_names):
                bad = sorted(set(effective_grain) - set(query_dim_names))
                ctx.errors.append(
                    SemanticError(
                        code="GRAIN_NOT_SUBSET",
                        message=(
                            f"Measure '{name}' grain {bad} is not a subset of "
                            f"query dimensions {query_dim_names}. "
                            f"This would cause row multiplication."
                        ),
                        path="select.measures",
                    )
                )
        return ResolvedMeasure(
            name=name,
            aggregation=measure.aggregation,
            expression=expr,
            is_expression=measure.expression is not None,
            total=measure.total,
            default_value=measure.default_value,
            grain_override=grain_override,
            effective_grain=effective_grain,
            filter_context=measure.filter_context,
        )

    def _build_measure_expr(self, ctx: _ResolutionContext, measure: Measure) -> Expr:
        """Build the aggregate expression for a measure."""
        # Engine-delegated aggregation (Databricks Metric View). Emit
        # ``MEASURE("<label>")`` literally — there's no source column
        # to read; the engine resolves the aggregation by name. Dialect
        # support is enforced downstream by ``_check_aggregation_supported``.
        if measure.aggregation == AggregationType.MEASURE:
            return FunctionCall(
                name="MEASURE",
                args=[ColumnRef(name=measure.name, table=None)],
            )
        if measure.expression:
            return self._expand_expression(ctx, measure)

        # Build column references for all columns. Routes through
        # ``make_column_expr`` so a measure column that points at a
        # computed (``expression:``) column inlines the template body
        # — without this, ``count_distinct`` over an ``expression:``
        # column would emit ``COUNT(DISTINCT "obj"."")`` (zero-length
        # identifier, DB error).
        # Whether a cast is coming, and whether it is a numeric one. A boolean
        # source only has to become a number when it is about to be read as
        # one; ``None`` here means the measure passes its value through.
        numeric_output = _reads_a_number(measure, ctx.model.settings)

        args: list[Expr] = []
        if measure.columns:
            for ref in measure.columns:
                obj_name = ref.view or ""
                col_name = ref.column or ""
                # A column-less ref (``dataObject`` set, ``column`` empty) anchors the
                # measure on the object without naming a column — used by the
                # synthesized row-count measure to emit ``COUNT(*)`` while still
                # contributing the anchor to source-object resolution.
                if not col_name:
                    continue
                obj = ctx.model.data_objects.get(obj_name)
                if obj and col_name in obj.columns:
                    col_expr = make_column_expr(ctx.model, obj_name, col_name)
                    if numeric_output:
                        col_expr = _flag_as_number(col_expr)
                    args.append(col_expr)
                else:
                    args.append(ColumnRef(name=col_name, table=obj_name))
        if not args:
            args = [Literal.number(1)]

        agg = measure.aggregation.upper()
        distinct = measure.distinct
        if agg == "COUNT_DISTINCT":
            agg = "COUNT"
            distinct = True

        # LISTAGG: attach separator and optional ordering
        separator: str | None = None
        order_by: list[OrderByItem] = []
        if agg == "LISTAGG":
            separator = measure.delimiter if measure.delimiter is not None else ","
            if measure.within_group:
                wg = measure.within_group
                wg_obj_name = wg.column.view or ""
                wg_col_name = wg.column.column or ""
                wg_obj = ctx.model.data_objects.get(wg_obj_name)
                if wg_obj and wg_col_name in wg_obj.columns:
                    wg_expr: Expr = make_column_expr(ctx.model, wg_obj_name, wg_col_name)
                else:
                    wg_expr = ColumnRef(name=wg_col_name, table=wg_obj_name)
                order_by = [
                    OrderByItem(expr=wg_expr, desc=wg.order.upper() == "DESC"),
                ]

        result = FunctionCall(
            name=agg,
            args=args,
            distinct=distinct,
            order_by=order_by,
            separator=separator,
        )
        return self._apply_measure_default(
            measure, self._apply_measure_filters(ctx, measure, result)
        )

    def _expand_expression(self, ctx: _ResolutionContext, measure: Measure) -> Expr:
        """Expand a measure expression with ``{[DataObject].[Column]}`` refs into AST."""
        formula = measure.expression or ""
        agg = measure.aggregation.upper()

        tokens = tokenize_measure_expression(formula, ctx.model)
        # The tokenizer resolves {[Object].[Column]} straight to a physical
        # ref, so the query zone has to be applied here as it is for a column
        # a dimension names: otherwise one column means two instants depending
        # on how the query reached it.
        inner = apply_query_timezone(parse_expression(tokens), ctx.model)
        # The same rule the ``columns:`` form gets: two spellings of one
        # measure, so a boolean reaches a numeric output as a number either
        # way. Scoping it to the other branch left this one failing.
        if _reads_a_number(measure, ctx.model.settings):
            inner = _flag_as_number(inner)

        distinct = measure.distinct
        if agg == "COUNT_DISTINCT":
            agg = "COUNT"
            distinct = True

        result = FunctionCall(
            name=agg,
            args=[inner],
            distinct=distinct,
        )
        return self._apply_measure_default(
            measure, self._apply_measure_filters(ctx, measure, result)
        )

    @staticmethod
    def _apply_measure_default(measure: Measure, expr: Expr) -> Expr:
        """Wrap an aggregate in its declared empty-set value.

        Outside the aggregate rather than inside: ``COALESCE(SUM(x), 0)``
        answers 0 when the aggregate saw nothing, where ``SUM(COALESCE(x, 0))``
        would answer 0 for a row whose value is missing — a different claim.

        Emitted for every dialect, which is the point: an aggregate over an
        empty row set is NULL in standard SQL and 0 on ClickHouse, so a model
        that says what it wants no longer depends on which engine runs it.
        """
        if measure.default_value is None:
            return expr
        return FunctionCall(name="COALESCE", args=[expr, Literal(value=measure.default_value)])

    @staticmethod
    def _apply_measure_filters(
        ctx: _ResolutionContext, measure: Measure, func: FunctionCall
    ) -> FunctionCall:
        """Wrap aggregate args with CASE WHEN if the measure has filters."""
        if not measure.filters:
            return func
        condition = build_measure_filter_condition(measure.filters, ctx.model, ctx.errors)
        if condition is None:
            return func
        wrapped_args: list[Expr] = [CaseExpr(when_clauses=[(condition, arg)]) for arg in func.args]
        return FunctionCall(
            name=func.name,
            args=wrapped_args,
            distinct=func.distinct,
            order_by=func.order_by,
            separator=func.separator,
        )

    def _resolve_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a metric to its combined expression."""
        return metric_resolution.resolve_metric(self, ctx, name, metric)

    def _validate_partition_dimensions(
        self,
        ctx: _ResolutionContext,
        metric_name: str,
        partition_by: list[str],
        path_template: str,
    ) -> bool:
        return metric_resolution.validate_partition_dimensions(
            self, ctx, metric_name, partition_by, path_template
        )

    def _resolve_window_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a window metric (rank/lag/lead/ntile/first_value/last_value)."""
        return metric_resolution.resolve_window_metric(self, ctx, name, metric)

    def _resolve_derived_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a derived metric to its combined expression."""
        return metric_resolution.resolve_derived_metric(self, ctx, name, metric)

    def _resolve_cumulative_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a cumulative metric referencing an existing measure."""
        return metric_resolution.resolve_cumulative_metric(self, ctx, name, metric)

    def _resolve_pop_metric(
        self, ctx: _ResolutionContext, name: str, metric: Metric
    ) -> ResolvedMeasure | None:
        """Resolve a period-over-period metric."""
        return metric_resolution.resolve_pop_metric(self, ctx, name, metric)

    def _collect_having_measure_refs(self, query: QueryObject, model: SemanticModel) -> list[str]:
        """Collect measure/metric names referenced in any HAVING filter.

        Walks ``query.having`` recursively (each entry is a
        ``QueryFilter`` or a ``QueryFilterGroup``) and returns the
        ordered, de-duplicated list of ``field`` values that name a
        known measure or metric in the model. Order is preserved for
        deterministic resolution; duplicates are dropped on first sight.
        """

        seen: set[str] = set()
        out: list[str] = []
        measure_names = model.effective_measures

        def _visit(item: QueryFilterItem) -> None:
            if isinstance(item, QueryFilterGroup):
                for child in item.filters:
                    _visit(child)
                return
            field = item.field
            if field in seen:
                return
            if field in measure_names or field in model.metrics:
                seen.add(field)
                out.append(field)

        for entry in query.having:
            _visit(entry)
        return out

    def _get_measure_join_objects(self, ctx: _ResolutionContext, name: str) -> set[str]:
        """Objects a measure needs *joined* without being sourced from them.

        A ``withinGroup`` column becomes the aggregate's ``ORDER BY``, so it has
        to resolve — but it contributes no value to the measure. Kept out of
        ``measure_source_objects`` for that reason: that set drives CFL
        detection and the explain output's fact-table list, and a LISTAGG's sort
        column is neither a fact nor a source.

        Without this the object is never joined and the compiler emits SQL that
        binds to nothing:

            SELECT LISTAGG("Products"."id", ',' ORDER BY "Sales"."quantity")
            FROM "products" AS "Products"          -- Sales never joined

        which every engine rejects at execution time.

        A column the measure reads may itself be computed from a column of
        another object, which lands here for the same reason and with the same
        care about ``measure_source_objects``: the object supplies part of an
        expression evaluated per fact row, not a second fact to union.
        """
        result: set[str] = set()

        if name in ctx.model.effective_measures:
            return ctx.model.measure_join_objects(name)

        metric = ctx.model.metrics.get(name)
        if metric is not None:
            if metric.measure:
                result.update(self._get_measure_join_objects(ctx, metric.measure))
            if metric.expression:
                for ref_name in re.findall(r"\{\[([^\]]+)\]\}", metric.expression):
                    result.update(self._get_measure_join_objects(ctx, ref_name))
        return result

    def _reject_filters_on_conformed_objects(
        self, ctx: _ResolutionContext, filter_objects: set[str]
    ) -> None:
        """Refuse a WHERE predicate on a fact an anchored measure only conforms.

        Covers both the query's ``where`` and the model's static ``filters:``.
        Either one resolves against an object the plan reads only as an
        aggregate, so neither can choose rows, and both were being skipped.
        """
        if not ctx.result.anchored_measures or not filter_objects:
            return
        conformed: set[str] = set()
        for name in ctx.result.anchored_measures:
            measure = ctx.model.effective_measures.get(name)
            if measure is not None:
                conformed |= anchored_conformed_objects(
                    ctx.model, measure, ctx.result.use_path_names
                )
        constrained = sorted(filter_objects & conformed)
        if not constrained:
            return
        listed = ", ".join(f"'{name}'" for name in constrained)
        ctx.errors.append(
            SemanticError(
                code="FILTER_ON_CONFORMED_OBJECT",
                message=(
                    f"A filter constrains {listed}, which an anchored measure reaches "
                    f"only by aggregating it to a shared key. The filter would compare a "
                    f"per-key total rather than choose rows, so it cannot be applied "
                    f"where it stands. This covers the query's own filters and the "
                    f"model's static ones alike."
                ),
                path="where",
                hint=(
                    "Filter on a data object the anchor reaches directly, or query the "
                    f"anchored measure separately from the restriction on {listed}."
                ),
            )
        )

    def _record_anchor(
        self,
        ctx: _ResolutionContext,
        name: str,
        measure: Measure,
        result: set[str],
    ) -> None:
        """Settle the grain a cross-fact measure is evaluated at, or refuse.

        A declared ``anchor:`` settles it. Otherwise the facts must share
        exactly one directly-joined object: several are several different
        answers, and the measure has to say which it means.
        """
        conformed = measure.source_objects
        anchor = effective_anchor(ctx.model, measure, ctx.result.use_path_names)
        if anchor is None:
            candidates = conform_key_candidates(ctx.model, measure, ctx.result.use_path_names)
            if len(candidates) != 1:
                ctx.errors.append(
                    SemanticError(
                        code="ANCHOR_REQUIRED_AMBIGUOUS_KEY",
                        message=(
                            f"Measure '{name}' reads {', '.join(sorted(conformed))}, which no "
                            f"join path reaches together, so each has to be aggregated to a key "
                            f"they share. "
                            + (
                                f"They share {', '.join(candidates)}, and conforming at each "
                                f"gives a different answer."
                                if candidates
                                else "They share no directly joined data object."
                            )
                        ),
                        path=f"measures.{name}",
                        hint=(
                            "Set anchor: on the measure to the data object whose grain the "
                            "expression should be evaluated at - one of the facts it reads, or "
                            "a data object all of them join to."
                            if candidates
                            else "Join the data objects to a common one, or read them in "
                            "separate measures and combine those with a metric."
                        ),
                    )
                )
                return
            anchor = candidates[0]
            ctx.result.warnings.append(
                warning(
                    code=WarningCode.CONFORMED_GRAIN_ASSUMED,
                    message=(
                        f"Measure '{name}' reads {', '.join(sorted(conformed))}, which no join "
                        f"path reaches together. Each was aggregated to '{anchor}', the only "
                        f"data object they both join to, and the expression evaluated once per "
                        f"'{anchor}' row."
                    ),
                    hint=(
                        "Set anchor: to evaluate per row of one of the facts instead, which "
                        "changes AVG / MIN / MAX (though not SUM)."
                    ),
                    context={"measure": name, "conformedAt": anchor},
                )
            )

        ctx.result.anchored_measures[name] = anchor
        # Only the objects actually conformed leave the join requirements. An
        # object the anchor *can* reach is read directly, by an ordinary join,
        # so dropping it here left the expression naming a table the plan no
        # longer joined - visible only when nothing else in the query happened
        # to require it.
        result -= anchored_conformed_objects(ctx.model, measure, ctx.result.use_path_names)
        result.add(anchor)

    def _get_measure_source_objects(self, ctx: _ResolutionContext, name: str) -> set[str]:
        """Extract all source data objects for a measure or metric."""
        result: set[str] = set()

        measure = ctx.model.effective_measures.get(name)
        if measure:
            for cref in measure.columns:
                if cref.view:
                    result.add(cref.view)
            if measure.expression:
                for obj_name, _col_name in find_qualified_refs(measure.expression):
                    result.add(obj_name)
            for fi in measure.filters:
                collect_measure_filter_objects(fi, result)
            # An anchored measure's independent facts are conformed into
            # subqueries by the planner, not joined. Reporting them here would
            # add them to the join requirements and, worse, make the multi-fact
            # check below flip the query into a CFL plan - whose UNION ALL puts
            # the two facts' columns on different rows, which is the exact
            # arrangement the anchor exists to avoid.
            # Gated on "needs a grain" rather than "has one": a measure that
            # needs one and cannot be given one has to be refused, and treating
            # that as "needs nothing" is what let it through to a CFL leg that
            # projected no such column.
            if needs_conforming(ctx.model, measure, ctx.result.use_path_names):
                self._record_anchor(ctx, name, measure, result)
            return result

        metric = ctx.model.metrics.get(name)
        if metric:
            if metric.type == MetricType.CUMULATIVE and metric.measure:
                # Cumulative metric: source objects come from the referenced measure
                result.update(self._get_measure_source_objects(ctx, metric.measure))
            elif metric.type == MetricType.WINDOW and metric.measure:
                # Window metric: source objects come from the referenced measure
                result.update(self._get_measure_source_objects(ctx, metric.measure))
            elif metric.expression:
                # Derived or PoP metric: parse expression for measure references
                measure_refs = re.findall(r"\{\[([^\]]+)\]\}", metric.expression)
                for ref_name in measure_refs:
                    result.update(self._get_measure_source_objects(ctx, ref_name))

        return result

    # -- base object selection -----------------------------------------------

    @staticmethod
    def _collect_where_filter_objects(query: QueryObject, model: SemanticModel) -> set[str]:
        """Data objects the query's WHERE clause references.

        Walks ``query.where`` recursively; each ``field`` is either a dimension
        name or a qualified ``DataObject.Column``. Measure names are skipped —
        those are HAVING predicates, evaluated after aggregation rather than
        joined into the FROM.

        ``EXISTS`` / ``NONEXISTS`` targets are skipped too: they compile to a
        correlated subquery, not a join, so they place no reachability demand on
        the base object.

        A predicate on a computed column demands whatever objects its expression
        reads as well — the predicate is only as reachable as the columns it
        compares.
        """
        found: set[str] = set()
        measure_names = model.effective_measures

        def visit(item: QueryFilterItem) -> None:
            if isinstance(item, QueryFilterGroup):
                for child in item.filters:
                    visit(child)
                return
            field = item.field
            if not field or field in measure_names or field in model.metrics:
                return
            dimension = model.dimensions.get(field)
            if dimension is not None:
                if dimension.view:
                    found.add(dimension.view)
                    found.update(model.column_reference_objects(dimension.view, dimension.column))
                return
            if "." in field:
                object_name, _, column_name = field.partition(".")
                object_name, column_name = object_name.strip(), column_name.strip()
                if object_name in model.data_objects:
                    found.add(object_name)
                    found.update(model.column_reference_objects(object_name, column_name))

        for entry in query.where:
            visit(entry)
        return found

    @staticmethod
    def _reanchor_if_unreachable(
        ctx: _ResolutionContext, best: str, filter_objects: set[str]
    ) -> str:
        """Re-anchor the base when the chosen measure source cannot reach the query.

        Anchoring on a measure's own source object is right whenever that object
        is the fact table. It is wrong when the measure lives on a *dimension*
        table: joins are declared many-to-one and traversed forward-only, so a
        base of ``Customers`` can reach nothing, and a query grouping
        ``Avg Customer Age`` by ``Category`` fails with
        ``UNREACHABLE_REQUIRED_OBJECT`` even though ``Sales`` joins to both.

        Such a query is not multi-fact — it is single-fact viewed from the wrong
        end. Re-anchoring on the common root that reaches every required object
        makes it plan as an ordinary star; the measure then sits on the replicated
        side of a forward join, where ``compiler.grain_dedup`` aggregates it over
        deduplicated rows.

        Deliberately narrow, so this can only turn an error into a result and
        never re-plan a query that already works:

        * Only with **one** measure source object. Multi-fact queries keep their
          existing base so CFL detection (which runs on it straight after) is
          untouched.
        * Only when that base genuinely cannot reach the rest — the case that
          errors today.
        * Only when a common root actually exists; otherwise the original base
          is returned and the existing error still fires.

        *filter_objects* are the data objects the query's WHERE clause names.
        They are not in ``required_objects`` yet — filters resolve much later —
        but the base still has to reach them: a WHERE on an unreachable object
        is silently dropped downstream, which would answer a different question
        than the one asked. Static model filters are deliberately excluded; those
        are declared "apply where relevant", so they must not drag the base
        towards a table the query never mentioned.
        """
        if len(ctx.result.measure_source_objects) != 1:
            return best

        wanted = ctx.result.required_objects | filter_objects
        remaining = wanted - {best}
        if not remaining:
            return best

        graph = JoinGraph(ctx.model, use_path_names=ctx.result.use_path_names or None)
        if remaining <= graph.descendants(best):
            return best

        root = graph.find_common_root(wanted | {best})
        if not root:
            return best
        # ``find_common_root`` falls back to a Steiner centre when no single
        # ancestor covers everything, so its answer is a best effort rather than
        # a guarantee. Re-anchor only on a root that genuinely reaches the whole
        # query; otherwise keep the original base and let the existing
        # unreachable error or filter skip stand.
        if wanted <= graph.descendants(root) | {root}:
            return root
        return best

    def _reject_unsupported_nested_shapes(
        self, ctx: _ResolutionContext, touched_objects: set[str]
    ) -> None:
        """Refuse the two nested shapes the planner cannot render yet.

        **A nested object in a union leg.** Each CFL leg picks its own root and
        builds its own FROM, and neither knows how to carry an unnest, so the
        leg would select from a table the object does not have. Two nested
        objects in one query are meant to *become* that union; this is what has
        to land first.

        **A nested object whose parent is itself nested.** An unnest names its
        parent's array column, and where the parent is an element rather than a
        row that reference is not a column at all: Snowflake's ``FLATTEN`` row
        exposes the element under ``value``, so the array is ``p.value:"Parts"``
        and ``p."Parts"`` does not compile, and MySQL's ``JSON_TABLE`` projects
        only the scalar columns it was told to extract, so the array is not
        there to read. Reaching it needs a per-dialect parent-element access
        that is designed and measured on its own; until then the shape is
        refused rather than emitted as SQL two engines reject and five have
        never been asked.

        Both are refused rather than compiled, because the alternative is SQL
        naming a table that does not exist or - worse, and this is what the
        filter case actually did - a plausible number from unfiltered rows.
        """
        multi_fact = ctx.result.requires_cfl or ctx.result.dimensions_exclude
        for name in sorted(touched_objects):
            obj = ctx.model.data_objects.get(name)
            source = obj.nested_in if obj is not None else None
            if source is None:
                continue
            parent = ctx.model.data_objects.get(source.data_object)
            if parent is not None and parent.is_nested:
                ctx.errors.append(
                    SemanticError(
                        code="NESTED_WITHIN_NESTED_UNSUPPORTED",
                        message=(
                            f"Data object '{name}' is nested in '{source.data_object}', which "
                            f"is itself nested. An unnest names its parent's array column, and "
                            f"where the parent is an array element rather than a row, that "
                            f"reference is spelled differently on every engine - so this "
                            f"compiles to SQL some of them reject."
                        ),
                        path=f"dataObjects.{name}.nestedIn",
                        hint=(
                            "Nest the object directly in the table's own object, or read the "
                            "inner array through a flattening view by declaring 'code' "
                            "alongside 'nestedIn'."
                        ),
                    )
                )
                continue
            if multi_fact:
                ctx.errors.append(
                    SemanticError(
                        code="NESTED_OBJECT_IN_MULTI_FACT",
                        message=(
                            f"Data object '{name}' takes its rows by unnesting "
                            f"'{source.data_object}.{source.column}', and this "
                            f"query is planned as a union of independent facts, where each "
                            f"leg selects from a table of its own. A nested object has none."
                        ),
                        path="select",
                        hint=(
                            "Query the nested object with measures from its own parent only, "
                            "or read it through a flattening view by declaring 'code' "
                            "alongside 'nestedIn'."
                        ),
                    )
                )

    def _select_base_object(
        self, ctx: _ResolutionContext, filter_objects: set[str] | None = None
    ) -> str:
        """Select the base (fact) object, which is never a nested one.

        A ``nestedIn`` object has no table: its rows are an array column on its
        parent, reached by an unnest that names the parent. Every route into
        this function can nominate one anyway - it is a measure source like any
        other, and the "prefer the object with the most joins" fallback does not
        look at where rows come from - so the answer is mapped up to the nearest
        ancestor a FROM clause can name, which reaches everything the nested
        object does and more.
        """
        return ctx.model.unnest_root(self._choose_base_object(ctx, filter_objects))

    def _choose_base_object(
        self, ctx: _ResolutionContext, filter_objects: set[str] | None = None
    ) -> str:
        """Prefer measure source objects with the most joins."""
        # An anchored measure has already been told which grain to run at, and
        # the planner joins its conformed subqueries against that object. Moving
        # the base elsewhere leaves those joins referencing a table no longer in
        # the FROM. Where the anchor cannot reach the query's other objects the
        # query is genuinely unanswerable at that grain, and the reachability
        # check below says so - which is more use than a binder error.
        anchors = set(ctx.result.anchored_measures.values())
        if len(anchors) == 1:
            return next(iter(anchors))

        if ctx.result.measure_source_objects:
            # Among the measure sources, prefer one that can actually reach the
            # others. "Most joins" alone picks the busiest fact, which is not
            # the same thing: a measure reading Sales and Returns, where the
            # declared join runs Returns -> Sales, has to be based on Returns,
            # because Sales reaches Returns only by traversing that join
            # backwards. Basing it on Sales left the join path empty and the SQL
            # said AVG("Sales"."salesamount" * "Returns"."returnquantity") over
            # a FROM with no Returns in it.
            #
            # Where nothing reaches everything the facts are genuinely
            # independent, and the fallback below leaves CFL and the conformed
            # anchor path to deal with them.
            #
            # Only a measure that *by itself* reads several objects constrains
            # the base, because only it needs them on one row. Constraining on
            # the union of every measure's objects instead re-bases ordinary
            # multi-fact queries: two independent measures would base at
            # whichever fact reaches the other, and the reached fact's rows
            # would then repeat once per row of the base. That is the fanout
            # those queries stay on CFL to avoid.
            spanning: set[str] = set()
            for resolved_measure in ctx.result.measures:
                model_measure = ctx.model.effective_measures.get(resolved_measure.name)
                if model_measure is None:
                    continue
                objects = model_measure.source_objects
                if len(objects) > 1:
                    spanning |= objects

            sources = ctx.result.measure_source_objects
            candidates = sorted(sources)
            if spanning:
                graph = JoinGraph(ctx.model, use_path_names=ctx.result.use_path_names or None)
                candidates = [
                    name for name in candidates if spanning <= (graph.descendants(name) | {name})
                ] or sorted(sources)

            best = ""
            best_joins = -1
            for obj_name in candidates:
                obj = ctx.model.data_objects.get(obj_name)
                n = len(obj.joins) if obj else 0
                if n > best_joins:
                    best = obj_name
                    best_joins = n
            if best:
                return self._reanchor_if_unreachable(ctx, best, filter_objects or set())

        # Dimension-only: use JoinGraph to find the deepest ancestor
        # (possibly an intermediate fact/bridge table) that can reach
        # all required dimension objects via directed join paths.
        # (See ``_reanchor_if_unreachable`` — the same idea, applied when a
        # measure pinned the base to an object that cannot reach the rest.)
        if len(ctx.result.required_objects) > 1:
            graph = JoinGraph(ctx.model, use_path_names=ctx.result.use_path_names or None)
            root = graph.find_common_root(ctx.result.required_objects)
            if root:
                return root

        for obj_name in sorted(ctx.result.required_objects):
            obj = ctx.model.data_objects.get(obj_name)
            if obj and obj.joins:
                return obj_name

        if ctx.result.required_objects:
            return next(iter(sorted(ctx.result.required_objects)))
        if ctx.model.data_objects:
            return next(iter(ctx.model.data_objects))
        return ""

    # -- usePathNames validation ---------------------------------------------

    def _validate_use_path_names(
        self, ctx: _ResolutionContext, use_path_names: list[UsePathName]
    ) -> None:
        """Validate usePathNames references."""
        for upn in use_path_names:
            if upn.source not in ctx.model.data_objects:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_DATA_OBJECT",
                        message=f"usePathNames references unknown data object '{upn.source}'",
                        path="usePathNames",
                    )
                )
                continue
            if upn.target not in ctx.model.data_objects:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_DATA_OBJECT",
                        message=f"usePathNames references unknown data object '{upn.target}'",
                        path="usePathNames",
                    )
                )
                continue
            source_obj = ctx.model.data_objects[upn.source]
            found = any(
                j.join_to == upn.target and j.secondary and j.path_name == upn.path_name
                for j in source_obj.joins
            )
            if not found:
                ctx.errors.append(
                    SemanticError(
                        code="UNKNOWN_PATH_NAME",
                        message=(
                            f"No secondary join with pathName '{upn.path_name}' "
                            f"from '{upn.source}' to '{upn.target}'"
                        ),
                        path="usePathNames",
                    )
                )

    # -- static model filters ------------------------------------------------

    def _resolve_static_filter(
        self, ctx: _ResolutionContext, mf: ModelFilter
    ) -> ResolvedFilter | None:
        """Resolve a static model filter to a physical WHERE expression.

        Silently skips filters on data objects that are unreachable from the
        query's join graph — they are simply irrelevant to the current query.
        """
        return filter_resolution.resolve_static_filter(self, ctx, mf)

    # -- filters -------------------------------------------------------------

    def _resolve_filter_object(
        self,
        ctx: _ResolutionContext,
        obj_name: str,
        filter_path: str,
        _field_label: str,
    ) -> bool:
        """Ensure *obj_name* is joined; auto-extend if reachable.

        Silently skips filters on unreachable data objects — they are
        irrelevant to the current query.
        """
        return filter_resolution.resolve_filter_object(
            self, ctx, obj_name, filter_path, _field_label
        )

    def _resolve_filter_item(
        self, ctx: _ResolutionContext, item: QueryFilterItem, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a filter item (leaf or group) to a physical expression."""
        return filter_resolution.resolve_filter_item(self, ctx, item, is_having=is_having)

    def _resolve_filter_group(
        self, ctx: _ResolutionContext, group: QueryFilterGroup, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a filter group recursively, combining with AND/OR."""
        return filter_resolution.resolve_filter_group(self, ctx, group, is_having=is_having)

    def _resolve_filter(
        self, ctx: _ResolutionContext, qf: QueryFilter, *, is_having: bool
    ) -> ResolvedFilter | None:
        """Resolve a query filter to a physical expression.

        Filter fields can reference:
        1. A dimension name (e.g. ``"Order Priority"``)
        2. A qualified column ``"DataObject.Column"`` (e.g. ``"Orders.Order Priority"``)
        3. For HAVING filters, a measure name (e.g. ``"Revenue"``)

        If the referenced data object is reachable but not yet joined, the
        join path is auto-extended.
        """
        return filter_resolution.resolve_filter(self, ctx, qf, is_having=is_having)

    # -- order by ------------------------------------------------------------

    def _resolve_order_by_field(
        self, ctx: _ResolutionContext, field_name: str, select_count: int
    ) -> Expr | None:
        """Resolve an order-by field to its expression."""
        return filter_resolution.resolve_order_by_field(self, ctx, field_name, select_count)


class ResolutionError(Exception):
    """Raised when query resolution encounters errors."""

    def __init__(self, errors: list[SemanticError]) -> None:
        self.errors = errors
        messages = "; ".join(e.message for e in errors)
        super().__init__(f"Resolution errors: {messages}")
