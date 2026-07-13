"""Arrow IPC + gzip codec for cached query results.

See ``design/PLAN_arrow_cache.md`` §3. The cache stores **only the row data** as
an **uncompressed** Arrow IPC *stream* (column names + inferred arrow types +
rows), then gzip-compresses the blob. Response metadata (compiled SQL, dialect,
explain block, warnings, timing, ``cached`` flag, …) is **not** cached — every
surface rebuilds it fresh per request from the compile result + model, so
per-request fields (``execution_time_ms``, ``cached``) are correct by
construction on a cache hit. The stored blob is a pure, self-describing Arrow
data stream readable with PyArrow alone.

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
from typing import Any

# gzip level 6: the balance point measured in the plan (§5) — whole-blob gzip
# lands smaller than Arrow-level per-buffer zstd while staying cheap enough to
# hide behind the DB query on a miss.
_GZIP_LEVEL = 6

# Bounded results (LIMIT-capped) serialize as a single record batch. Keep the
# chunk size well above 10k rows to avoid IPC batch-fragmentation overhead
# (§5: batch=10k → +0.1%, batch=100 → +10.4%).
_MAX_CHUNKSIZE = 100_000


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


def encode_data(column_names: list[str], rows: list[list[Any]]) -> bytes:
    """Serialize row data as a gzip'd Arrow IPC stream blob (data only).

    No response envelope is baked in — the blob is a pure Arrow data stream. The
    caller stores this in the cache; metadata is rebuilt fresh on every read.
    Types are inferred from the row values (see :func:`build_result_table`); a
    caller that already holds a fully-typed table should use
    :func:`encode_table` instead to preserve the exact schema.
    """
    table = build_result_table(column_names, rows)
    return gzip.compress(to_ipc_stream(table), _GZIP_LEVEL)


def encode_table(table: Any) -> bytes:
    """Serialize a pyarrow ``Table`` as a gzip'd Arrow IPC blob, keeping its
    exact schema.

    Unlike :func:`encode_data` — which rebuilds the table from column names +
    Python rows and so *re-infers* Arrow types — this preserves the caller's
    original types. Use it when the caller already holds a fully-typed table
    (e.g. Flight, whose warehouse driver returns typed columns): re-inference
    would collapse an empty / all-null ``int64``/``string`` result to
    ``null``-typed columns, so a cache hit would stream a schema that no longer
    matches the fresh / advertised one. The byte format is identical to
    :func:`encode_data`'s, so :func:`decode_data` reads either.
    """
    return gzip.compress(to_ipc_stream(table), _GZIP_LEVEL)


def decode_data(payload: bytes) -> Any:
    """Decode a cached blob to the raw pyarrow ``Table`` (columnar data only).

    Shares the exact byte format :func:`encode_data` writes, so any reader
    (REST, pgwire, Flight) consumes an entry written by any writer. The envelope
    is reconstructed by the caller from the compile result, not from the blob.
    """
    import pyarrow as pa
    import pyarrow.ipc as ipc

    raw = gzip.decompress(payload)
    with ipc.open_stream(pa.BufferReader(raw)) as reader:
        return reader.read_all()


def table_to_rows(table: Any) -> list[list[Any]]:
    """Return a decoded table's rows as list-of-lists in schema column order."""
    names = table.column_names
    return [[row.get(n) for n in names] for row in table.to_pylist()]
