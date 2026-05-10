"""Parquet codec for cached query results.

See ``design/PLAN_freshness_driven_cache.md`` §16. Stores rows as columnar
Parquet data with the response envelope (compiled SQL, dialect, explain
block, warnings, physical table list, …) packed into Parquet's key/value
metadata. The result is one self-describing file per cache entry that can
be read back with PyArrow alone — no external schema needed.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class CachedQueryEnvelope:
    """Decoded form of a cached query result.

    ``rows`` are a list-of-lists in the same shape ``QueryExecuteResponse``
    expects. ``columns`` mirrors the column-metadata list. Everything else
    is the supporting envelope reconstituted from Parquet metadata.
    """

    columns: list[dict[str, Any]]
    rows: list[list[Any]]
    sql: str
    dialect: str
    explain: dict[str, Any] | None
    warnings: list[dict[str, Any]]
    sql_valid: bool
    execution_time_ms: float
    timezone: str | None
    resolved: dict[str, Any]
    physical_tables: list[str]
    row_count: int
    cached_at_iso: str | None = None


_METADATA_PREFIX = "obsl_"


def encode(
    *,
    columns: list[dict[str, Any]],
    rows: list[list[Any]],
    sql: str,
    dialect: str,
    explain: dict[str, Any] | None,
    warnings: list[dict[str, Any]],
    sql_valid: bool,
    execution_time_ms: float,
    timezone: str | None,
    resolved: dict[str, Any],
    physical_tables: list[str],
) -> bytes:
    """Serialize a query result envelope as a single Parquet blob."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    column_names = [str(c.get("name", f"col_{i}")) for i, c in enumerate(columns)]
    if rows:
        # Ensure each row has the right arity; pad if shorter.
        normalized_rows = [list(r) + [None] * (len(column_names) - len(r)) for r in rows]
        # Reshape to columns by transposing.
        cols_data: list[list[Any]] = [
            [normalized_rows[r][c] for r in range(len(normalized_rows))]
            for c in range(len(column_names))
        ]
    else:
        cols_data = [[] for _ in column_names]

    arrays = [pa.array(col, from_pandas=False) for col in cols_data]
    table = pa.Table.from_arrays(arrays, names=column_names) if column_names else pa.table({})

    metadata: dict[bytes, bytes] = {}
    payload_map = {
        "columns": columns,
        "sql": sql,
        "dialect": dialect,
        "explain": explain,
        "warnings": warnings,
        "sql_valid": sql_valid,
        "execution_time_ms": execution_time_ms,
        "timezone": timezone,
        "resolved": resolved,
        "physical_tables": physical_tables,
        "row_count": len(rows),
    }
    for k, v in payload_map.items():
        metadata[f"{_METADATA_PREFIX}{k}".encode()] = json.dumps(v, default=str).encode("utf-8")

    table = table.replace_schema_metadata(metadata)
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf, compression="snappy")
    return bytes(buf.getvalue())


def decode(payload: bytes) -> CachedQueryEnvelope:
    """Reverse :func:`encode` — reconstruct the response envelope."""
    import pyarrow.parquet as pq

    reader = pq.ParquetFile(io.BytesIO(payload))
    table = reader.read()
    metadata_bytes = (table.schema.metadata or {}) if table.schema is not None else {}

    def _meta(name: str, default: Any) -> Any:
        key = f"{_METADATA_PREFIX}{name}".encode()
        raw = metadata_bytes.get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return default

    columns = _meta("columns", [])
    sql = _meta("sql", "")
    dialect = _meta("dialect", "")
    explain = _meta("explain", None)
    warnings = _meta("warnings", [])
    sql_valid = bool(_meta("sql_valid", True))
    execution_time_ms = float(_meta("execution_time_ms", 0.0))
    tz_value = _meta("timezone", None)
    resolved = _meta("resolved", {})
    physical_tables = _meta("physical_tables", [])
    row_count = int(_meta("row_count", table.num_rows))

    pylist = table.to_pylist()
    column_names = [str(c.get("name", "")) for c in columns] if columns else table.column_names
    rows: list[list[Any]] = [[row.get(name) for name in column_names] for row in pylist]

    return CachedQueryEnvelope(
        columns=columns,
        rows=rows,
        sql=sql,
        dialect=dialect,
        explain=explain,
        warnings=warnings,
        sql_valid=sql_valid,
        execution_time_ms=execution_time_ms,
        timezone=tz_value if isinstance(tz_value, str) else None,
        resolved=resolved if isinstance(resolved, dict) else {},
        physical_tables=physical_tables if isinstance(physical_tables, list) else [],
        row_count=row_count,
    )
