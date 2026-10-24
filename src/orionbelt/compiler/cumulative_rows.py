"""Cumulative metrics evaluated on rows the query had no data for.

A pass that adds rows after the cumulative pass (``fill``'s empty periods,
``population: dimension``'s members without data) has no value for a
cumulative metric on them: its window ran over the rows with data. Here each
added row is put into that window, so it reads the value its row would have
shown: a running total carries on, a rolling window drops the periods it no
longer reaches, a member without earlier periods reads none.

The windows read :attr:`ResolvedQuery.cumulative_sources`, one row per group
and period with data, with a row per added group and period appended. Its base
measure is the NULL of the unmatched join, which no aggregate counts, so the
window reads the periods with data as the cumulative pass's own window does,
and a row with data reads exactly what it read there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from orionbelt.ast.nodes import (
    CTE,
    AliasedExpr,
    ColumnRef,
    Expr,
    From,
    IsNull,
    Join,
    JoinType,
    Select,
    UnionAll,
)
from orionbelt.compiler.cumulative_wrap import (
    _all_of,
    _is_rolling,
    _partition_names,
    _rolling_query,
    _window_for,
)
from orionbelt.compiler.time_lookback import null_safe_eq

if TYPE_CHECKING:
    from orionbelt.compiler.resolution import ResolvedMeasure, ResolvedQuery
    from orionbelt.dialect.base import Dialect
    from orionbelt.models.semantic import SemanticModel


def cumulative_on_rows(
    resolved: ResolvedQuery,
    rows: str,
    time_name: str,
    model: SemanticModel,
    dialect: Dialect,
    *,
    prefix: str,
    same_period: Callable[[Expr, Expr], Expr],
) -> tuple[list[CTE], dict[str, str]]:
    """The CTEs evaluating each cumulative metric over *time_name* on *rows*.

    *rows* is a CTE with a column per query dimension, under its name, and a
    row per group and period the metric is wanted for. A row there without
    one in the metric's source is added to the window; one without a period
    (its *time_name* NULL) is not a period and reads nothing here.
    *same_period* compares the source's period with the row's.

    Returns the CTEs, named from *prefix*, and per metric the CTE holding its
    value under its name, beside the dimensions.
    """
    by_source: dict[str, list[ResolvedMeasure]] = {}
    for m in resolved.measures:
        source = resolved.cumulative_sources.get(m.name)
        if source is not None and m.cumulative_time_dimension == time_name:
            by_source.setdefault(source, []).append(m)
    names = [d.name for d in resolved.dimensions]
    ctes: list[CTE] = []
    values: dict[str, str] = {}
    for index, (source, metrics) in enumerate(by_source.items(), 1):
        suffix = "" if index == 1 else f"_{index}"
        union_name = f"{prefix}_source{suffix}"
        bases = list(dict.fromkeys(m.cumulative_measure or m.name for m in metrics))
        with_rows = Select(
            columns=[ColumnRef(name=name) for name in [*names, *bases]],
            from_=From(source=source, alias=source),
        )
        on = _all_of(
            same_period(
                ColumnRef(name=time_name, table=source), ColumnRef(name=time_name, table=rows)
            )
            if name == time_name
            else null_safe_eq(ColumnRef(name=name, table=source), ColumnRef(name=name, table=rows))
            for name in names
        )
        without_rows = Select(
            columns=[
                *(AliasedExpr(expr=ColumnRef(name=name, table=rows), alias=name) for name in names),
                *(
                    AliasedExpr(expr=ColumnRef(name=base, table=source), alias=base)
                    for base in bases
                ),
            ],
            from_=From(source=rows, alias=rows),
            joins=[Join(join_type=JoinType.LEFT, source=source, alias=source, on=on)],
            where=_all_of(
                [
                    IsNull(expr=ColumnRef(name=time_name, table=source)),
                    IsNull(expr=ColumnRef(name=time_name, table=rows), negated=True),
                ]
            ),
        )
        ctes.append(CTE(name=union_name, query=UnionAll(queries=[with_rows, without_rows])))

        windowed = [m for m in metrics if not _is_rolling(m)]
        if windowed:
            name = f"{prefix}{suffix}"
            ctes.append(
                CTE(
                    name=name,
                    query=Select(
                        columns=[
                            *(AliasedExpr(expr=ColumnRef(name=n), alias=n) for n in names),
                            *(
                                AliasedExpr(
                                    expr=_window_for(m, resolved, model, dialect), alias=m.name
                                )
                                for m in windowed
                            ),
                        ],
                        from_=From(source=union_name, alias=union_name),
                    ),
                )
            )
            values.update((m.name, name) for m in windowed)
        rolling: dict[tuple[str, ...], list[ResolvedMeasure]] = {}
        for m in metrics:
            if _is_rolling(m):
                rolling.setdefault(tuple(_partition_names(m, resolved)), []).append(m)
        for count, (partitions, same_axis) in enumerate(rolling.items(), 1):
            name = f"{prefix}_rolling{suffix}" + ("" if count == 1 else f"_{count}")
            query = _rolling_query(union_name, same_axis, time_name, list(partitions), resolved)
            ctes.append(CTE(name=name, query=query))
            values.update((m.name, name) for m in same_axis)
    return ctes, values
