"""Shared metadata-row extraction for OBSL surfaces.

Both the Arrow Flight catalog and the Postgres-wire catalog expose
``_dimensions_metadata`` / ``_measures_metadata`` / ``_metrics_metadata``
introspection views. The row shapes are identical by design — same
column names, same column order, same types — so that BI tools see the
same surface whether they connect over Flight or pgwire.

This module owns the single source of truth for that extraction.
Surfaces render the row dicts to their native representation
(``pa.Table`` for Flight, ``CREATE VIEW … VALUES (...)`` for pgwire);
neither implementation re-derives the field set.

The column schemas listed at the top of this module are the contract.
Changing them requires updating both rendering paths in lockstep.
"""

from __future__ import annotations

from typing import Any

# Column ordering — kept identical to ``DIMENSIONS_METADATA_SCHEMA`` /
# ``MEASURES_METADATA_SCHEMA`` / ``METRICS_METADATA_SCHEMA`` in
# ``drivers/ob-flight-extension/src/ob_flight/catalog.py``.
DIMENSION_METADATA_COLUMNS: tuple[str, ...] = (
    "name",
    "data_object",
    "column",
    "type",
    "time_grain",
    "description",
)

MEASURE_METADATA_COLUMNS: tuple[str, ...] = (
    "name",
    "aggregation",
    "expression",
    "type",
    "columns",
    "description",
)

METRIC_METADATA_COLUMNS: tuple[str, ...] = (
    "name",
    "metric_type",
    "expression",
    "measure",
    "time_dimension",
    "time_grain",
    "window",
    "grain_to_date",
    "description",
)


def _enum_value(maybe_enum: Any, default: str | None = None) -> str | None:
    """Return ``.value`` of an Enum, or the string form, or default."""

    if maybe_enum is None:
        return default
    val = getattr(maybe_enum, "value", None)
    if val is not None:
        return str(val)
    text = str(maybe_enum)
    return text or default


def build_dimension_rows(
    model: Any,
) -> list[tuple[str, str, str, str, str | None, str | None]]:
    """Extract one row per dimension matching ``DIMENSION_METADATA_COLUMNS``."""

    rows: list[tuple[str, str, str, str, str | None, str | None]] = []
    if not getattr(model, "dimensions", None):
        return rows
    for dim_name, dim in model.dimensions.items():
        name = getattr(dim, "label", dim_name) or dim_name
        data_object = getattr(dim, "view", "") or ""
        column = getattr(dim, "column", "") or ""
        type_ = _enum_value(getattr(dim, "result_type", None), default="string") or "string"
        time_grain = _enum_value(getattr(dim, "time_grain", None))
        description = getattr(dim, "description", None)
        rows.append((name, data_object, column, type_, time_grain, description))
    return rows


def build_measure_rows(
    model: Any,
) -> list[tuple[str, str, str | None, str, str, str | None]]:
    """Extract one row per measure matching ``MEASURE_METADATA_COLUMNS``."""

    rows: list[tuple[str, str, str | None, str, str, str | None]] = []
    # Use effective measures so BI tools (Flight / pgwire catalog) see the
    # synthesized ``<object>.count`` measures alongside declared ones.
    effective = getattr(model, "effective_measures", None) or getattr(model, "measures", None)
    if not effective:
        return rows
    for meas_name, meas in effective.items():
        name = getattr(meas, "label", meas_name) or meas_name
        aggregation = _enum_value(getattr(meas, "aggregation", None), default="") or ""
        expression = getattr(meas, "expression", None)
        type_ = _enum_value(getattr(meas, "result_type", None), default="float") or "float"
        cols = getattr(meas, "columns", []) or []
        col_strs: list[str] = []
        for ref in cols:
            view = getattr(ref, "view", "") or ""
            col = getattr(ref, "column", "") or ""
            col_strs.append(f"{view}.{col}" if view else col)
        columns_str = ", ".join(col_strs)
        description = getattr(meas, "description", None)
        rows.append((name, aggregation, expression, type_, columns_str, description))
    return rows


def build_metric_rows(
    model: Any,
) -> list[
    tuple[
        str, str, str | None, str | None, str | None, str | None, int | None, str | None, str | None
    ]
]:
    """Extract one row per metric matching ``METRIC_METADATA_COLUMNS``.

    Resolves ``time_dimension`` and ``time_grain`` the same way Flight
    does — derived metrics have neither; cumulative carries them
    directly; period-over-period reaches into the nested
    ``period_over_period`` block.
    """

    rows: list[
        tuple[
            str,
            str,
            str | None,
            str | None,
            str | None,
            str | None,
            int | None,
            str | None,
            str | None,
        ]
    ] = []
    model_dims = getattr(model, "dimensions", None) or {}
    if not getattr(model, "metrics", None):
        return rows
    for met_name, met in model.metrics.items():
        name = getattr(met, "label", met_name) or met_name
        metric_type = _enum_value(getattr(met, "type", None), default="derived") or "derived"
        expression = getattr(met, "expression", None)
        measure = getattr(met, "measure", None)
        td = getattr(met, "time_dimension", None)
        pop = getattr(met, "period_over_period", None)
        if not td and pop is not None:
            td = getattr(pop, "time_dimension", None)
        time_dimension = td or None
        grain: str | None = None
        if pop is not None:
            grain = _enum_value(getattr(pop, "grain", None))
        if grain is None and td and td in model_dims:
            grain = _enum_value(getattr(model_dims[td], "time_grain", None))
        win = getattr(met, "window", None)
        window: int | None = int(win) if isinstance(win, int) else None
        grain_to_date = _enum_value(getattr(met, "grain_to_date", None))
        description = getattr(met, "description", None)
        rows.append(
            (
                name,
                metric_type,
                expression,
                measure,
                time_dimension,
                grain,
                window,
                grain_to_date,
                description,
            )
        )
    return rows
