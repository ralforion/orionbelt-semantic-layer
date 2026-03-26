"""Flight catalog — maps OrionBelt model metadata to Flight schema info."""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pyarrow.flight as flight


# Map OBML abstract types to Arrow types
_OBML_TYPE_MAP: dict[str, pa.DataType] = {
    "string": pa.utf8(),
    "int": pa.int64(),
    "float": pa.float64(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
    "datetime": pa.timestamp("us"),
    "timestamp": pa.timestamp("us"),
}

# ---------------------------------------------------------------------------
# Virtual metadata table schemas
# ---------------------------------------------------------------------------

DIMENSIONS_SCHEMA = pa.schema([
    pa.field("name", pa.utf8()),
    pa.field("data_object", pa.utf8()),
    pa.field("column", pa.utf8()),
    pa.field("type", pa.utf8()),
    pa.field("time_grain", pa.utf8()),
    pa.field("description", pa.utf8()),
])

MEASURES_SCHEMA = pa.schema([
    pa.field("name", pa.utf8()),
    pa.field("aggregation", pa.utf8()),
    pa.field("expression", pa.utf8()),
    pa.field("type", pa.utf8()),
    pa.field("columns", pa.utf8()),
    pa.field("description", pa.utf8()),
])

METRICS_SCHEMA = pa.schema([
    pa.field("name", pa.utf8()),
    pa.field("metric_type", pa.utf8()),
    pa.field("expression", pa.utf8()),
    pa.field("measure", pa.utf8()),
    pa.field("description", pa.utf8()),
])

VIRTUAL_TABLES: dict[str, pa.Schema] = {
    "_dimensions": DIMENSIONS_SCHEMA,
    "_measures": MEASURES_SCHEMA,
    "_metrics": METRICS_SCHEMA,
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
            arrow_type = _OBML_TYPE_MAP.get(abstract_type, pa.utf8())
            label = getattr(col, "label", col_name) or col_name
            fields.append(pa.field(label, arrow_type))
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
            types.append(rt.value if hasattr(rt, "value") else str(rt or "string"))
            tg = getattr(dim, "time_grain", None)
            time_grains.append(tg.value if hasattr(tg, "value") else None)
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
        schema=DIMENSIONS_SCHEMA,
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
            types.append(rt.value if hasattr(rt, "value") else str(rt or "float"))
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
        schema=MEASURES_SCHEMA,
    )


def build_metrics_data(model: Any) -> pa.Table:
    """Build a queryable table of all metrics in the semantic model."""
    names: list[str] = []
    metric_types: list[str] = []
    expressions: list[str | None] = []
    measures: list[str | None] = []
    descriptions: list[str | None] = []

    if hasattr(model, "metrics") and model.metrics:
        for met_name, met in model.metrics.items():
            names.append(getattr(met, "label", met_name) or met_name)
            mt = getattr(met, "type", None)
            metric_types.append(mt.value if hasattr(mt, "value") else str(mt or "derived"))
            expressions.append(getattr(met, "expression", None))
            measures.append(getattr(met, "measure", None))
            descriptions.append(getattr(met, "description", None))

    return pa.table(
        {
            "name": names,
            "metric_type": metric_types,
            "expression": expressions,
            "measure": measures,
            "description": descriptions,
        },
        schema=METRICS_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Flight info builders
# ---------------------------------------------------------------------------

def model_to_flight_infos(
    model: Any,
    model_id: str,
) -> list[flight.FlightInfo]:
    """Convert a SemanticModel to a list of FlightInfo entries.

    One FlightInfo per data object plus virtual metadata tables —
    makes them browsable as "tables" in DBeaver's schema tree.
    """
    infos: list[flight.FlightInfo] = []
    if not hasattr(model, "data_objects") or not model.data_objects:
        return infos
    for obj_name, obj in model.data_objects.items():
        schema = object_to_schema(obj)
        descriptor = flight.FlightDescriptor.for_path(model_id, obj_name)
        info = flight.FlightInfo(schema, descriptor, [], -1, -1)
        infos.append(info)
    # Virtual metadata tables
    for vt_name, vt_schema in VIRTUAL_TABLES.items():
        descriptor = flight.FlightDescriptor.for_path(model_id, vt_name)
        info = flight.FlightInfo(vt_schema, descriptor, [], -1, -1)
        infos.append(info)
    return infos
