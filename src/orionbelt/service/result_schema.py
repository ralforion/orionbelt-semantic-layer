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


def declared_arrow_types(model: Any, query: Any = None) -> dict[str, Any]:
    """Column label -> the Arrow type declared for it.

    Keyed by name rather than built from a query, because the surfaces that
    need it have the executor's columns and not always the ``QueryObject``.
    A column absent from the map - a raw ``select.fields`` projection of a
    physical column, a ``GROUPING()`` flag - is one the model makes no claim
    about, and is left alone.

    *query* adds the labels only a query can name. A coalesce entry outputs its
    ``as`` alias, which is not a model dimension, so a model-only map never
    mentions it and a coalesced date or boolean keeps whatever the engine
    returned - this reconciliation's own gap, reappearing for exactly the
    columns a query invents. The alias takes its type from its members, which
    the model requires to agree.
    """
    types: dict[str, Any] = {}
    for label, dim in model.dimensions.items():
        rt = getattr(getattr(dim, "result_type", None), "value", None)
        if rt:
            types[label] = obml_type_to_arrow(rt)
    for label, item in list(model.measures.items()) + list(model.metrics.items()):
        decimal_type = numeric_result_arrow_type(item, model)
        if decimal_type is not None:
            types[label] = decimal_type
            continue
        rt = getattr(getattr(item, "result_type", None), "value", None)
        if rt:
            types[label] = obml_type_to_arrow(rt)
    if query is not None:
        select = getattr(query, "select", None)
        for entry in getattr(select, "dimensions", None) or []:
            if isinstance(entry, str):
                continue  # a plain name, already in the model map under it
            label, declared = dimension_label_and_declaration(entry, model)
            rt = getattr(getattr(declared, "result_type", None), "value", None)
            if label and rt:
                types[label] = obml_type_to_arrow(rt)
    return types


def _bool_is_safe(column: Any) -> bool:
    """Whether an integer column holds only 0, 1 and NULL.

    Arrow's ``int -> bool`` cast maps every nonzero to ``True``, so a column
    holding 7 would be asserted ``true`` by a plain cast. That is a judgement
    about data the model did not make: the declaration says the column is a
    boolean, and a 7 says it is not one yet. Reconciling only the values whose
    meaning is unambiguous, and reporting the rest, keeps this a change of
    representation rather than of content.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    if column.null_count == len(column):
        return True
    try:
        distinct = pc.unique(
            column.combine_chunks() if hasattr(column, "combine_chunks") else column
        )
        return all(v.as_py() in (0, 1, None) for v in distinct)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError, AttributeError):
        return False


def _recoverable(actual: Any, target: Any) -> bool:
    """Whether casting *actual* to *target* recovers the declaration losslessly.

    An allowlist, not a general "types differ" test, and the difference is not
    academic. The general version cast a stored ``decimal128(38, 2)`` down to
    ``float64`` because the measure declares ``resultType: float`` - rounding
    123456789012345678.90 to 1.2345678901234568e+17 on a cache hit. A
    ``resultType`` is a statement about a *family*, not an instruction to
    narrow the engine's answer to the coarsest member of it, and an engine
    returning something more precise than the declaration is not drift.

    So only the cases where an engine could not express the declared type at
    all are reconciled:

    * a declared boolean over an integer - MySQL has no boolean type, so a
      declared one arrives as ``int64``. The caller additionally checks the
      values are 0/1/NULL before trusting it.
    * a declared date over a wider date - Dremio returns ``date64[ms]`` where
      the other seven give ``date32[day]``, and a date has no sub-day part to
      lose.

    Timestamps are deliberately absent. ClickHouse's zoned-for-naive case is
    the same shape but needs the wall clock preserved value by value (#407,
    ``pc.local_timestamp``); a plain cast converts to UTC and moves the clock.
    """
    import pyarrow as pa

    if pa.types.is_boolean(target) and pa.types.is_integer(actual):
        return True
    return bool(pa.types.is_date(target) and pa.types.is_date(actual))


def reconcile_to_declared(
    table: Any, declared: dict[str, Any]
) -> tuple[Any, list[tuple[str, str]]]:
    """Cast *table*'s columns to the types *declared* names for them.

    Returns the table and one ``(column, reason)`` pair per column that named a
    reconcilable difference and could not be reconciled anyway, so a surface
    can report what it could not apply instead of silently not applying it -
    Flight's equivalent is silent, and a mismatch there is invisible.

    Only the differences :func:`_recoverable` admits are acted on. A column the
    model does not name, or one whose type differs in a way that is not an
    engine failing to express the declaration, is passed through untouched and
    unreported.
    """
    import pyarrow as pa

    skipped: list[tuple[str, str]] = []
    fields: list[Any] = []
    changed = False
    for field in table.schema:
        target = declared.get(field.name)
        if target is None or field.type.equals(target) or not _recoverable(field.type, target):
            fields.append(field)
            continue
        if (
            pa.types.is_boolean(target)
            and pa.types.is_integer(field.type)
            and not _bool_is_safe(table.column(field.name))
        ):
            skipped.append(
                (
                    field.name,
                    f"declared boolean but the column holds values other than "
                    f"0 and 1; left as {field.type}",
                )
            )
            fields.append(field)
            continue
        fields.append(pa.field(field.name, target))
        changed = True
    if not changed:
        return table, skipped
    try:
        return table.cast(pa.schema(fields)), skipped
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
        # One column's cast failed and Arrow casts a table whole, so fall back
        # to per-column so a single bad column cannot drop every other
        # reconciliation on the result.
        return _reconcile_per_column(table, declared, skipped, str(exc))


def _reconcile_per_column(
    table: Any, declared: dict[str, Any], skipped: list[tuple[str, str]], whole_error: str
) -> tuple[Any, list[tuple[str, str]]]:
    """Cast column by column, keeping the ones that work."""
    import pyarrow as pa

    already = {name for name, _ in skipped}
    columns: list[Any] = []
    fields: list[Any] = []
    for field in table.schema:
        column = table.column(field.name)
        target = declared.get(field.name)
        if (
            target is None
            or field.type.equals(target)
            or field.name in already
            or not _recoverable(field.type, target)
        ):
            columns.append(column)
            fields.append(field)
            continue
        try:
            columns.append(column.cast(target))
            fields.append(pa.field(field.name, target))
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError):
            skipped.append((field.name, f"cannot cast {field.type} to {target}"))
            columns.append(column)
            fields.append(field)
    return pa.Table.from_arrays(columns, schema=pa.schema(fields)), skipped


def reconciliation_possible(declared: dict[str, Any]) -> bool:
    """Whether any declared type is one :func:`_recoverable` could act on.

    Answered from the declaration alone, without looking at data, so a caller
    holding an undecoded blob can decide whether it is safe to pass through.
    That matters because the cache is shared: an entry written by a surface
    that does not reconcile - pgwire passes no declared types - is stored in
    the engine's types, and shipping it verbatim to a client whose sidecar
    says ``boolean`` hands over data that contradicts its own metadata.

    Conservative in the useful direction: it says "possible" for any model that
    declares a boolean or a date, whether or not the engine got them wrong. The
    cost of a false positive is one decode; the cost of a false negative is a
    wrong answer.
    """
    import pyarrow as pa

    return any(pa.types.is_boolean(t) or pa.types.is_date(t) for t in declared.values())
