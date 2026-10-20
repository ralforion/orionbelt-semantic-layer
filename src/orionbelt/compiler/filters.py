"""Filter expression builder — converts QueryFilter and MeasureFilter to AST expressions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal
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
    LikeMatch,
    Literal,
    RegexMatch,
    RelativeDateRange,
    Select,
    UnaryOp,
    Unnest,
)
from orionbelt.compiler.expr_rewrite import map_column_refs
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import FilterOperator, QueryFilter, UsePathName
from orionbelt.models.semantic import (
    DataObject,
    DataType,
    Dimension,
    FilterEnforcement,
    FilterLogic,
    FilterValue,
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    ModelFilter,
    SemanticModel,
)

if TYPE_CHECKING:
    from orionbelt.compiler.graph import JoinGraph, JoinStep


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
        case FilterOperator.CONTAINS | FilterOperator.NOT_CONTAINS:
            return LikeMatch(
                column=col,
                pattern=f"%{_escape_like(str(val))}%",
                negated=op == FilterOperator.NOT_CONTAINS,
            )
        case FilterOperator.STARTS_WITH:
            return LikeMatch(column=col, pattern=f"{_escape_like(str(val))}%")
        case FilterOperator.ENDS_WITH:
            return LikeMatch(column=col, pattern=f"%{_escape_like(str(val))}")
        case FilterOperator.LIKE | FilterOperator.NOT_LIKE:
            return LikeMatch(column=col, pattern=str(val), negated=op == FilterOperator.NOT_LIKE)
        case FilterOperator.ILIKE | FilterOperator.NOT_ILIKE:
            return LikeMatch(
                column=col,
                pattern=str(val),
                negated=op == FilterOperator.NOT_ILIKE,
                case_insensitive=True,
            )
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


def _extract_filter_value(fv: FilterValue) -> str | int | float | Decimal | bool | None:
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
        case DataType.DECIMAL:
            return fv.decimal_value
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

    if any(fv.is_inexact_decimal for fv in mf.values):
        errors.append(
            SemanticError(
                code="INEXACT_DECIMAL_VALUE",
                message=(
                    f"Measure filter on '{mf.column.view}.{mf.column.column}' has a decimal "
                    f"value with more than 15 significant digits, which arrives as a "
                    f"rounded double"
                ),
                path="measures",
            )
        )
        return None

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
        case "contains" | "notcontains":
            v = values[0] if values else ""
            return LikeMatch(
                column=col,
                pattern=f"%{_escape_like(str(v))}%",
                negated=op_str == "notcontains",
            )
        case "starts_with":
            v = values[0] if values else ""
            return LikeMatch(column=col, pattern=f"{_escape_like(str(v))}%")
        case "ends_with":
            v = values[0] if values else ""
            return LikeMatch(column=col, pattern=f"%{_escape_like(str(v))}")
        case "like" | "notlike":
            v = values[0] if values else ""
            return LikeMatch(column=col, pattern=str(v), negated=op_str == "notlike")
        case "ilike" | "notilike":
            v = values[0] if values else ""
            return LikeMatch(
                column=col,
                pattern=str(v),
                negated=op_str == "notilike",
                case_insensitive=True,
            )
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


#: Alias prefix of the subject's own row, re-read inside an ``EXISTS`` body to
#: unnest its array (see ``build_exists_filter_expr``).
_SELF_ALIAS = "__ob_self_"


def _realiased(expr: Expr, alias: str) -> Expr:
    """*expr* reading its table's columns under *alias* instead."""
    return map_column_refs(
        expr, lambda ref: replace(ref, table=alias) if ref.table is not None else ref
    )


def _body_hop(
    step: JoinStep,
    joined_object: str,
    model: SemanticModel,
    graph: JoinGraph,
    qualify_table: Callable[[DataObject], str],
    parent_alias: str | None = None,
) -> Join | Unnest | None:
    """The clause that brings *joined_object* into an ``EXISTS`` body.

    A table is an INNER JOIN. A nested object is an unnest of its parent's
    array column - the parent is in the body already, since the step walks
    from it - and an inner one: an element that is not there is no row to
    find. ``None`` for a hop out of a nested object back to its parent,
    which has no key to join on.
    """
    obj = model.data_objects.get(joined_object)
    if obj is None:
        return None
    if obj.is_nested:
        if not step.nested or joined_object != step.to_object:
            return None
        from orionbelt.compiler.nested import build_unnest

        unnest = build_unnest(step, obj)
        return replace(unnest, parent_alias=parent_alias or step.from_object, outer=False)
    return Join(
        join_type=JoinType.INNER,
        source=qualify_table(obj),
        alias=joined_object,
        on=graph.build_join_condition(step),
    )


