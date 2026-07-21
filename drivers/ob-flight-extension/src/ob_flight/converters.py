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


# pyarrow decimal128 tops out at precision 38, decimal256 at 76. We advertise
# the maximum precision so a column's magnitude cannot overflow the inferred
# type for realistic NUMERIC data, and prefer decimal128 (universally supported
# by Arrow clients — arrow-js, JDBC, ODBC) unless the scale needs more room.
_DECIMAL128_MAX_PRECISION = 38
_DECIMAL256_MAX_PRECISION = 76


def _decimal_scale(value: Decimal) -> int:
    """Number of fractional digits in a Decimal (0 for integers or specials)."""
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def decimal_arrow_type(scale: int) -> pa.DataType:
    """Widest safe Arrow decimal type for a column with the given fractional scale.

    Preserves NUMERIC precision that ``float64`` would round away past ~15-16
    significant digits (issue #136). Uses the maximum precision so the column's
    integer magnitude cannot overflow the type for realistic data, preferring
    ``decimal128`` and only widening to ``decimal256`` when the scale is large.
    Falls back to ``float64`` for pathological scales neither decimal represents.
    """
    scale = max(0, scale)
    if scale <= _DECIMAL128_MAX_PRECISION:
        return pa.decimal128(_DECIMAL128_MAX_PRECISION, scale)
    if scale <= _DECIMAL256_MAX_PRECISION:
        return pa.decimal256(_DECIMAL256_MAX_PRECISION, scale)
    return pa.float64()


def _python_type_to_arrow(value: Any) -> pa.DataType:
    """Infer Arrow type from a Python value (fallback when type codes are unreliable)."""
    if isinstance(value, bool):
        return pa.bool_()
    if isinstance(value, int):
        return pa.int64()
    if isinstance(value, float):
        return pa.float64()
    if isinstance(value, Decimal):
        return decimal_arrow_type(_decimal_scale(value))
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

    For DECIMAL columns the Arrow scale is taken from the widest sampled value
    so it covers every value in the column — a fixed-scale ``NUMERIC(p, s)``
    column reports a stable scale, so the sample is representative (issue #136).
    """
    # Normalize: prefer sample_rows, fall back to single sample_row
    rows = sample_rows or ([sample_row] if sample_row is not None else [])

    fields = []
    for i, col in enumerate(description):
        name = col[0]
        # Collect this column's non-None sample values (across rows).
        sampled = [row[i] for row in rows if i < len(row) and row[i] is not None]
        if sampled and isinstance(sampled[0], Decimal):
            # Cover the widest sampled scale so no value overflows the type.
            max_scale = max(_decimal_scale(v) for v in sampled if isinstance(v, Decimal))
            arrow_type = decimal_arrow_type(max_scale)
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
    columns: dict[str, list[Any]] = {schema.field(i).name: [] for i in range(n_cols)}
    for row in rows:
        for i in range(n_cols):
            col_name = schema.field(i).name
            columns[col_name].append(row[i] if i < len(row) else None)
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
