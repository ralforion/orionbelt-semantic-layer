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

import contextlib
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


# Substrings that mark an extension's ``type_name`` as numeric. Mirrors
# ``service.value_formatting._NUMERIC_TYPE_TOKENS``, which the cache layer may
# not import (``tests/architecture/test_dependencies.py`` forbids cache ->
# service; Flight reaches this module through the cache). The two are pinned
# equal by ``test_numeric_tokens_match_the_service_definition``.
_NUMERIC_TYPE_TOKENS = ("number", "int", "float", "decimal", "numeric", "double", "real")

# Type names a token matches without the type being numeric. The tokens are
# substrings because the numeric names are a family rather than a list -
# ``bigint``, ``smallint`` and ``integer`` all have to match ``int`` - and
# ``interval`` is what that costs: it contains ``int`` and is a duration.
_NOT_NUMERIC_TYPE_NAMES = ("interval",)


def _is_string_backed_numeric(arrow_type: Any) -> bool:
    """Whether the type is an Arrow extension wrapping a *number* as a string.

    ADBC's PostgreSQL driver represents NUMERIC as
    ``arrow.opaque[storage_type=string, type_name=numeric]`` to keep precision
    Arrow's ``decimal128`` cannot hold, and the executor parses those cells back
    to ``Decimal`` before they reach this module. Duck-typed on the
    ``storage_type`` / ``type_name`` pair that plain Arrow types do not carry -
    the same detection ``db_executor._is_string_stored_numeric_arrow_type``
    makes.

    The ``type_name`` test is what keeps this narrow, and it is load-bearing
    rather than defensive. An opaque ``json``, ``uuid`` or ``interval`` is
    string-backed too, and its cells stay strings all the way here, so
    ``string`` is the right offer for it and refusing one would make *those*
    columns value-dependent instead - inferred ``string`` when populated and
    ``null`` when empty.

    ``interval`` needs saying twice because the tokens are substrings: the
    numeric names are a family, so ``int`` has to match ``bigint`` and
    ``integer``, and it matches ``interval`` on the way past.
    """
    import pyarrow as pa

    try:
        storage = getattr(arrow_type, "storage_type", None)
        type_name = getattr(arrow_type, "type_name", None)
        if storage is None or type_name is None:
            return False
        if not pa.types.is_string(storage):
            return False
        if isinstance(type_name, bytes):
            type_name = type_name.decode("utf-8", "ignore")
        name = str(type_name).lower()
        if any(excluded in name for excluded in _NOT_NUMERIC_TYPE_NAMES):
            return False
        return any(tok in name for tok in _NUMERIC_TYPE_TOKENS)
    except (AttributeError, TypeError):
        return False


def _serialized_field_type(arrow_type: Any) -> Any:
    """The Arrow type a column has *after* the executor serialises its cells,
    or ``None`` where the declaration says nothing the blob can use.

    ``encode_data`` is handed rows that already went through
    ``_serialize_value``: temporals become ISO strings, binary becomes base64,
    intervals become ``str(timedelta)``. Numerics, booleans, strings and
    ``Decimal`` pass through untouched. This maps a driver's Arrow type onto
    the type its *serialised* values carry, which is the type the blob can
    actually hold - naming the driver's ``timestamp[us]`` here would be a
    declaration no serialised row could satisfy.

    A string-backed numeric is the one case with no usable answer, and
    ``None`` rather than ``string`` is what keeps it honest. Its cells arrive as
    ``Decimal``, so ``string`` is refused wherever there are values and accepted
    wherever there are none - the same column typed ``decimal128`` for one
    filter and ``string`` for another, which is the instability this schema
    exists to remove. A width cannot be invented instead: ADBC's opaque type
    carries none, PostgreSQL reports typmod ``-1`` for a computed expression,
    and offering a fixed one would rescale the value - ``1.50`` stored under
    ``decimal128(38, 9)`` reads back ``1.500000000``, which is what a cache hit
    would then render. So the column is inferred where it has values and left
    ``null`` where it has none, and the entry's column sidecar carries the
    ``number`` type and its format either way.
    """
    import pyarrow as pa

    if _is_string_backed_numeric(arrow_type):
        return None
    try:
        if (
            pa.types.is_integer(arrow_type)
            or pa.types.is_floating(arrow_type)
            or pa.types.is_boolean(arrow_type)
            or pa.types.is_decimal(arrow_type)
        ):
            return arrow_type
    except (AttributeError, TypeError):
        return pa.string()
    return pa.string()