def _subquery_filter_dimension(
    field: str, model: SemanticModel, target_object: str
) -> Dimension | None:
    """The dimension a subquery filter's *field* resolves through, if any.

    The same precedence as :func:`_resolve_subquery_filter_field`: a column of
    the target shadows a dimension of the same name. A dimension found here is
    filtered as ``where`` filters it, so a grained one compares its period.
    """
    target_obj = model.data_objects.get(target_object)
    if target_obj is not None and field in target_obj.columns:
        return None
    dim = model.dimensions.get(field)
    dim_obj = model.data_objects.get(dim.view) if dim is not None else None
    if dim is None or dim_obj is None or dim.column not in dim_obj.columns:
        return None
    return dim


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

    hops: list[Join | Unnest] = []
    for step, joined_object in zip(steps, joined_objects, strict=True):
        if joined_object in scope or joined_object not in model.data_objects:
            continue
        # A nested object, or one behind it, is unnested from its parent.
        hop = _body_hop(step, joined_object, model, graph, qualify_table)
        if hop is None:
            errors.append(_nested_subquery_error(joined_object, model.data_objects[joined_object]))
            return False
        hops.append(hop)
        scope.add(joined_object)
    joins.extend(hops)
    return True


def _enforced_filters_in_subquery(
    model: SemanticModel,
    *,
    subject_object: str,
    target_object: str,
    graph: JoinGraph,
    scope: set[str],
    joins: list[Join | Unnest],
    qualify_table: Callable[[DataObject], str],
    errors: list[SemanticError],
) -> list[Expr]:
    """The ``enforce: always`` static filters an ``EXISTS`` body must apply.

    The body is a scan of its own: its rows decide the predicate's outcome, so a
    row the filter excludes would still answer "is there one". Each enforced
    filter is joined into the body and applied there. One on the correlation
    subject needs nothing - the body's rows belong to an outer row the outer
    query has already filtered - and one the body cannot reach refuses the
    query (``STATIC_FILTER_UNREACHABLE``).
    """
    predicates: list[Expr] = []
    for mf in model.filters:
        if mf.enforce != FilterEnforcement.ALWAYS:
            continue
        needed = [
            mf.data_object,
            *sorted(model.column_reference_objects(mf.data_object, mf.column)),
        ]
        if set(needed) <= {subject_object}:
            continue
        unreachable: list[SemanticError] = []
        if not all(
            _join_filter_object_into_subquery(
                obj,
                f"{mf.data_object}.{mf.column}",
                graph=graph,
                model=model,
                subject_object=subject_object,
                target_object=target_object,
                scope=scope,
                joins=joins,
                qualify_table=qualify_table,
                errors=unreachable,
            )
            for obj in needed
        ):
            errors.append(
                static_filter_unreachable_error(
                    f"static filter on '{mf.data_object}.{mf.column}'",
                    mf.data_object,
                    f"the subquery on '{target_object}'",
                )
            )
            continue
        predicate = static_filter_predicate(model, mf, errors)
        if predicate is not None:
            predicates.append(predicate)
    return predicates


