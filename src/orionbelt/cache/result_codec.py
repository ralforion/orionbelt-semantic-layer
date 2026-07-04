"""Arrow IPC + gzip codec for cached query results.

See ``design/PLAN_arrow_cache.md`` §3. Stores rows as an **uncompressed**
Arrow IPC *stream* with the response envelope (compiled SQL, dialect, explain
block, warnings, physical table list, …) packed into the Arrow schema's
key/value metadata, then gzip-compresses the whole blob. The result is one
self-describing entry per cache key that can be read back with PyArrow alone.

Two deliberate choices, both measured in the plan:

- **Arrow buffers stay uncompressed** (no LZ4/ZSTD at the Arrow layer). There is
  no single Arrow-level codec every reader accepts — arrow-js lacks ZSTD by
  default, DuckDB lacks LZ4 — so compressing at the Arrow layer would break a
  universal byte passthrough. Compression moves to the blob layer instead (§4).
- **gzip at the blob level.** Whole-blob gzip sees cross-buffer redundancy
  (repeated dimension strings) that Arrow's independent per-buffer compression
  can't, so it lands *smaller* than Arrow-level zstd while staying universally
  decodable by every HTTP client (§5).
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from typing import Any

_METADATA_PREFIX = "obsl_"

# gzip level 6: the balance point measured in the plan (§5) — whole-blob gzip
# lands smaller than Arrow-level per-buffer zstd while staying cheap enough to
# hide behind the DB query on a miss.
_GZIP_LEVEL = 6

# Bounded results (LIMIT-capped) serialize as a single record batch. Keep the
# chunk size well above 10k rows to avoid IPC batch-fragmentation overhead
# (§5: batch=10k → +0.1%, batch=100 → +10.4%).
_MAX_CHUNKSIZE = 100_000


@dataclass
class CachedQueryEnvelope:
    """Decoded form of a cached query result.

    ``rows`` are a list-of-lists in the same shape ``QueryExecuteResponse``
    expects. ``columns`` mirrors the column-metadata list. Everything else
    is the supporting envelope reconstituted from Arrow schema metadata.
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
    # Raw stored blob, attached by the cache-read layer for byte-passthrough
    # (serving a raw-arrow hit verbatim). Not populated by :func:`decode`.
    raw_payload: bytes | None = None


def build_result_table(column_names: list[str], rows: list[list[Any]]) -> Any:
    """Build a pyarrow Table from result column names + list-of-lists rows.

    Rows are padded to the column arity and transposed into columns. Types are
    inferred by pyarrow from the (already JSON-serializable) cell values — the
    same typed, locale-neutral shape the executor produces, so a cached entry
    and a fresh execution serialize identically.
    """
    import pyarrow as pa

    if not column_names:
        return pa.table({})
    width = len(column_names)
    if rows:
        normalized = [list(r) + [None] * (width - len(r)) for r in rows]
        cols_data: list[list[Any]] = [
            [normalized[r][c] for r in range(len(normalized))] for c in range(width)
        ]
    else:
        cols_data = [[] for _ in column_names]
    arrays = [pa.array(col, from_pandas=False) for col in cols_data]
    return pa.Table.from_arrays(arrays, names=list(column_names))


def to_ipc_stream(table: Any) -> bytes:
    """Serialize a table as an **uncompressed** Arrow IPC stream.

    No Arrow-level buffer compression: the buffers stay raw so every reader
    (pyarrow, arrow-js, DuckDB, Rust, Go) can decode them; compression happens
    at the blob/transport layer instead (§4).
    """
    import pyarrow as pa
    import pyarrow.ipc as ipc

    sink = pa.BufferOutputStream()
    writer = ipc.new_stream(sink, table.schema)
    for batch in table.to_batches(max_chunksize=_MAX_CHUNKSIZE):
        writer.write_batch(batch)
    writer.close()
    raw: bytes = sink.getvalue().to_pybytes()
    return raw


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
    cached: bool = False,
    cached_at: str | None = None,
) -> bytes:
    """Serialize a query result envelope as a gzip'd Arrow IPC stream blob.

    ``cached`` / ``cached_at`` are response-level flags: the cache-storage path
    leaves them at their defaults (a stored blob is not itself "a cache hit"),
    while the ``format=arrow`` wire response sets them so a self-describing Arrow
    client (e.g. the UI) can tell a hit from a miss, matching the JSON response.
    """
    column_names = [str(c.get("name", f"col_{i}")) for i, c in enumerate(columns)]
    table = build_result_table(column_names, rows)

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
        "cached": cached,
        "cached_at": cached_at,
    }
    metadata: dict[bytes, bytes] = {
        f"{_METADATA_PREFIX}{k}".encode(): json.dumps(v, default=str).encode("utf-8")
        for k, v in payload_map.items()
    }
    table = table.replace_schema_metadata(metadata)
    return gzip.compress(to_ipc_stream(table), _GZIP_LEVEL)


def read_envelope(table: Any) -> dict[str, Any]:
    """Extract the ``obsl_`` envelope (sql, columns, explain, …) from a decoded
    table's schema metadata into a plain dict, stripping the key prefix.

    Lets a client that read the Arrow stream itself (e.g. the Gradio UI over the
    ``format=arrow`` endpoint) recover the same fields the JSON response carries,
    without re-deriving the prefix/JSON handling. Unknown/undecodable keys are
    skipped.
    """
    md = (table.schema.metadata or {}) if table.schema is not None else {}
    prefix = _METADATA_PREFIX.encode()
    out: dict[str, Any] = {}
    for key, raw in md.items():
        if not key.startswith(prefix):
            continue
        name = key[len(prefix) :].decode("utf-8", "ignore")
        try:
            out[name] = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
    return out


def decode_table(payload: bytes) -> Any:
    """Decode a cached blob to the raw pyarrow ``Table`` (columnar data only).

    The Flight surface streams the ``pa.Table`` directly rather than rebuilding
    a response envelope, so it skips the row/metadata reconstruction :func:`decode`
    does. The envelope still rides in the table's schema metadata (harmless to
    carry along). Shares the exact byte format :func:`encode` writes, so a
    Flight reader consumes a REST/pgwire writer's entry and vice versa.
    """
    import pyarrow as pa
    import pyarrow.ipc as ipc

    raw = gzip.decompress(payload)
    with ipc.open_stream(pa.BufferReader(raw)) as reader:
        return reader.read_all()


def decode(payload: bytes) -> CachedQueryEnvelope:
    """Reverse :func:`encode` — reconstruct the response envelope."""
    import pyarrow as pa
    import pyarrow.ipc as ipc

    raw = gzip.decompress(payload)
    with ipc.open_stream(pa.BufferReader(raw)) as reader:
        table = reader.read_all()
    metadata_bytes = (table.schema.metadata or {}) if table.schema is not None else {}

    def _meta(name: str, default: Any) -> Any:
        key = f"{_METADATA_PREFIX}{name}".encode()
        raw_meta = metadata_bytes.get(key)
        if raw_meta is None:
            return default
        try:
            return json.loads(raw_meta.decode("utf-8"))
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