def build_result_table(column_names: list[str], rows: list[list[Any]], schema: Any = None) -> Any:
    """Build a pyarrow Table from result column names + list-of-lists rows.

    Rows are padded to the column arity and transposed into columns.

    ``schema`` is the *driver* Arrow schema (from ``ExecutionResult``), and
    where it is given it **decides** each column's type rather than merely
    rescuing the ones inference would type as ``null``. Inference reads the
    values that happen to be present, so a ``decimal(18, 2)`` measure holding
    1.50 and 2.25 came back ``decimal128(3, 2)``, and the same column came back
    a different width for a different filter: a consumer that read the schema
    once was wrong about the next result. That is the failure #393 fixed on
    MySQL and #407 on Snowflake, arriving here by a third route, and the fix is
    the same one - read the width from the declaration, not from the rows.

    The declared type is only *offered*: rows have already been through
    ``_serialize_value``, so a column whose values no longer fit what the driver
    declared falls back to inference. pyarrow raises on every such mismatch
    rather than coercing (measured: a Decimal into a string array, a value wider
    than the declared precision, an integer too large for the declared width),
    so the fallback cannot silently change a value.

    One declaration is refused before it is offered. A string-backed numeric -
    PostgreSQL NUMERIC under ADBC - carries no width to read and its cells
    arrive as ``Decimal``, so ``_serialized_field_type`` answers ``None`` for it
    and the column is inferred, exactly as it was before this. Offering
    ``string`` there would have been accepted by an empty result and refused by
    a populated one, which is the instability this schema exists to remove.

    Without a schema, types are inferred as before. That is the PEP 249 path,
    which has no Arrow schema to read, and the ``format_values`` arrow response,
    where every cell is a display string by construction.

    Types are matched by position, since the caller's ``column_names`` are the
    model-decorated names for the same columns in the same order.
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

    hints: list[Any] = []
    if schema is not None and len(schema) == width:
        hints = [_serialized_field_type(f.type) for f in schema]

    arrays = []
    for i, col in enumerate(cols_data):
        arr = None
        if hints and hints[i] is not None and not pa.types.is_null(hints[i]):
            with contextlib.suppress(pa.ArrowInvalid, pa.ArrowTypeError, TypeError, ValueError):
                arr = pa.array(col, type=hints[i], from_pandas=False)
        if arr is None:
            arr = pa.array(col, from_pandas=False)
        arrays.append(arr)
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


def encode_data(column_names: list[str], rows: list[list[Any]], schema: Any = None) -> bytes:
    """Serialize row data as a gzip'd Arrow IPC stream blob (data only).

    No response envelope is baked in — the blob is a pure Arrow data stream. The
    caller stores this in the cache; metadata is rebuilt fresh on every read.
    ``schema`` is the executor's driver Arrow schema, and it decides the
    column types (see :func:`build_result_table`); without one they are
    inferred from the values. A caller that already holds a fully-typed table
    should use :func:`encode_table` instead, which keeps the table's own schema
    with no serialisation step in between.
    """
    table = build_result_table(column_names, rows, schema)
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


def warm() -> None:
    """Warm the encode/decode path so the first real cache hit isn't slow.

    ``decode_data`` imports pyarrow lazily (the C-extension load alone is
    ~100-250ms), so the first cache *hit* pays that one-time cost inside the
    timed fetch and reports an inflated ``execution_time_ms`` (issue: first hit
    on a freshly-started Cloud Run instance shows hundreds of ms, every
    subsequent hit is single-digit). Running a tiny round-trip at startup loads
    pyarrow and warms the decode path off the request hot path. Best-effort: a
    failure here must never block startup.
    """
    encode_data(["_"], [[0]])
    decode_data(encode_data(["_"], [[0]]))