def build_exists_filter_expr(
    qf: QueryFilter,
    model: SemanticModel,
    subject_object: str,
    qualify_table: Callable[[DataObject], str],
    errors: list[SemanticError],
    touched_objects: set[str] | None = None,
    *,
    use_path_names: list[UsePathName] | None = None,
) -> Expr | None:
    """Compile an ``exists`` / ``nonexists`` filter into an ``Exists`` AST node.

    *use_path_names* are the query's secondary-path choices, so a correlation
    the planner builds itself follows the same relationship the query's rows
    do; a ``subquery.pathName`` takes precedence.

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
    from orionbelt.compiler.resolution import make_column_expr, make_filter_subject_expr

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

    graph = JoinGraph.of(model, use_path_names=overrides or use_path_names)

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

    # First step bridges outer scope -> subquery scope (correlation).
    # Remaining steps live entirely inside the subquery (INNER JOIN chain).
    first_step = path[0]
    joins: list[Join | Unnest] = []
    correlation: list[Expr]
    # Objects the body reads besides the path's own: a nested target's parent.
    body_objects: set[str] = set()
    first_target_obj = model.data_objects.get(first_step.to_object)
    if first_target_obj is None:
        return None  # graph would not yield it otherwise
    if first_step.nested and not first_target_obj.is_nested:
        # From a nested subject up to its parent: no key to correlate on.
        errors.append(_nested_subquery_error(subject_object, subject_obj))
        return None
    if first_step.nested:
        # The subject's own array: the body re-reads the subject's row, under
        # an alias of its own, and unnests it there. The row is matched on the
        # subject's key - an alias equal to the outer one would rebind the
        # correlation to the body's rows.
        keys = [name for name, column in subject_obj.columns.items() if column.primary_key]
        if not keys:
            errors.append(
                SemanticError(
                    code="NESTED_OBJECT_IN_SUBQUERY",
                    message=(
                        f"EXISTS over '{first_step.to_object}' reads the array of each "
                        f"'{subject_object}' row, and matches the row on its primary "
                        f"key; '{subject_object}' declares none."
                    ),
                    path="filters",
                    hint=f"Declare 'primaryKey: true' on the key columns of '{subject_object}'.",
                )
            )
            return None
        self_alias = f"{_SELF_ALIAS}{subject_object}"
        from_node = From(source=qualify_table(subject_obj), alias=self_alias)
        unnest = _body_hop(
            first_step, first_step.to_object, model, graph, qualify_table, self_alias
        )
        assert unnest is not None  # a nested step's far end is the nested object
        joins.append(unnest)
        # The stored values, as a join compares them: read in the query's
        # timezone, two instants an hour apart in a DST fall-back are equal.
        correlation = [
            BinaryOp(
                left=_realiased(
                    make_column_expr(model, subject_object, key, in_query_timezone=False),
                    self_alias,
                ),
                op="=",
                right=make_column_expr(model, subject_object, key, in_query_timezone=False),
            )
            for key in keys
        ]
    elif first_target_obj.is_nested:
        # Reached by an ordinary join walked backwards (owners to the labels
        # naming them): the body reads the labels' parent and unnests it, and
        # the join is the correlation.
        assert first_target_obj.nested_in is not None
        parent = first_target_obj.nested_in.data_object
        parent_obj = model.data_objects.get(parent)
        containment = graph.find_join_path({parent}, {parent, first_step.to_object})
        if parent_obj is None or parent_obj.is_nested or len(containment) != 1:
            errors.append(_nested_subquery_error(first_step.to_object, first_target_obj))
            return None
        from_node = From(source=qualify_table(parent_obj), alias=parent)
        unnest = _body_hop(containment[0], first_step.to_object, model, graph, qualify_table)
        assert unnest is not None  # a containment step's far end is the nested object
        joins.append(unnest)
        correlation = [graph.build_join_condition(first_step)]
        body_objects.add(parent)
    else:
        from_node = From(source=qualify_table(first_target_obj), alias=first_step.to_object)
        correlation = [graph.build_join_condition(first_step)]

    # The undirected walk orients each step along the path: its far end is
    # ``to_object``.
    for step in path[1:]:
        joined_object = step.to_object
        if joined_object in body_objects:
            continue  # the nested target's parent, already in FROM
        hop = _body_hop(step, joined_object, model, graph, qualify_table)
        if hop is None:
            errors.append(_nested_subquery_error(joined_object, model.data_objects[joined_object]))
            return None
        joins.append(hop)

    where_parts: list[Expr] = list(correlation)

    # Aliases the subquery body owns: its target plus every hop the
    # correlation path walked through. The subject stays outside — it is
    # referenced by the correlation predicate, not joined.
    scope = {step.to_object for step in path} | body_objects

    for sub_qf in sub.filter:
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
        if sub_qf.op in (FilterOperator.EXISTS, FilterOperator.NONEXISTS):
            # An EXISTS inside the body correlates to the object its field is
            # on, which the body joins: only the object is the subject.
            needed = needed[:1]
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
        if sub_qf.op in (FilterOperator.EXISTS, FilterOperator.NONEXISTS):
            sub_expr = build_exists_filter_expr(
                sub_qf,
                model,
                ref_object,
                qualify_table,
                errors,
                touched_objects,
                use_path_names=use_path_names,
            )
        else:
            dim = _subquery_filter_dimension(sub_qf.field, model, sub.data_object)
            col_expr = (
                make_column_expr(model, ref_object, ref_column)
                if dim is None
                else make_filter_subject_expr(model, dim)
            )
            sub_expr = build_filter_expr(col_expr, sub_qf, errors)
        if sub_expr is not None:
            where_parts.append(sub_expr)

    where_parts.extend(
        _enforced_filters_in_subquery(
            model,
            subject_object=subject_object,
            target_object=sub.data_object,
            graph=graph,
            scope=scope,
            joins=joins,
            qualify_table=qualify_table,
            errors=errors,
        )
    )

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


def static_filter_predicate(
    model: SemanticModel, mf: ModelFilter, errors: list[SemanticError]
) -> Expr | None:
    """A static model filter as a predicate over its column.

    Through ``make_column_expr``, so a filter on a computed column inlines the
    expression; without it a filter on a boolean ``expression:`` column emitted
    ``(1 = FALSE)`` because the empty ``code:`` was collapsed by the CAST.
    """
    from orionbelt.compiler.resolution import make_column_expr

    col_expr = make_column_expr(model, mf.data_object, mf.column)
    qf = QueryFilter(field=mf.column, op=mf.operator, value=mf.value or mf.values or None)
    return build_filter_expr(col_expr, qf, errors)


def static_filter_unreachable_error(label: str, unreachable: str, where: str) -> SemanticError:
    """The refusal for an ``enforce: always`` static filter that cannot be applied."""
    return SemanticError(
        code="STATIC_FILTER_UNREACHABLE",
        message=(
            f"The {label} is declared 'enforce: always', and {where} cannot reach "
            f"'{unreachable}'. The query would return rows the filter excludes."
        ),
        path="filters",
        hint="Query measures and dimensions whose data objects reach the filtered one.",
    )
