"""A count is never NULL.

Inside one aggregate a count over no rows is 0, but a measure computed apart
from the rows it is shown on (a ``filterContext`` CTE, a reaggregate stage, an
as-of value, a filled period) is joined back, and a row the join finds no
match for reads NULL. :func:`counts` says whether a measure's value is a count
and :func:`zero_if_missing` gives it the 0 a row without a match counted,
typed as the measure declares.

A running or rolling ``sum`` of a count is a count too: over no period its sum
is NULL where the count of what it adds up is 0. Only the finished value
defaults; the inputs stay NULL, so an ``avg`` over the periods, or a ``count``
of them, reads the periods that have rows and no others.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from orionbelt.ast.nodes import Expr, FunctionCall, Literal
from orionbelt.compiler.type_resolver import resolve_measure_data_type, resolve_metric_data_type
from orionbelt.models.semantic import CumulativeAggType, ReaggregateAggType

if TYPE_CHECKING:
    from orionbelt.compiler.resolution import ResolvedMeasure
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import SemanticModel

COUNT_AGGREGATIONS = frozenset({"count", "count_distinct"})


def _counts_rows(name: str | None, model: SemanticModel) -> bool:
    """Whether the measure named *name* is a ``count`` or ``count_distinct``."""
    measure = model.effective_measures.get(name) if name else None
    return measure is not None and measure.aggregation.lower() in COUNT_AGGREGATIONS


def counts(measure: ResolvedMeasure, model: SemanticModel) -> bool:
    """Whether *measure*'s value is a count: a ``count`` / ``count_distinct``
    measure, a reaggregate ``count``, a cumulative ``count``, or a cumulative
    ``sum`` of a count."""
    if measure.is_reaggregate:
        return measure.reaggregate_aggregation is ReaggregateAggType.COUNT
    if measure.is_cumulative:
        return measure.cumulative_type is CumulativeAggType.COUNT or (
            measure.cumulative_type is CumulativeAggType.SUM
            and _counts_rows(measure.cumulative_measure, model)
        )
    if measure.component_measures or measure.is_pop or measure.is_window:
        return False
    return measure.aggregation.lower() in COUNT_AGGREGATIONS


def zero_if_missing(
    value: Expr, measure: ResolvedMeasure, model: SemanticModel, dialect: Dialect
) -> Expr:
    """*value*, or a count's 0 where it is NULL, in the type the measure
    declares (a count declared ``dataType: varchar`` reads ``'0'``)."""
    declared = (
        resolve_metric_data_type(model.metrics[measure.name], model.settings)
        if measure.name in model.metrics
        else resolve_measure_data_type(
            model.effective_measures[measure.name], model.settings, model
        )
        if measure.name in model.effective_measures
        else None
    )
    zero: Expr = Literal.number(0)
    if declared is not None:
        zero = dialect.cast_to_obml_type(zero, declared)
    return FunctionCall(name="COALESCE", args=[value, zero])
