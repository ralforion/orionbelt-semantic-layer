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


def _python_type_to_arrow(value: Any) -> pa.DataType:
    """Infer Arrow type from a Python value (fallback when type codes are unreliable)."""
    if isinstance(value, bool):
        return pa.bool_()
    if isinstance(value, int):
        return pa.int64()
    if isinstance(value, float):
        return pa.float64()
    if isinstance(value, Decimal):
        return pa.float64()
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
    """
    # Normalize: prefer sample_rows, fall back to single sample_row
    rows = sample_rows or ([sample_row] if sample_row is not None else [])

    fields = []
    for i, col in enumerate(description):
        name = col[0]
        # Scan rows for first non-None value in this column
        value = None
        for row in rows:
            if i < len(row) and row[i] is not None:
                value = row[i]
                break
        if value is not None:
            arrow_type = _python_type_to_arrow(value)
        else:
            type_code = col[1]
            arrow_type = pep249_type_to_arrow(type_code)
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
