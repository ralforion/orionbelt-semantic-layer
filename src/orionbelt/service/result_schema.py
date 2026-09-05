"""The Arrow schema a model *declares* for a query's result.

Separate from what a warehouse happens to return. An engine answers in the
types it has, and those are not always the ones the model named: MySQL has no
boolean, so a declared one arrives as ``int64`` 1; Dremio returns ``date`` as
``date64[ms]`` where the other seven give ``date32[day]``; ClickHouse has no
naive ``DateTime``, so a declared wall clock arrives zoned. Reconciling those
against the declaration is what keeps a column's type a property of the model
rather than of whichever engine is behind it today.

This lived in ``ob_flight`` and so applied only to Flight, which is why REST
and pgwire still hand the driver's type straight through. It is model
knowledge, not transport knowledge, so it belongs here and the driver imports
it - the direction a driver depending on core should run.

pyarrow is imported inside the functions rather than at module scope: it is an
extra rather than a core dependency, and importing this module must not be the
thing that decides whether a deployment has it. See
``db_executor.ensure_arrow`` for who guarantees it is loaded.
"""

from __future__ import annotations

import functools
from typing import Any

#: Arrow's widest ``decimal128`` / ``decimal256`` precision. A declared width
#: past the latter is one OBML permits and Arrow cannot hold.
DECIMAL128_MAX_PRECISION = 38
DECIMAL256_MAX_PRECISION = 76


@functools.cache
def _obml_type_map() -> dict[str, Any]:
    """OBML abstract type -> Arrow type. Covers the full ``DataType`` enum.

    ``int`` maps to ``int64`` rather than ``int32``: OBML's ``int`` is 64-bit,
    and the probe measures engines against that.
    """
    import pyarrow as pa

    return {
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


def obml_type_to_arrow(type_name: str | None) -> Any:
    """Map an OBML type name to an Arrow type, defaulting to utf8."""
    import pyarrow as pa

    if not type_name:
        return pa.utf8()
    return _obml_type_map().get(type_name, pa.utf8())


def decimal_arrow_type(precision: int, scale: int, *, exact: bool = False) -> Any:
    """Arrow decimal type wide enough for ``(precision, scale)``.

    Preserves NUMERIC precision that ``float64`` would round away past ~15-16
    significant digits (issue #136). Prefers ``decimal128`` (max precision 38,
    the broadest Arrow-client support - arrow-js, JDBC, ODBC), widens to
    ``decimal256`` (max 76) once precision exceeds 38, and falls back to
    ``float64`` beyond 76 - a width no real NUMERIC column reaches.

    Unless ``exact`` is set the maximum precision of the chosen width is used,
    so a column whose *sampled* rows under-represent its true magnitude cannot
    overflow a later row. ``exact`` keeps the given precision and is used when
    it comes from an authoritative declared type (the DB enforces the bound).
    """
    import pyarrow as pa

    scale = max(0, scale)
    precision = max(precision, scale, 1)
    if precision <= DECIMAL128_MAX_PRECISION:
        return pa.decimal128(precision if exact else DECIMAL128_MAX_PRECISION, scale)
    if precision <= DECIMAL256_MAX_PRECISION:
        return pa.decimal256(precision if exact else DECIMAL256_MAX_PRECISION, scale)
    return pa.float64()


def numeric_result_arrow_type(item: Any, model: Any) -> Any | None:
    """A governed measure/metric's declared DECIMAL as an exact Arrow decimal.

    The precision/scale come from the model's declared ``dataType`` (e.g.
    ``decimal(18, 2)``), falling back to the model-level
    ``defaultNumericDataType`` - the same source of truth the pgwire NUMERIC
    surface uses (issue #116). Returns ``None`` when the item has no declared
    decimal type, so the caller falls back to ``result_type``.
    """
    from orionbelt.service.db_executor import parse_decimal_type

    declared = getattr(item, "data_type", None)
    if not declared:
        settings = getattr(model, "settings", None)
        declared = getattr(settings, "default_numeric_data_type", None) if settings else None
    if not declared:
        return None
    parsed = parse_decimal_type(declared)
    if parsed is None:
        return None
    precision, scale = parsed
    return decimal_arrow_type(precision, scale, exact=True)


def dimension_label_and_declaration(entry: Any, model: Any) -> tuple[str | None, Any]:
    """The column a selected dimension produces, and the model's declaration.

    Both have to be read the way the compiler reads them, or the schema names
    columns the result does not have:

    * ``"At:day"`` is a grain request. The compiler resolves it through
      :meth:`DimensionRef.parse` and projects ``AS "At"``, so the entry is
      neither the label nor the lookup key - taking it for both named ``At:day``
      as a string beside a result carrying a timestamp called ``At``.
    * a coalesce entry names its output in ``as`` and its inputs in
      ``coalesce``. The alias is the label, and the type is its members', which
      the model requires to agree; looking the alias up in ``model.dimensions``
      finds nothing and called a timestamp a string.
    """
    from orionbelt.models.query import DimensionRef

    if isinstance(entry, str):
        name = DimensionRef.parse(entry).name
        return name, model.dimensions.get(name)
    label = getattr(entry, "alias", None)
    if label is None:
        return None, None
    for member in getattr(entry, "coalesce", None) or []:
        declared = model.dimensions.get(member)
        if declared is not None:
            return label, declared
    return label, None


def declared_result_schema(query: Any, model: Any) -> Any:
    """The Arrow schema *model* declares for *query*, without touching a database.

    Reads ``result_type`` from each selected dimension / measure / metric,
    upgrading governed DECIMAL measures and metrics to an exact Arrow decimal
    (issue #136).
    """
    import pyarrow as pa

    fields: list[Any] = []
    dims = getattr(query.select, "dimensions", [])
    measures = getattr(query.select, "measures", [])
    for entry in dims:
        label, dim = dimension_label_and_declaration(entry, model)
        if label is None:
            continue
        rt = getattr(getattr(dim, "result_type", None), "value", None) or "string"
        fields.append(pa.field(label, obml_type_to_arrow(rt)))
    for label in measures:
        meas = model.measures.get(label)
        met = model.metrics.get(label) if meas is None else None
        decimal_type = numeric_result_arrow_type(meas or met, model)
        if decimal_type is not None:
            fields.append(pa.field(label, decimal_type))
        elif meas is not None:
            rt = getattr(getattr(meas, "result_type", None), "value", None) or "float"
            fields.append(pa.field(label, obml_type_to_arrow(rt)))
        else:
            fields.append(pa.field(label, pa.float64()))
    if getattr(query, "grouping", None) is not None:
        # GROUPING() flag columns - int64, one per dimension. See
        # PLAN_with_rollup.md §"Output: GROUPING() flag columns".
        for entry in dims:
            label, _ = dimension_label_and_declaration(entry, model)
            if label is None:
                continue
            fields.append(pa.field(f"_g_{label}", pa.int64()))
    return pa.schema(fields)
