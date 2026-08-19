"""Filter expression builder — converts QueryFilter and MeasureFilter to AST expressions."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict

from orionbelt.ast.nodes import (
    Between,
    BinaryOp,
    Exists,
    Expr,
    From,
    FunctionCall,
    InList,
    IsNull,
    Join,
    JoinType,
    Literal,
    RegexMatch,
    RelativeDateRange,
    Select,
    UnaryOp,
    Unnest,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import FilterOperator, QueryFilter, UsePathName
from orionbelt.models.semantic import (
    DataObject,
    DataType,
    FilterLogic,
    FilterValue,
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    SemanticModel,
)

if TYPE_CHECKING:
    from orionbelt.compiler.graph import JoinGraph


def _escape_like(val: str) -> str:
    """Escape SQL LIKE wildcard characters (% and _) with backslash."""
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class RelativeFilterParsed(TypedDict):
    unit: str
    count: int
    direction: str
    include_current: bool


def build_filter_expr(col: Expr, qf: QueryFilter, errors: list[SemanticError]) -> Expr | None:
    """Build a filter expression from operator and value."""
    op = qf.op
    val = qf.value

    match op:
        case FilterOperator.EQUALS | FilterOperator.EQ:
            return BinaryOp(left=col, op="=", right=Literal(value=val))
        case FilterOperator.NOT_EQUALS | FilterOperator.NEQ:
            return BinaryOp(left=col, op="<>", right=Literal(value=val))
        case FilterOperator.GT | FilterOperator.GREATER:
            return BinaryOp(left=col, op=">", right=Literal(value=val))
        case FilterOperator.GTE | FilterOperator.GREATER_EQ:
            return BinaryOp(left=col, op=">=", right=Literal(value=val))
        case FilterOperator.LT | FilterOperator.LESS:
            return BinaryOp(left=col, op="<", right=Literal(value=val))
        case FilterOperator.LTE | FilterOperator.LESS_EQ:
            return BinaryOp(left=col, op="<=", right=Literal(value=val))
        case FilterOperator.IN_LIST | FilterOperator.IN:
            vals: list[Expr] = (
                [Literal(value=v) for v in val] if isinstance(val, list) else [Literal(value=val)]
            )
            return InList(expr=col, values=vals)
        case FilterOperator.NOT_IN_LIST | FilterOperator.NOT_IN:
            not_vals: list[Expr] = (
                [Literal(value=v) for v in val] if isinstance(val, list) else [Literal(value=val)]
            )
            return InList(expr=col, values=not_vals, negated=True)
        case FilterOperator.SET | FilterOperator.IS_NOT_NULL:
            return IsNull(expr=col, negated=True)
        case FilterOperator.NOT_SET | FilterOperator.IS_NULL:
            return IsNull(expr=col, negated=False)
        case FilterOperator.CONTAINS:
            return BinaryOp(
                left=col,
                op="LIKE",
                right=Literal.string(f"%{_escape_like(str(val))}%"),
            )
        case FilterOperator.NOT_CONTAINS:
            return BinaryOp(
                left=col,
                op="NOT LIKE",
                right=Literal.string(f"%{_escape_like(str(val))}%"),
            )
        case FilterOperator.STARTS_WITH:
            return BinaryOp(
                left=col,
                op="LIKE",
                right=Literal.string(f"{_escape_like(str(val))}%"),
            )
        case FilterOperator.ENDS_WITH:
            return BinaryOp(
                left=col,
                op="LIKE",
                right=Literal.string(f"%{_escape_like(str(val))}"),
            )
        case FilterOperator.LIKE:
            return BinaryOp(left=col, op="LIKE", right=Literal.string(str(val)))
        case FilterOperator.NOT_LIKE:
            return BinaryOp(left=col, op="NOT LIKE", right=Literal.string(str(val)))
        case FilterOperator.BETWEEN:
            if isinstance(val, list) and len(val) >= 2:
                return Between(
                    expr=col,
                    low=Literal(value=val[0]),
                    high=Literal(value=val[1]),
                )
            return BinaryOp(left=col, op="=", right=Literal(value=val))
        case FilterOperator.NOT_BETWEEN:
            if isinstance(val, list) and len(val) >= 2:
                return Between(
                    expr=col,
                    low=Literal(value=val[0]),
                    high=Literal(value=val[1]),
                    negated=True,
                )
            return BinaryOp(left=col, op="<>", right=Literal(value=val))
        case FilterOperator.REGEX | FilterOperator.NOT_REGEX:
            if not isinstance(val, str):
                errors.append(
                    SemanticError(
                        code="INVALID_FILTER_VALUE",
                        message=f"'{op}' requires a string pattern, got {type(val).__name__}",
                        path="filters",
                    )
                )
                return None
            return RegexMatch(column=col, pattern=val, negated=(op == FilterOperator.NOT_REGEX))
        case FilterOperator.BLANK:
            # NULL OR TRIM(col) = ''
            return BinaryOp(
                left=IsNull(expr=col),
                op="OR",
                right=BinaryOp(
                    left=FunctionCall(name="TRIM", args=[col]),
                    op="=",
                    right=Literal.string(""),
                ),
            )
        case FilterOperator.NOT_BLANK:
            return BinaryOp(
                left=IsNull(expr=col, negated=True),
                op="AND",
                right=BinaryOp(
                    left=FunctionCall(name="TRIM", args=[col]),
                    op="<>",
                    right=Literal.string(""),
                ),
            )
        case FilterOperator.LENGTH_EQ | FilterOperator.LENGTH_GT | FilterOperator.LENGTH_LT:
            if not isinstance(val, int) or isinstance(val, bool):
                errors.append(
                    SemanticError(
                        code="INVALID_FILTER_VALUE",
                        message=f"'{op}' requires an integer length, got {type(val).__name__}",
                        path="filters",
                    )
                )
                return None
            cmp = {
                FilterOperator.LENGTH_EQ: "=",
                FilterOperator.LENGTH_GT: ">",
                FilterOperator.LENGTH_LT: "<",
            }[op]
            return BinaryOp(
                left=FunctionCall(name="LENGTH", args=[col]),
                op=cmp,
                right=Literal.number(val),
            )
        case FilterOperator.RELATIVE:
            relative = parse_relative_filter(val, errors, field=qf.field)
            if relative is None:
                return None
            return RelativeDateRange(
                column=col,
                unit=relative["unit"],
                count=relative["count"],
                direction=relative["direction"],
                include_current=relative["include_current"],
            )
        case FilterOperator.EXISTS | FilterOperator.NONEXISTS:
            # exists/nonexists need model + subject_object + qualify_table to
            # build the correlated subquery — call build_exists_filter_expr()
            # directly instead. This branch only fires when a caller routes
            # an exists filter through build_filter_expr() by mistake.
            errors.append(
                SemanticError(
                    code="INVALID_FILTER_OPERATOR",
                    message=(
                        f"'{op}' must be dispatched via build_exists_filter_expr "
                        "with the model and subject object — not build_filter_expr."
                    ),
                    path="filters",
                )
            )
            return None
        case _:
            errors.append(
                SemanticError(
                    code="INVALID_FILTER_OPERATOR",
                    message=f"Unsupported filter operator '{op}'",
                    path="filters",
                )
            )
            return None


def parse_relative_filter(
    value: object, errors: list[SemanticError], field: str
) -> RelativeFilterParsed | None:
    """Parse and validate a relative date filter value."""
    if not isinstance(value, dict):
        errors.append(
            SemanticError(
                code="INVALID_RELATIVE_FILTER",
                message=(
                    f"Relative filter for '{field}' must be an object "
                    "with keys {unit, count, direction?, include_current?}"
                ),
                path="filters",
            )
        )
        return None

    unit = value.get("unit")
    count = value.get("count")
    direction = value.get("direction", "past")
    include_current = value.get("include_current", value.get("includeCurrent", True))

    if not isinstance(unit, str):
        errors.append(
            SemanticError(
                code="INVALID_RELATIVE_FILTER",
                message=f"Relative filter for '{field}' requires string 'unit'",
                path="filters",
            )
        )
        return None
    unit = unit.lower()
    if unit not in {"day", "week", "month", "year"}:
        errors.append(
            SemanticError(
                code="INVALID_RELATIVE_FILTER",
                message=f"Relative filter for '{field}' has unsupported unit '{unit}'",
                path="filters",
            )
        )
        return None
    if not isinstance(count, int) or count <= 0:
        errors.append(
            SemanticError(
                code="INVALID_RELATIVE_FILTER",
                message=f"Relative filter for '{field}' requires positive integer 'count'",
                path="filters",
            )
        )
        return None
    if direction not in {"past", "future"}:
        errors.append(
            SemanticError(
                code="INVALID_RELATIVE_FILTER",
                message=f"Relative filter for '{field}' has invalid direction '{direction}'",
                path="filters",
            )
        )
        return None
    if not isinstance(include_current, bool):
        errors.append(
            SemanticError(
                code="INVALID_RELATIVE_FILTER",
                message=f"Relative filter for '{field}' has non-boolean include_current",
                path="filters",
            )
        )
        return None

    return {
        "unit": unit,
        "count": count,
        "direction": direction,
        "include_current": include_current,
    }


# ---------------------------------------------------------------------------
# Measure-level filter compilation (MeasureFilter → CASE WHEN condition)
# ---------------------------------------------------------------------------


def _extract_filter_value(fv: FilterValue) -> str | int | float | bool | None:
    """Pick the concrete value from a typed FilterValue."""
    if fv.is_null:
        return None
    match fv.data_type:
        case DataType.STRING | DataType.JSON:
            return fv.value_string
        case DataType.INT:
            return fv.value_int
        case DataType.FLOAT:
            return fv.value_float
        case DataType.DATE | DataType.TIMESTAMP:
            return fv.value_date
        case DataType.BOOLEAN:
            return fv.value_boolean
    return fv.value_string  # fallback


def _build_single_measure_filter(
    mf: MeasureFilter,
    model: SemanticModel,
    errors: list[SemanticError],
) -> Expr | None:
    """Convert a single MeasureFilter leaf to an AST condition expression."""
    if not mf.column or not mf.column.view or not mf.column.column:
        errors.append(
            SemanticError(
                code="INVALID_MEASURE_FILTER",
                message="Measure filter must specify column.dataObject and column.column",
                path="measures",
            )
        )
        return None

    obj = model.data_objects.get(mf.column.view)
    if not obj:
        errors.append(
            SemanticError(
                code="UNKNOWN_FILTER_DATA_OBJECT",
                message=f"Measure filter references unknown data object '{mf.column.view}'",
                path="measures",
            )
        )
        return None

    obj_col = obj.columns.get(mf.column.column)
    if not obj_col:
        errors.append(
            SemanticError(
                code="UNKNOWN_FILTER_COLUMN",
                message=(
                    f"Measure filter references unknown column "
                    f"'{mf.column.column}' in '{mf.column.view}'"
                ),
                path="measures",
            )
        )
        return None

    # Route through ``make_column_expr`` so a measure-level filter on a
    # computed (``expression:``) column inlines the template body.
    # Without this, a filter like ``WHERE "Has Financial Row" = false``
    # where ``Has Financial Row`` is computed compiled to ``(1 = FALSE)``
    # (operator-does-not-exist at the DB).
    from orionbelt.compiler.resolution import make_column_expr

    col: Expr = make_column_expr(model, mf.column.view, mf.column.column)
    op_str = mf.operator.lower()

    # Extract values
    values = [_extract_filter_value(fv) for fv in mf.values]

    match op_str:
        case "equals":
            return BinaryOp(left=col, op="=", right=Literal(value=values[0] if values else None))
        case "notequals":
            return BinaryOp(left=col, op="<>", right=Literal(value=values[0] if values else None))
        case "gt":
            return BinaryOp(left=col, op=">", right=Literal(value=values[0] if values else None))
        case "gte":
            return BinaryOp(left=col, op=">=", right=Literal(value=values[0] if values else None))
        case "lt":
            return BinaryOp(left=col, op="<", right=Literal(value=values[0] if values else None))
        case "lte":
            return BinaryOp(left=col, op="<=", right=Literal(value=values[0] if values else None))
        case "inlist":
            return InList(expr=col, values=[Literal(value=v) for v in values])
        case "notinlist":
            return InList(expr=col, values=[Literal(value=v) for v in values], negated=True)
        case "set":
            return IsNull(expr=col, negated=True)
        case "notset":
            return IsNull(expr=col, negated=False)
        case "contains":
            v = values[0] if values else ""
            return BinaryOp(left=col, op="LIKE", right=Literal.string(f"%{_escape_like(str(v))}%"))
        case "notcontains":
            v = values[0] if values else ""
            return BinaryOp(
                left=col, op="NOT LIKE", right=Literal.string(f"%{_escape_like(str(v))}%")
            )
        case "starts_with":
            v = values[0] if values else ""
            return BinaryOp(left=col, op="LIKE", right=Literal.string(f"{_escape_like(str(v))}%"))
        case "ends_with":
            v = values[0] if values else ""
            return BinaryOp(left=col, op="LIKE", right=Literal.string(f"%{_escape_like(str(v))}"))
        case "like":
            v = values[0] if values else ""
            return BinaryOp(left=col, op="LIKE", right=Literal.string(str(v)))
        case "notlike":
            v = values[0] if values else ""
            return BinaryOp(left=col, op="NOT LIKE", right=Literal.string(str(v)))
        case "between":
            if len(values) >= 2:
                return Between(
                    expr=col,
                    low=Literal(value=values[0]),
                    high=Literal(value=values[1]),
                )
            return BinaryOp(left=col, op="=", right=Literal(value=values[0] if values else None))
        case "notbetween":
            if len(values) >= 2:
                return Between(
                    expr=col,
                    low=Literal(value=values[0]),
                    high=Literal(value=values[1]),
                    negated=True,
                )
            return BinaryOp(left=col, op="<>", right=Literal(value=values[0] if values else None))
        case _:
            errors.append(
                SemanticError(
                    code="INVALID_MEASURE_FILTER_OPERATOR",
                    message=f"Unsupported measure filter operator '{mf.operator}'",
                    path="measures",
                )
            )
            return None


def _build_measure_filter_item(
    item: MeasureFilterItem,
    model: SemanticModel,
    errors: list[SemanticError],
) -> Expr | None:
    """Recursively build an AST condition from a MeasureFilter or MeasureFilterGroup."""
    if isinstance(item, MeasureFilter):
        return _build_single_measure_filter(item, model, errors)

    # MeasureFilterGroup — recurse children, combine with logic
    child_exprs: list[Expr] = []
    for child in item.filters:
        expr = _build_measure_filter_item(child, model, errors)
        if expr is not None:
            child_exprs.append(expr)

    if not child_exprs:
        return None

    op = "AND" if item.logic == FilterLogic.AND else "OR"
    combined: Expr = child_exprs[0]
    for expr in child_exprs[1:]:
        combined = BinaryOp(left=combined, op=op, right=expr)

    if item.negated:
        combined = UnaryOp(op="NOT", operand=combined)

    return combined


def build_measure_filter_condition(
    filters: list[MeasureFilterItem],
    model: SemanticModel,
    errors: list[SemanticError],
) -> Expr | None:
    """Build a combined AST condition from a measure's filter list.

    Top-level filters are combined with AND. Returns ``None`` if no valid
    conditions could be built.
    """
    parts: list[Expr] = []
    for item in filters:
        expr = _build_measure_filter_item(item, model, errors)
        if expr is not None:
            parts.append(expr)

    if not parts:
        return None

    combined: Expr = parts[0]
    for expr in parts[1:]:
        combined = BinaryOp(left=combined, op="AND", right=expr)
    return combined


def collect_measure_filter_objects(item: MeasureFilterItem, objects: set[str]) -> None:
    """Recursively collect data object names referenced by measure filters."""
    if isinstance(item, MeasureFilter):
        if item.column and item.column.view:
            objects.add(item.column.view)
    elif isinstance(item, MeasureFilterGroup):
        for child in item.filters:
            collect_measure_filter_objects(child, objects)


# ---------------------------------------------------------------------------
# exists / nonexists — correlated subquery filter
# ---------------------------------------------------------------------------


def _nested_subquery_error(name: str, obj: DataObject) -> SemanticError:
    """A correlated subquery cannot reach a nested data object.

    There is no table for its FROM clause and no key for its correlation
    predicate: the rows exist only inside their parent's, which is exactly what
    makes the ordinary join to it work without either. Returned as a structured
    error because ``qualify_table`` would otherwise raise
    ``UnrenderableDataObjectError`` from inside the builder, which routers hand
    back as a 500 rather than the 422 every other subquery mistake gets.
    """
    source = obj.nested_in
    where = (
        f"unnesting '{source.data_object}.{source.column}'" if source else "unnesting its parent"
    )
    return SemanticError(
        code="NESTED_OBJECT_IN_SUBQUERY",
        message=(
            f"EXISTS cannot select from '{name}': it takes its rows by {where}, so it has "
            f"no table of its own and no key to correlate on."
        ),
        path="filters",
        hint=(
            "Filter on the nested object's columns directly - the query joins it through "
            "its parent - or read it through a flattening view by declaring 'code' "
            "alongside 'nestedIn'."
        ),
    )


def _resolve_subquery_filter_field(
    field: str,
    model: SemanticModel,
    target_object: str,
    errors: list[SemanticError],
) -> tuple[str, str] | None:
    """Resolve a subquery filter's ``field`` to a ``(data object, column)`` pair.

    Accepts, in precedence order, a column of the subquery's own data object,
    a dimension name, or a qualified ``DataObject.Column`` — the same
    vocabulary the outer ``where`` understands, minus measure names: a
    correlated subquery filters rows, not aggregates.

    Column-of-the-target wins over a same-named dimension so a model that
    grew a dimension shadowing a column keeps compiling to the same SQL.
    """
    target_obj = model.data_objects.get(target_object)
    if target_obj is not None and field in target_obj.columns:
        return target_object, field

    dim = model.dimensions.get(field)
    if dim is not None and dim.view:
        dim_obj = model.data_objects.get(dim.view)
        if dim_obj is not None and dim.column in dim_obj.columns:
            return dim.view, dim.column

    if "." in field:
        obj_name, _, col_name = field.partition(".")
        obj_name, col_name = obj_name.strip(), col_name.strip()
        obj = model.data_objects.get(obj_name)
        if obj is not None and col_name in obj.columns:
            return obj_name, col_name

    errors.append(
        SemanticError(
            code="UNKNOWN_SUBQUERY_FILTER_COLUMN",
            message=(
                f"Subquery filter references unknown column '{field}' on "
                f"'{target_object}' — use a column of that data object, a "
                f"dimension name, or a qualified 'DataObject.Column'"
            ),
            path="filters",
        )
    )
    return None


def _join_filter_object_into_subquery(
    ref_object: str,
    field: str,
    *,
    graph: JoinGraph,
    model: SemanticModel,
    subject_object: str,
    target_object: str,
    scope: set[str],
    joins: list[Join | Unnest],
    qualify_table: Callable[[DataObject], str],
    errors: list[SemanticError],
    read_through_expression: bool = False,
) -> bool:
    """Make *ref_object* addressable inside the ``EXISTS`` body.

    Objects already in *scope* — the subquery's own target and the hops the
    correlation path walked through — need nothing. Anything else is reached
    with the same walker the outer query uses (forward along many-to-one,
    either way along the row-preserving cardinalities), and the resulting
    INNER JOINs are appended to *joins* (and *scope*) in place.

    *read_through_expression* says *ref_object* is not where the field lives but
    something its computed column reads, which only changes how the failures are
    worded — the object has to be joined either way.

    Returns ``False`` (with a :class:`SemanticError` appended) when the object
    cannot be reached, or when reaching it would re-enter the correlation
    subject: an inner ``FROM``/``JOIN`` alias shadows the outer one, which
    would silently rebind the correlation predicate to the subquery's own rows.
    """
    if ref_object in scope:
        return True

    reaches = (
        f"reads '{ref_object}' through a computed column"
        if read_through_expression
        else f"is on '{ref_object}'"
    )

    if ref_object == subject_object:
        resolves = (
            f"reads '{ref_object}' through a computed column"
            if read_through_expression
            else f"resolves to '{ref_object}'"
        )
        errors.append(
            SemanticError(
                code="SUBQUERY_FILTER_OBJECT_NOT_JOINABLE",
                message=(
                    f"Subquery filter field '{field}' {resolves}, the subject "
                    f"of the correlation — filter it in the outer 'where' instead"
                ),
                path="filters",
            )
        )
        return False

    # Ask the walker itself rather than a separate reachability test: it is
    # what decides which hops are legal, so a second rule would only diverge.
    steps = graph.find_join_path(set(scope), {ref_object})
    if not steps:
        errors.append(
            SemanticError(
                code="UNREACHABLE_SUBQUERY_FILTER_OBJECT",
                message=(
                    f"Subquery filter field '{field}' {reaches}, which is not "
                    f"reachable from '{target_object}' — declare a 'joins:' block"
                ),
                path="filters",
            )
        )
        return False

    # ``JoinStep`` keeps from/to in the declared join direction, so the object
    # a step actually brings into the body is the far end of its *traversal*.
    joined_objects = [step.from_object if step.reversed else step.to_object for step in steps]

    if subject_object in joined_objects:
        errors.append(
            SemanticError(
                code="SUBQUERY_FILTER_OBJECT_NOT_JOINABLE",
                message=(
                    f"Subquery filter field '{field}' {reaches}, reachable only "
                    f"through '{subject_object}' — the subject of the "
                    f"correlation cannot be joined inside the subquery"
                ),
                path="filters",
            )
        )
        return False

    for step, joined_object in zip(steps, joined_objects, strict=True):
        if joined_object in scope:
            continue
        step_obj = model.data_objects.get(joined_object)
        if step_obj is None:
            continue
        joins.append(
            Join(
                join_type=JoinType.INNER,
                source=qualify_table(step_obj),
                alias=joined_object,
                on=graph.build_join_condition(step),
            )
        )
        scope.add(joined_object)
    return True


def build_exists_filter_expr(
    qf: QueryFilter,
    model: SemanticModel,
    subject_object: str,
    qualify_table: Callable[[DataObject], str],
    errors: list[SemanticError],
    touched_objects: set[str] | None = None,
) -> Expr | None:
    """Compile an ``exists`` / ``nonexists`` filter into an ``Exists`` AST node.

    The join path from ``subject_object`` to ``qf.subquery.dataObject`` is
    resolved by walking the model's existing ``joins:`` — the same machinery
    the query planner uses.  Single-hop is the common case; multi-hop paths
    are supported and emit INNER JOINs inside the subquery.

    Subquery filters resolve against the target's join graph, not only its own
    columns, so a semi-join can be windowed by a dimension one or more hops
    away; the joins that makes necessary are emitted inside the ``EXISTS``
    body.

    Every data object the body reads is added to *touched_objects* when given,
    so the caller can key the freshness cache on tables that never appear in
    the outer FROM/JOIN chain.

    Returns ``None`` and appends ``SemanticError``s on validation failure.
    """
    # Local import: resolution imports filters at module load.
    from orionbelt.compiler.graph import JoinGraph
    from orionbelt.compiler.resolution import make_column_expr

    sub = qf.subquery
    if sub is None:
        # Pydantic model_validator normally catches this; defensive guard.
        errors.append(
            SemanticError(
                code="INVALID_FILTER_OPERATOR",
                message=f"'{qf.op}' requires a 'subquery' object",
                path="filters",
            )
        )
        return None

    target_obj = model.data_objects.get(sub.data_object)
    if target_obj is None:
        errors.append(
            SemanticError(
                code="UNKNOWN_SUBQUERY_DATA_OBJECT",
                message=(f"Subquery references unknown data object '{sub.data_object}'"),
                path="filters",
            )
        )
        return None

    # A nested object has no table, so ``SELECT 1 FROM <it>`` cannot be written,
    # and no key to correlate on either - its rows exist only inside its
    # parent's. Refused here rather than at the FROM clause below, where
    # ``qualify_table`` raises an exception routed as a 500 instead of the
    # structured error every other subquery mistake gets. The containment edge
    # is what made this reachable: before it there was no path to walk, so the
    # query failed with NO_JOIN_PATH_TO_SUBQUERY by accident rather than on
    # purpose.
    if target_obj.is_nested:
        errors.append(_nested_subquery_error(sub.data_object, target_obj))
        return None

    subject_obj = model.data_objects.get(subject_object)
    if subject_obj is None:
        errors.append(
            SemanticError(
                code="UNKNOWN_FILTER_DATA_OBJECT",
                message=(f"Subquery subject references unknown data object '{subject_object}'"),
                path="filters",
            )
        )
        return None

    # If pathName is given, locate the secondary join. EXISTS is direction-
    # agnostic, so the join may be declared on either the subject or the
    # target side. ``UsePathName`` overrides are keyed by the side that
    # *declares* the join (matching JoinGraph's edge construction).
    overrides: list[UsePathName] | None = None
    if sub.path_name is not None:
        declaring_obj: str | None = None
        other_obj: str | None = None
        for j in subject_obj.joins:
            if j.join_to == sub.data_object and j.secondary and j.path_name == sub.path_name:
                declaring_obj, other_obj = subject_object, sub.data_object
                break
        if declaring_obj is None:
            for j in target_obj.joins:
                if j.join_to == subject_object and j.secondary and j.path_name == sub.path_name:
                    declaring_obj, other_obj = sub.data_object, subject_object
                    break
        if declaring_obj is None or other_obj is None:
            errors.append(
                SemanticError(
                    code="UNKNOWN_PATH_NAME",
                    message=(
                        f"No secondary join with pathName '{sub.path_name}' "
                        f"between '{subject_object}' and '{sub.data_object}'"
                    ),
                    path="filters",
                )
            )
            return None
        overrides = [
            UsePathName(
                source=declaring_obj,
                target=other_obj,
                path_name=sub.path_name,
            )
        ]

    graph = JoinGraph(model, use_path_names=overrides)

    # EXISTS correlates without multiplying outer rows, so cardinality
    # direction is irrelevant — use the undirected walker.
    path = graph.find_join_path_undirected(subject_object, sub.data_object)
    if not path:
        errors.append(
            SemanticError(
                code="NO_JOIN_PATH_TO_SUBQUERY",
                message=(
                    f"No join path from '{subject_object}' to "
                    f"'{sub.data_object}' — declare a 'joins:' block."
                ),
                path="filters",
            )
        )
        return None

    # A hop *through* a nested object is the same problem one step further in:
    # the subquery body would join a table that does not exist. Reachable when a
    # nested object declares a join onward to a third one, which is legal and is
    # how a nested fact reaches its dimensions.
    for step in path:
        hop = model.data_objects.get(step.to_object)
        if hop is not None and hop.is_nested:
            errors.append(_nested_subquery_error(step.to_object, hop))
            return None

    # First step bridges outer scope → subquery scope (correlation).
    # Remaining steps live entirely inside the subquery (INNER JOIN chain).
    first_step = path[0]
    first_target_obj = model.data_objects.get(first_step.to_object)
    if first_target_obj is None:
        return None  # graph would not yield it otherwise

    from_node = From(
        source=qualify_table(first_target_obj),
        alias=first_step.to_object,
    )

    joins: list[Join | Unnest] = []
    for step in path[1:]:
        step_target_obj = model.data_objects.get(step.to_object)
        if step_target_obj is None:
            continue
        joins.append(
            Join(
                join_type=JoinType.INNER,
                source=qualify_table(step_target_obj),
                alias=step.to_object,
                on=graph.build_join_condition(step),
            )
        )

    where_parts: list[Expr] = [graph.build_join_condition(first_step)]

    # Aliases the subquery body owns: its target plus every hop the
    # correlation path walked through. The subject stays outside — it is
    # referenced by the correlation predicate, not joined.
    scope = {step.to_object for step in path}

    nested_seen = False
    for sub_qf in sub.filter:
        if sub_qf.op in (FilterOperator.EXISTS, FilterOperator.NONEXISTS):
            if not nested_seen:
                errors.append(
                    SemanticError(
                        code="NESTED_SUBQUERY_NOT_SUPPORTED",
                        message=(
                            f"Nested '{sub_qf.op}' inside a subquery filter is "
                            "not supported in v2.7.0"
                        ),
                        path="filters",
                    )
                )
                nested_seen = True
            continue
        ref = _resolve_subquery_filter_field(sub_qf.field, model, sub.data_object, errors)
        if ref is None:
            continue
        ref_object, ref_column = ref
        # The field's own object, then whatever its expression reads: a computed
        # column is inlined into the body, so a cross-object reference names an
        # alias the subquery has to join for itself.
        needed = [(ref_object, False)] + [
            (dep, True) for dep in sorted(model.column_reference_objects(ref_object, ref_column))
        ]
        if not all(
            _join_filter_object_into_subquery(
                obj,
                sub_qf.field,
                graph=graph,
                model=model,
                subject_object=subject_object,
                target_object=sub.data_object,
                scope=scope,
                joins=joins,
                qualify_table=qualify_table,
                errors=errors,
                read_through_expression=through_expression,
            )
            for obj, through_expression in needed
        ):
            continue
        col_expr = make_column_expr(model, ref_object, ref_column)
        sub_expr = build_filter_expr(col_expr, sub_qf, errors)
        if sub_expr is not None:
            where_parts.append(sub_expr)

    if touched_objects is not None:
        touched_objects.update(scope)

    where_expr: Expr = where_parts[0]
    for part in where_parts[1:]:
        where_expr = BinaryOp(left=where_expr, op="AND", right=part)

    select = Select(
        columns=[Literal(value=1)],
        from_=from_node,
        joins=joins,
        where=where_expr,
    )

    return Exists(subquery=select, negated=(qf.op == FilterOperator.NONEXISTS))
