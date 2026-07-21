"""Arrow conversion utilities for Flight SQL results."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
from ob_driver_core.type_codes import (  # type: ignore[import-untyped]
    BINARY,
    DATETIME,
    NUMBER,
    STRING,
)


def pep249_type_to_arrow(type_code: Any) -> pa.DataType:
    """Map a PEP 249 type code object to an Arrow data type.

    Uses equality comparison — PEP 249 type objects support __eq__ for membership testing.
    """
    if type_code == NUMBER:
        return pa.float64()
    if type_code == STRING:
        return pa.utf8()
    if type_code == DATETIME:
        return pa.timestamp("us")
    if type_code == BINARY:
        return pa.binary()
    return pa.utf8()  # fallback


# pyarrow decimal128 tops out at precision 38, decimal256 at 76.
_DECIMAL128_MAX_PRECISION = 38
_DECIMAL256_MAX_PRECISION = 76


def _decimal_precision_scale(value: Decimal) -> tuple[int, int]:
    """Return the ``(precision, scale)`` needed to represent a Decimal exactly.

    ``precision`` is the count of significant digits (always ``>= scale``);
    ``scale`` is the number of fractional digits (0 for integers / specials).
    """
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):  # NaN / Infinity
        return (1, 0)
    ndigits = len(value.as_tuple().digits)
    if exponent >= 0:
        # e.g. 12E3 == 12000 → 5 integer digits, no fraction.
        return (ndigits + exponent, 0)
    scale = -exponent
    return (max(ndigits, scale), scale)


def decimal_arrow_type(precision: int, scale: int, *, exact: bool = False) -> pa.DataType:
    """Arrow decimal type wide enough for ``(precision, scale)``.

    Preserves NUMERIC precision that ``float64`` would round away past ~15-16
    significant digits (issue #136). Prefers ``decimal128`` (max precision 38,
    the broadest Arrow-client support — arrow-js, JDBC, ODBC), widens to
    ``decimal256`` (max 76) once precision exceeds 38, and falls back to
    ``float64`` beyond 76 — a width no real NUMERIC column reaches.

    Unless ``exact`` is set the maximum precision of the chosen width is used, so
    a column whose *sampled* rows under-represent its true magnitude cannot
    overflow a later row. ``exact`` keeps the given precision and is used when it
    comes from an authoritative declared type (the DB enforces the bound).
    """
    scale = max(0, scale)
    precision = max(precision, scale, 1)
    if precision <= _DECIMAL128_MAX_PRECISION:
        return pa.decimal128(precision if exact else _DECIMAL128_MAX_PRECISION, scale)
    if precision <= _DECIMAL256_MAX_PRECISION:
        return pa.decimal256(precision if exact else _DECIMAL256_MAX_PRECISION, scale)
    return pa.float64()


def _decimal_column_type(col: tuple[Any, ...], sampled: list[Any]) -> pa.DataType:
    """Arrow type for a DECIMAL column from its PEP 249 description + samples.

    A column's first fetched batch can under-represent its declared width (e.g.
    a ``NUMERIC(40, 2)`` whose early rows are all small), so the driver-reported
    precision/scale (``description[4]`` / ``[5]``) — which bound *every* row —
    take priority. The sampled width is combined in (via ``max``) so neither an
    under-sampled batch nor an under-reported description can narrow the type,
    then the width is chosen by :func:`decimal_arrow_type` (decimal128 ≤ 38,
    decimal256 ≤ 76, float64 beyond). See issue #136.
    """
    ps = [_decimal_precision_scale(v) for v in sampled if isinstance(v, Decimal)]
    scale = max((s for _, s in ps), default=0)
    int_digits = max((p - s for p, s in ps), default=0)
    desc_precision = col[4] if len(col) > 4 else None
    desc_scale = col[5] if len(col) > 5 else None
    if isinstance(desc_scale, int) and desc_scale >= 0:
        scale = max(scale, desc_scale)
        if isinstance(desc_precision, int) and desc_precision > desc_scale:
            int_digits = max(int_digits, desc_precision - desc_scale)
    return decimal_arrow_type(int_digits + scale, scale)


def _python_type_to_arrow(value: Any) -> pa.DataType:
    """Infer Arrow type from a Python value (fallback when type codes are unreliable)."""
    if isinstance(value, bool):
        return pa.bool_()
    if isinstance(value, int):
        return pa.int64()
    if isinstance(value, float):
        return pa.float64()
    if isinstance(value, Decimal):
        return decimal_arrow_type(*_decimal_precision_scale(value))
    if isinstance(value, datetime.datetime):
        return pa.timestamp("us")
    if isinstance(value, datetime.date):
        return pa.date32()
    if isinstance(value, datetime.time):
        return pa.time64("us")
    if isinstance(value, bytes):
        return pa.binary()
    return pa.utf8()


def schema_from_description(
    description: tuple[tuple[Any, ...], ...],
    sample_row: tuple[Any, ...] | None = None,
    sample_rows: list[tuple[Any, ...]] | None = None,
) -> pa.Schema:
    """Build an Arrow Schema from PEP 249 cursor.description.

    Each description entry is a 7-tuple: (name, type_code, ...).
    When sample_rows (or sample_row) is provided, Python value types are used
    as the primary type source (more reliable than type_code with ADBC drivers).

    For UNION ALL queries with NULL padding, a single sample row may have None
    for some columns. Passing sample_rows allows scanning multiple rows to find
    the first non-None value per column.

    For DECIMAL columns the Arrow precision/scale come from the driver-reported
    ``description[4]`` / ``[5]`` when present (they bound every row), combined
    with the widest sampled value as a fallback — so a column whose first batch
    under-represents its declared width is still typed wide enough. The width
    jumps to decimal256 past precision 38 and to float64 past 76 (issue #136).
    """
    # Normalize: prefer sample_rows, fall back to single sample_row
    rows = sample_rows or ([sample_row] if sample_row is not None else [])

    fields = []
    for i, col in enumerate(description):
        name = col[0]
        # Collect this column's non-None sample values (across rows).
        sampled = [row[i] for row in rows if i < len(row) and row[i] is not None]
        if sampled and isinstance(sampled[0], Decimal):
            arrow_type = _decimal_column_type(col, sampled)
        elif sampled:
            arrow_type = _python_type_to_arrow(sampled[0])
        else:
            arrow_type = pep249_type_to_arrow(col[1])
        fields.append(pa.field(name, arrow_type))
    return pa.schema(fields)


def rows_to_batch(
    rows: list[tuple[Any, ...]],
    schema: pa.Schema,
) -> pa.RecordBatch:
    """Convert a list of row tuples to an Arrow RecordBatch.

    Transposes row-major data to column-major for Arrow.
    """
    if not rows:
        return pa.RecordBatch.from_pydict({field.name: [] for field in schema}, schema=schema)
    n_cols = len(schema)
    # Columns whose Arrow type is floating but whose Python values are Decimal —
    # the pathological ``decimal_arrow_type`` fallback (precision > 76). pyarrow
    # won't build a float array straight from Decimals, so coerce those cells so
    # delivery degrades (lossily) instead of raising.
    float_cols = {i for i in range(n_cols) if pa.types.is_floating(schema.field(i).type)}
    columns: dict[str, list[Any]] = {schema.field(i).name: [] for i in range(n_cols)}
    for row in rows:
        for i in range(n_cols):
            col_name = schema.field(i).name
            value = row[i] if i < len(row) else None
            if i in float_cols and isinstance(value, Decimal):
                value = float(value)
            columns[col_name].append(value)
    return pa.RecordBatch.from_pydict(columns, schema=schema)


def cursor_to_batches(
    cursor: Any,
    schema: pa.Schema,
    batch_size: int = 1024,
) -> list[pa.RecordBatch]:
    """Fetch all rows from a PEP 249 cursor as Arrow RecordBatches.

    Fetches in chunks of batch_size to manage memory.
    Returns a list of RecordBatches (not a generator, for simplicity).
    """
    batches: list[pa.RecordBatch] = []
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        batches.append(rows_to_batch(rows, schema))
    return batches
