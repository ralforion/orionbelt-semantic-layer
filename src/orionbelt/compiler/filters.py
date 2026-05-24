"""Filter expression builder — converts QueryFilter and MeasureFilter to AST expressions."""

from __future__ import annotations

from typing import TypedDict

from orionbelt.ast.nodes import (
    Between,
    BinaryOp,
    Expr,
    FunctionCall,
    InList,
    IsNull,
    Literal,
    RegexMatch,
    RelativeDateRange,
    UnaryOp,
)
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import FilterOperator, QueryFilter
from orionbelt.models.semantic import (
    DataType,
    FilterLogic,
    FilterValue,
    MeasureFilter,
    MeasureFilterGroup,
    MeasureFilterItem,
    SemanticModel,
)


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
