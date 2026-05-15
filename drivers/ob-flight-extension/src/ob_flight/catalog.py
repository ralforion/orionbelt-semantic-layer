"""Flight catalog — maps OrionBelt model metadata to Flight schema info."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.flight as flight


# Map OBML abstract types to Arrow types. Covers the full OBML DataType enum.
_OBML_TYPE_MAP: dict[str, pa.DataType] = {
    "string": pa.utf8(),
    "json": pa.utf8(),
    "int": pa.int64(),
    "float": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "datetime": pa.timestamp("us"),
    "time": pa.utf8(),
    "time_tz": pa.utf8(),
    "timestamp": pa.timestamp("us"),
    "timestamp_tz": pa.timestamp("us", tz="UTC"),
}


def _obml_type_to_arrow(type_name: str | None) -> pa.DataType:
    """Map an OBML type name to an Arrow type, defaulting to utf8."""
    if not type_name:
        return pa.utf8()
    return _OBML_TYPE_MAP.get(type_name, pa.utf8())


# ---------------------------------------------------------------------------
# Virtual views — two flavours per category
# ---------------------------------------------------------------------------
#
# **Label views** (``_dimensions``, ``_measures``, ``_metrics``) — schema
# built per-model with one column per dim / measure / metric label, typed
# by the declared ``result_type``. BI tools' column picker populates from
# these, so users see a per-category pick list. Queries against them
# (``SELECT "X" FROM _dimensions``) route to semantic mode and compile
# through the standard pipeline — the view name is an alias for the
# model's virtual table restricted to that category.
#
# **Metadata views** (``_dimensions_metadata``, ``_measures_metadata``,
# ``_metrics_metadata``) — fixed introspection schema (name,
# data_object, type, …). Queries return metadata *rows* describing each
# dim/measure/metric. Used by REST callers and SQL tinkering; BI tool
# users mostly ignore them.

# Label views — names BI tools see in the picker. No leading underscore
# so they look like regular catalog objects users can pick from.
LABEL_VIEW_NAMES: tuple[str, ...] = ("dimensions", "measures", "metrics")

# Metadata views — the underscore-prefixed introspection counterparts.
# The leading ``_`` follows the "internal" convention used by Postgres
# (``pg_*``), DBeaver, and BigQuery (``INFORMATION_SCHEMA``).
METADATA_VIEW_NAMES: tuple[str, ...] = (
    "_dimensions_metadata",
    "_measures_metadata",
    "_metrics_metadata",
)

# Back-compat alias: prior name used in server / tests.
VIRTUAL_TABLE_NAMES: tuple[str, ...] = LABEL_VIEW_NAMES + METADATA_VIEW_NAMES

DIMENSIONS_METADATA_SCHEMA = pa.schema(
    [
        pa.field("name", pa.utf8()),
        pa.field("data_object", pa.utf8()),
        pa.field("column", pa.utf8()),
        pa.field("type", pa.utf8()),
        pa.field("time_grain", pa.utf8()),
        pa.field("description", pa.utf8()),
    ]
)

MEASURES_METADATA_SCHEMA = pa.schema(
    [
        pa.field("name", pa.utf8()),
        pa.field("aggregation", pa.utf8()),
        pa.field("expression", pa.utf8()),
        pa.field("type", pa.utf8()),
        pa.field("columns", pa.utf8()),
        pa.field("description", pa.utf8()),
    ]
)

METRICS_METADATA_SCHEMA = pa.schema(
    [
        pa.field("name", pa.utf8()),
        pa.field("metric_type", pa.utf8()),
        pa.field("expression", pa.utf8()),
        pa.field("measure", pa.utf8()),
        # Cumulative & period-over-period metrics require a time dimension —
        # surface it (and its grain) so BI users know to pair the metric
        # with that dim AND can read window=3 + time_grain=month as
        # "3 months".
        pa.field("time_dimension", pa.utf8()),
        pa.field("time_grain", pa.utf8()),
        pa.field("window", pa.int64()),
        pa.field("grain_to_date", pa.utf8()),
        pa.field("description", pa.utf8()),
    ]
)


def dimensions_view_schema(model: Any) -> pa.Schema:
    """Per-model schema for the ``_dimensions`` view: one field per dim label."""
    fields: list[pa.Field] = []
    if hasattr(model, "dimensions") and model.dimensions:
        for label, dim in model.dimensions.items():
            display = getattr(dim, "label", label) or label
            rt = getattr(dim, "result_type", None)
            rt_name = getattr(rt, "value", None) or "string"
            fields.append(pa.field(display, _obml_type_to_arrow(rt_name)))
    return pa.schema(fields)


def measures_view_schema(model: Any) -> pa.Schema:
    """Per-model schema for the ``_measures`` view: one field per measure label."""
    fields: list[pa.Field] = []
    if hasattr(model, "measures") and model.measures:
        for label, meas in model.measures.items():
            display = getattr(meas, "label", label) or label
            rt = getattr(meas, "result_type", None)
            rt_name = getattr(rt, "value", None) or "float"
            fields.append(pa.field(display, _obml_type_to_arrow(rt_name)))
    return pa.schema(fields)


def metrics_view_schema(model: Any) -> pa.Schema:
    """Per-model schema for the ``_metrics`` view: one field per metric label."""
    fields: list[pa.Field] = []
    if hasattr(model, "metrics") and model.metrics:
        for label, met in model.metrics.items():
            display = getattr(met, "label", label) or label
            fields.append(pa.field(display, pa.float64()))
    return pa.schema(fields)


def virtual_view_schema(model: Any, view_name: str) -> pa.Schema:
    """Build the BI-tool-facing schema for a view name.

    Label views (``dimensions``/``measures``/``metrics``) → per-model
    schema of dim/measure/metric labels. Metadata views
    (``_dimensions_metadata`` etc.) → fixed introspection schema
    (name / data_object / type / …).
    """
    if view_name == "dimensions":
        return dimensions_view_schema(model)
    if view_name == "measures":
        return measures_view_schema(model)
    if view_name == "metrics":
        return metrics_view_schema(model)
    if view_name == "_dimensions_metadata":
        return DIMENSIONS_METADATA_SCHEMA
    if view_name == "_measures_metadata":
        return MEASURES_METADATA_SCHEMA
    if view_name == "_metrics_metadata":
        return METRICS_METADATA_SCHEMA
    return pa.schema([])


# Catalog enumeration map. Label views use per-model schemas (built
# lazily via ``virtual_view_schema``); metadata views have fixed schemas
# returned directly. Server code iterates this dict to know which views
# to advertise and which builder to call for each.
VIRTUAL_TABLES: dict[str, pa.Schema] = {
    "_dimensions_metadata": DIMENSIONS_METADATA_SCHEMA,
    "_measures_metadata": MEASURES_METADATA_SCHEMA,
    "_metrics_metadata": METRICS_METADATA_SCHEMA,
}


def object_to_schema(data_object: Any) -> pa.Schema:
    """Build an Arrow schema from a SemanticModel data object.

    Expects data_object to have .columns dict where each column has
    .label (str) and .abstract_type (str).
    """
    fields: list[pa.Field] = []
    if hasattr(data_object, "columns") and data_object.columns:
        for col_name, col in data_object.columns.items():
            abstract_type = getattr(col, "abstract_type", "string") or "string"
            arrow_type = _obml_type_to_arrow(getattr(abstract_type, "value", str(abstract_type)))
            label = getattr(col, "label", col_name) or col_name
            fields.append(pa.field(label, arrow_type))
    return pa.schema(fields)


def model_virtual_table_name(model: Any) -> str:
    """Stable virtual-table name for a model.

    Per ``design/PLAN_flight_natural_sql.md`` §3.1, every model is exposed
    as exactly one virtual table. The server side stamps ``_ob_model_id``
    on the model when it pulls it from the SessionManager — that's the
    source of truth. Falls back to ``model.label`` / ``model.name`` for
    tests that hand-build a model without the session-id stamp, and finally
    to ``"sales_model"``.
    """
    # Check ``__dict__`` directly so MagicMock auto-attrs don't masquerade as
    # a real stamp. Only Pydantic-side-channeled values survive this check.
    if hasattr(model, "__dict__"):
        stamped = model.__dict__.get("_ob_model_id")
        if isinstance(stamped, str) and stamped:
            return stamped
    for attr in ("label", "name"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return "sales_model"


def model_to_virtual_table_schema(model: Any) -> pa.Schema:
    """Build the virtual-table Arrow schema for a model.

    Columns are the union of dimensions + measures + metrics, typed by each
    artefact's ``result_type``. This is the schema BI tools see when they
    pick from the catalog tree.
    """
    fields: list[pa.Field] = []
    if hasattr(model, "dimensions") and model.dimensions:
        for label, dim in model.dimensions.items():
            display = getattr(dim, "label", label) or label
            rt = getattr(dim, "result_type", None)
            rt_name = getattr(rt, "value", None) or "string"
            fields.append(pa.field(display, _obml_type_to_arrow(rt_name)))
    if hasattr(model, "measures") and model.measures:
        for label, meas in model.measures.items():
            display = getattr(meas, "label", label) or label
            rt = getattr(meas, "result_type", None)
            rt_name = getattr(rt, "value", None) or "float"
            fields.append(pa.field(display, _obml_type_to_arrow(rt_name)))
    if hasattr(model, "metrics") and model.metrics:
        for label, met in model.metrics.items():
            display = getattr(met, "label", label) or label
            # Metrics have no result_type; default to float (matches OBML default
            # for ratio/derived metrics).
            fields.append(pa.field(display, pa.float64()))
    return pa.schema(fields)


# ---------------------------------------------------------------------------
# Virtual metadata table data builders
# ---------------------------------------------------------------------------


def build_dimensions_data(model: Any) -> pa.Table:
    """Build a queryable table of all dimensions in the semantic model."""
    names: list[str] = []
    data_objects: list[str] = []
    columns: list[str] = []
    types: list[str] = []
    time_grains: list[str | None] = []
    descriptions: list[str | None] = []

    if hasattr(model, "dimensions") and model.dimensions:
        for dim_name, dim in model.dimensions.items():
            names.append(getattr(dim, "label", dim_name) or dim_name)
            data_objects.append(getattr(dim, "view", "") or "")
            columns.append(getattr(dim, "column", "") or "")
            rt = getattr(dim, "result_type", None)
            rt_value = getattr(rt, "value", None)
            types.append(rt_value if rt_value is not None else str(rt or "string"))
            tg = getattr(dim, "time_grain", None)
            time_grains.append(getattr(tg, "value", None))
            descriptions.append(getattr(dim, "description", None))

    return pa.table(
        {
            "name": names,
            "data_object": data_objects,
            "column": columns,
            "type": types,
            "time_grain": time_grains,
            "description": descriptions,
        },
        schema=DIMENSIONS_METADATA_SCHEMA,
    )


def build_measures_data(model: Any) -> pa.Table:
    """Build a queryable table of all measures in the semantic model."""
    names: list[str] = []
    aggregations: list[str] = []
    expressions: list[str | None] = []
    types: list[str] = []
    columns_list: list[str] = []
    descriptions: list[str | None] = []

    if hasattr(model, "measures") and model.measures:
        for meas_name, meas in model.measures.items():
            names.append(getattr(meas, "label", meas_name) or meas_name)
            aggregations.append(getattr(meas, "aggregation", "") or "")
            expressions.append(getattr(meas, "expression", None))
            rt = getattr(meas, "result_type", None)
            rt_value = getattr(rt, "value", None)
            types.append(rt_value if rt_value is not None else str(rt or "float"))
            cols = getattr(meas, "columns", []) or []
            col_strs = []
            for c in cols:
                v = getattr(c, "view", "") or ""
                col = getattr(c, "column", "") or ""
                col_strs.append(f"{v}.{col}" if v else col)
            columns_list.append(", ".join(col_strs))
            descriptions.append(getattr(meas, "description", None))

    return pa.table(
        {
            "name": names,
            "aggregation": aggregations,
            "expression": expressions,
            "type": types,
            "columns": columns_list,
            "description": descriptions,
        },
        schema=MEASURES_METADATA_SCHEMA,
    )


def build_metrics_data(model: Any) -> pa.Table:
    """Build a queryable table of all metrics in the semantic model."""
    names: list[str] = []
    metric_types: list[str] = []
    expressions: list[str | None] = []
    measures: list[str | None] = []
    time_dimensions: list[str | None] = []
    time_grains: list[str | None] = []
    windows: list[int | None] = []
    grain_to_dates: list[str | None] = []
    descriptions: list[str | None] = []

    model_dims = getattr(model, "dimensions", None) or {}

    if hasattr(model, "metrics") and model.metrics:
        for met_name, met in model.metrics.items():
            names.append(getattr(met, "label", met_name) or met_name)
            mt = getattr(met, "type", None)
            mt_value_attr = getattr(mt, "value", None)
            mt_value = mt_value_attr if mt_value_attr is not None else str(mt or "derived")
            metric_types.append(mt_value)
            expressions.append(getattr(met, "expression", None))
            measures.append(getattr(met, "measure", None))

            # Cumulative metrics carry ``time_dimension`` directly. PoP metrics
            # hide theirs inside ``period_over_period.time_dimension``. Derived
            # metrics have neither — emit NULL.
            td = getattr(met, "time_dimension", None)
            pop = getattr(met, "period_over_period", None)
            if not td and pop is not None:
                td = getattr(pop, "time_dimension", None)
            time_dimensions.append(td or None)

            # Resolve the grain so ``window=3`` reads as "3 <grain>". PoP
            # carries its own explicit grain; cumulative inherits from the
            # referenced dimension's ``time_grain``.
            grain: str | None = None
            if pop is not None:
                pop_grain = getattr(pop, "grain", None)
                grain = getattr(pop_grain, "value", None)
            if grain is None and td and td in model_dims:
                dim = model_dims[td]
                tg = getattr(dim, "time_grain", None)
                grain = getattr(tg, "value", None)
            time_grains.append(grain)

            win = getattr(met, "window", None)
            windows.append(int(win) if isinstance(win, int) else None)

            gtd = getattr(met, "grain_to_date", None)
            gtd_value_attr = getattr(gtd, "value", None)
            gtd_value = (
                gtd_value_attr if gtd_value_attr is not None else (str(gtd) if gtd else None)
            )
            grain_to_dates.append(gtd_value)

            descriptions.append(getattr(met, "description", None))

    return pa.table(
        {
            "name": names,
            "metric_type": metric_types,
            "expression": expressions,
            "measure": measures,
            "time_dimension": time_dimensions,
            "time_grain": time_grains,
            "window": windows,
            "grain_to_date": grain_to_dates,
            "description": descriptions,
        },
        schema=METRICS_METADATA_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Flight info builders
# ---------------------------------------------------------------------------


def model_to_flight_infos(
    model: Any,
    model_id: str,
    *,
    expose_data_objects: bool = False,
) -> list[flight.FlightInfo]:
    """Convert a SemanticModel to a list of FlightInfo entries.

    By default (``expose_data_objects=False``, the v2.4.0+ behaviour) only
    the semantic virtual table and ``_dimensions / _measures / _metrics``
    metadata views are listed — data-object physical tables are not
    exposed. The ``expose_data_objects`` kwarg is preserved for
    introspection use cases (e.g. building admin tooling) but no callers
    in the shipped surface set it to True. See
    ``design/PLAN_flight_natural_sql.md`` §3.5.
    """
    infos: list[flight.FlightInfo] = []
    if not hasattr(model, "data_objects") or not model.data_objects:
        return infos

    # Always-on: the semantic virtual table (the canonical query surface).
    vt_name = model_virtual_table_name(model)
    vt_schema = model_to_virtual_table_schema(model)
    if len(vt_schema) > 0:
        descriptor = flight.FlightDescriptor.for_path(model_id, vt_name)
        infos.append(flight.FlightInfo(vt_schema, descriptor, [], -1, -1))

    # Opt-in: data-object pass-through tables.
    if expose_data_objects:
        for obj_name, obj in model.data_objects.items():
            schema = object_to_schema(obj)
            descriptor = flight.FlightDescriptor.for_path(model_id, obj_name)
            info = flight.FlightInfo(schema, descriptor, [], -1, -1)
            infos.append(info)

    # Label views — per-category dim/measure/metric labels for BI pickers.
    for vt_name in LABEL_VIEW_NAMES:
        view_schema = virtual_view_schema(model, vt_name)
        if len(view_schema) == 0:
            continue
        descriptor = flight.FlightDescriptor.for_path(model_id, vt_name)
        infos.append(flight.FlightInfo(view_schema, descriptor, [], -1, -1))

    # Metadata views — fixed introspection schemas.
    for vt_name in METADATA_VIEW_NAMES:
        view_schema = virtual_view_schema(model, vt_name)
        descriptor = flight.FlightDescriptor.for_path(model_id, vt_name)
        infos.append(flight.FlightInfo(view_schema, descriptor, [], -1, -1))
    return infos
