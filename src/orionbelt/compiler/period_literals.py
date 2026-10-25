"""Period literals in date filter values: ``'2025'``, ``'2025-Q1'``, ``'2025-03'``.

A period names a range of days, so a filter value naming one is expanded to
its bounds at compile time: ``= '2025-Q1'`` is the whole quarter, ``<=
'2024-12'`` runs through 31 December, and ``between ['2025-01', '2025-03']``
from 1 January to 31 March. A comparison against the string itself would read
``'2025-Q1'`` as a date and fail, or ``'2024-12'`` as the first of the month and
leave the rest of December out.

Only a date-bearing field is expanded, and only a value in one of these
shapes: an ISO date stays the value it was. Every path that builds a filter
over a field does it, through ``build_filter_expr``: a query's ``where``, a
``filterContext.include`` item, a filter inside an ``exists`` subquery and a
static model filter.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from orionbelt.ast.nodes import BinaryOp, Expr, Literal, UnaryOp
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import FilterOperator, QueryFilter
from orionbelt.models.semantic import DATE_BEARING_TYPES, Dimension, SemanticModel

_PERIOD = re.compile(r"^(\d{4})(?:-(?:[Qq]([1-4])|(0[1-9]|1[0-2])))?$")


def dimension_is_date_bearing(dim: Dimension) -> bool:
    """Whether a filter on *dim* reads a period literal as its bounds."""
    return dim.time_grain is not None or dim.result_type in DATE_BEARING_TYPES


def column_is_date_bearing(model: SemanticModel, obj_name: str, col_name: str) -> bool:
    """Whether a filter on the column *obj_name*.*col_name* reads a period literal."""
    obj = model.data_objects.get(obj_name)
    column = obj.columns.get(col_name) if obj else None
    return column is not None and column.abstract_type in DATE_BEARING_TYPES


def is_period_literal(value: object) -> bool:
    """Whether *value* has the shape of a period literal, representable or not."""
    return isinstance(value, str) and _PERIOD.match(value.strip()) is not None


def period_bounds(value: object) -> tuple[date, date] | None:
    """The half-open ``[start, end)`` a period literal names; ``None`` otherwise.

    Raises ``ValueError`` for a period a date cannot bound: year 0, or one
    ending in year 10000 (``'9999'``, ``'9999-12'``).
    """
    if not isinstance(value, str):
        return None
    match = _PERIOD.match(value.strip())
    if match is None:
        return None
    year = int(match.group(1))
    if match.group(2):
        first_month, months = 3 * int(match.group(2)) - 2, 3
    elif match.group(3):
        first_month, months = int(match.group(3)), 1
    else:
        first_month, months = 1, 12
    end_month = first_month + months
    end = date(year + (end_month - 1) // 12, (end_month - 1) % 12 + 1, 1)
    return date(year, first_month, 1), end


def has_period_literal(value: object) -> bool:
    """Whether *value*, or an item of it, is a period literal."""
    items = value if isinstance(value, list) else [value]
    return any(is_period_literal(item) for item in items)


def _cmp(col: Expr, op: str, day: date) -> Expr:
    return BinaryOp(left=col, op=op, right=Literal(value=day.isoformat()))


def _within(col: Expr, value: Any) -> Expr:
    """The column inside the period *value*, or equal to a plain value."""
    bounds = period_bounds(value)
    if bounds is None:
        return BinaryOp(left=col, op="=", right=Literal(value=value))
    start, end = bounds
    return BinaryOp(left=_cmp(col, ">=", start), op="AND", right=_cmp(col, "<", end))


def _any_of(col: Expr, values: list[Any]) -> Expr:
    combined = _within(col, values[0])
    for value in values[1:]:
        combined = BinaryOp(left=combined, op="OR", right=_within(col, value))
    return combined


def _lower(col: Expr, value: Any) -> Expr:
    bounds = period_bounds(value)
    if bounds is None:
        return BinaryOp(left=col, op=">=", right=Literal(value=value))
    return _cmp(col, ">=", bounds[0])


def _upper(col: Expr, value: Any) -> Expr:
    bounds = period_bounds(value)
    if bounds is None:
        return BinaryOp(left=col, op="<=", right=Literal(value=value))
    return _cmp(col, "<", bounds[1])


def build_period_filter_expr(
    col: Expr, qf: QueryFilter, errors: list[SemanticError]
) -> Expr | None:
    """The predicate of a filter whose value holds a period literal.

    A NULL column fails every form, as it fails ``=`` and ``NOT IN``: each
    comparison with it is NULL, and so is ``NOT`` around one.
    """
    op, val = qf.op, qf.value
    for item in val if isinstance(val, list) else [val]:
        try:
            period_bounds(item)
        except ValueError:
            errors.append(
                SemanticError(
                    code="INVALID_FILTER_VALUE",
                    message=(
                        f"Filter on '{qf.field}': the period {item!r} has bounds outside "
                        "the dates from 0001-01-01 to 9999-12-31"
                    ),
                    path="filters",
                )
            )
            return None
    bounds = period_bounds(val)
    match op:
        case FilterOperator.EQUALS | FilterOperator.EQ if bounds:
            return _within(col, val)
        case FilterOperator.NOT_EQUALS | FilterOperator.NEQ if bounds:
            return BinaryOp(
                left=_cmp(col, "<", bounds[0]), op="OR", right=_cmp(col, ">=", bounds[1])
            )
        case FilterOperator.GT | FilterOperator.GREATER if bounds:
            return _cmp(col, ">=", bounds[1])
        case FilterOperator.GTE | FilterOperator.GREATER_EQ if bounds:
            return _cmp(col, ">=", bounds[0])
        case FilterOperator.LT | FilterOperator.LESS if bounds:
            return _cmp(col, "<", bounds[0])
        case FilterOperator.LTE | FilterOperator.LESS_EQ if bounds:
            return _cmp(col, "<", bounds[1])
        case FilterOperator.IN_LIST | FilterOperator.IN:
            return _any_of(col, val if isinstance(val, list) else [val])
        case FilterOperator.NOT_IN_LIST | FilterOperator.NOT_IN:
            values = val if isinstance(val, list) else [val]
            return UnaryOp(op="NOT", operand=_any_of(col, values))
        case FilterOperator.BETWEEN | FilterOperator.NOT_BETWEEN if (
            isinstance(val, list) and len(val) == 2
        ):
            inside = BinaryOp(left=_lower(col, val[0]), op="AND", right=_upper(col, val[1]))
            if op == FilterOperator.BETWEEN:
                return inside
            return UnaryOp(op="NOT", operand=inside)
    errors.append(
        SemanticError(
            code="INVALID_FILTER_VALUE",
            message=(
                f"Filter on '{qf.field}': '{op}' does not take a period literal "
                f"({val!r}); use =, !=, <, <=, >, >=, in, not_in, between or notbetween"
            ),
            path="filters",
        )
    )
    return None
