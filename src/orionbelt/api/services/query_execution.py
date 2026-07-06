"""Query-execution helpers and core logic extracted from the session router."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import HTTPException, Response

from orionbelt.api.deps import (
    CacheRuntimeConfig,
    get_default_locale,
)
from orionbelt.api.query_cache import (
    build_explain_response,
    build_format_map,
    build_type_map,
    exec_columns_from_table,
    execute_query_with_cache,
    execution_result_from_data,
)
from orionbelt.api.schemas import (
    ColumnMetadata,
    ExplainPlanResponse,
    QueryExecuteResponse,
    ResolvedInfoResponse,
)
from orionbelt.api.warnings_adapter import semantic_error_to_warning
from orionbelt.cache.protocol import Cache
from orionbelt.compiler.validator import format_sql
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionUnavailableError,
    resolve_timezone,
)
from orionbelt.service.model_store import ModelStore
from orionbelt.service.value_formatting import format_row, to_tsv

# Column type/format helpers live in orionbelt.api.query_cache (shared with the
# pgwire/Flight cache path). Re-exported here under their historical private
# names for existing importers (value_formatting, oneshot).
_build_type_map = build_type_map
_build_format_map = build_format_map
_build_explain_response = build_explain_response

# Response formats the execute endpoints can emit.
ExecuteFormat = Literal["json", "tsv", "arrow"]

# Historical Accept token used to negotiate the arrow surface.
ARROW_STREAM_MEDIA_TYPE = "application/vnd.apache.arrow.stream"

# The ``format=arrow`` response is a length-prefixed frame that separates the
# freshly-assembled metadata (JSON) from the cached row data (gzip'd Arrow IPC):
#
#     [u32 big-endian json_len][JSON envelope utf-8][gzip'd Arrow IPC stream]
#
# Only the row data is cached; the JSON envelope (sql, explain, timing,
# ``cached`` flag, …) is rebuilt per request, so a cache hit reports correct
# per-request metadata for every consumer. The Arrow sub-part carries its own
# gzip, so on a hit it is the verbatim cached data blob (data stays zero-copy);
# the whole frame is NOT additionally HTTP-gzip'd.
ORIONBELT_RESULT_MEDIA_TYPE = "application/vnd.orionbelt.result+arrow"


def negotiate_execute_format(format_param: ExecuteFormat, accept: str | None) -> ExecuteFormat:
    """Resolve the effective response format from ``?format=`` + the Accept header.

    An explicit ``?format=`` (``tsv``/``arrow``) always wins. Otherwise, a
    client asking for the Arrow media type via ``Accept`` gets the Arrow frame;
    everything else stays JSON.
    """
    if format_param in ("tsv", "arrow"):
        return format_param
    if accept and ARROW_STREAM_MEDIA_TYPE in accept.lower():
        return "arrow"
    return "json"


def _arrow_envelope_dict(
    *,
    columns_meta: list[ColumnMetadata],
    sql: str,
    dialect: str,
    explain: ExplainPlanResponse | None,
    warnings: list[Any],
    sql_valid: bool,
    execution_time_ms: float,
    timezone: str | None,
    resolved: ResolvedInfoResponse,
    physical_tables: list[str],
    row_count: int,
    cached: bool,
    cached_at: str | None,
) -> dict[str, Any]:
    """Assemble the JSON metadata envelope prepended to the Arrow data frame.

    Mirrors the JSON ``QueryExecuteResponse`` fields minus ``rows`` (which ride
    in the Arrow data sub-part). Built fresh every request from the compile
    result + per-request timing, so it is never cached.
    """
    return {
        "columns": [c.model_dump() for c in columns_meta],
        "sql": sql,
        "dialect": dialect,
        "explain": explain.model_dump() if explain else None,
        "warnings": [w.model_dump() for w in warnings],
        "sql_valid": sql_valid,
        "execution_time_ms": execution_time_ms,
        "timezone": timezone,
        "resolved": resolved.model_dump(),
        "physical_tables": list(physical_tables),
        "row_count": row_count,
        "cached": cached,
        "cached_at": cached_at,
    }


def _frame_result(meta: dict[str, Any], gzipped_data: bytes) -> Response:
    """Emit the length-prefixed ``[u32 len][json][gzip'd arrow]`` frame."""
    import json as _json

    meta_bytes = _json.dumps(meta, default=str).encode("utf-8")
    body = len(meta_bytes).to_bytes(4, "big") + meta_bytes + gzipped_data
    return Response(content=body, media_type=ORIONBELT_RESULT_MEDIA_TYPE)


def _physical_tables_for(model: Any, result: Any) -> list[str]:
    """Compute qualified physical tables touched by a compilation."""
    names = list(dict.fromkeys(result.resolved.fact_tables))
    if result.explain:
        for j in result.explain.joins:
            for nm in (j.from_object, j.to_object):
                if nm not in names:
                    names.append(nm)
        for leg in result.explain.cfl_legs:
            if leg.measure_source and leg.measure_source not in names:
                names.append(leg.measure_source)
    out: list[str] = []
    for nm in names:
        obj = model.data_objects.get(nm)
        if obj is None:
            out.append(nm)
            continue
        parts = [p for p in (obj.database, obj.schema_name, obj.code) if p]
        out.append(".".join(parts) if parts else nm)
    return out


def _render_response(
    *,
    response_format: ExecuteFormat,
    format_values: bool,
    locale: str,
    accept_encoding: str | None,
    columns_meta: list[ColumnMetadata],
    rows: list[list[Any]],
    fmt_map: dict[str, Any],
    type_map: dict[str, str],
    sql: str,
    dialect: str,
    explain: ExplainPlanResponse | None,
    warnings: list[Any],
    sql_valid: bool,
    resolved: ResolvedInfoResponse,
    physical_tables: list[str],
    row_count: int,
    execution_time_ms: float,
    timezone: str | None,
    cached: bool = False,
    cached_at: str | None = None,
) -> QueryExecuteResponse | Response:
    """Render already-resolved columns + RAW rows into the requested surface.

    Shared by the fresh-execute and cache-hit paths so a cached raw result is
    delivered identically to a freshly executed one. Value-formatting is a
    delivery-time leaf op applied *here* (never baked into the cache): ``tsv``
    always formats, ``json`` and ``arrow`` format only when ``format_values`` is
    set. Arrow without ``format_values`` (the UI round trip) ships raw typed,
    locale-neutral values verbatim; arrow *with* ``format_values`` bakes the
    locale-aware display strings into the Arrow data.
    """
    column_names = [c.name for c in columns_meta]

    if response_format == "arrow":
        from orionbelt.cache import result_codec

        # Default (UI round trip): raw typed, locale-neutral values, formatted
        # client-side. With format_values the locale-aware display strings are
        # encoded into the Arrow data, mirroring the JSON format_values variant.
        arrow_rows: list[list[Any]]
        if format_values:
            arrow_rows = [
                cast(
                    list[Any],
                    format_row(
                        row,
                        column_names=column_names,
                        fmt_map=fmt_map,
                        type_map=type_map,
                        locale=locale,
                    ),
                )
                for row in rows
            ]
        else:
            arrow_rows = rows
        gzipped_data = result_codec.encode_data(column_names, arrow_rows)
        meta = _arrow_envelope_dict(
            columns_meta=columns_meta,
            sql=sql,
            dialect=dialect,
            explain=explain,
            warnings=warnings,
            sql_valid=sql_valid,
            execution_time_ms=execution_time_ms,
            timezone=timezone,
            resolved=resolved,
            physical_tables=physical_tables,
            row_count=row_count,
            cached=cached,
            cached_at=cached_at,
        )
        return _frame_result(meta, gzipped_data)

    if response_format == "tsv":
        formatted = [
            format_row(
                row,
                column_names=column_names,
                fmt_map=fmt_map,
                type_map=type_map,
                locale=locale,
            )
            for row in rows
        ]
        return Response(
            content=to_tsv(column_names, formatted),
            media_type="text/tab-separated-values",
        )

    if format_values:
        out_rows: list[list[Any]] = [
            cast(
                list[Any],
                format_row(
                    row,
                    column_names=column_names,
                    fmt_map=fmt_map,
                    type_map=type_map,
                    locale=locale,
                ),
            )
            for row in rows
        ]
    else:
        out_rows = rows

    return QueryExecuteResponse(
        sql=sql,
        dialect=dialect,
        columns=columns_meta,
        rows=out_rows,
        row_count=row_count,
        execution_time_ms=execution_time_ms,
        timezone=timezone,
        resolved=resolved,
        warnings=warnings,
        sql_valid=sql_valid,
        explain=explain,
        physical_tables=list(physical_tables),
        cached=cached,
        cached_at=cached_at,
    )


def _columns_and_maps(
    model: Any, exec_columns: list[Any]
) -> tuple[list[ColumnMetadata], dict[str, Any], dict[str, str]]:
    """Build ``columns_meta`` + the fmt/type maps from executor columns + model.

    Sourced from the executor's column list (name + ``type_hint`` +
    ``default_format``), so a cache hit — whose columns are reconstructed from
    the Arrow schema via :func:`exec_columns_from_table` — yields identical
    metadata to a fresh execution.
    """
    model_type_map = _build_type_map(model)
    fmt_map = _build_format_map(model)
    # Auto-default for columns without an explicit model-side format — the
    # executor proposes a pattern based on the column's Arrow / driver type
    # (None for ints/strings/dates so they stay as bare ``str(val)``;
    # ``"#,##0.00"`` for floats and decimals). Raw-mode ``select.fields``
    # projections benefit most: physical columns no longer need a measure
    # to inherit a sensible locale-aware render.
    for c in exec_columns:
        if fmt_map.get(c.name) is None and getattr(c, "default_format", None):
            fmt_map[c.name] = c.default_format

    columns_meta = [
        ColumnMetadata(
            name=c.name,
            type=model_type_map.get(c.name, c.type_hint or "string"),
            format=fmt_map.get(c.name),
        )
        for c in exec_columns
    ]
    # Merge the executor's type_hint as a fallback for columns that aren't
    # exposed via the dimension/measure/metric layer — notably raw-mode
    # ``select.fields`` projections, which reference physical columns the
    # model-level type_map doesn't list. Without this merge, a numeric raw
    # column ("decimal(18, 2)" from the driver) wouldn't be classified as
    # numeric in format_row.
    type_map: dict[str, str] = {
        c.name: model_type_map.get(c.name, c.type_hint or "") for c in exec_columns
    }
    return columns_meta, fmt_map, type_map


def _build_execute_response(
    *,
    compile_result: Any,
    exec_result: Any,
    model: Any,
    response_format: ExecuteFormat,
    format_values: bool,
    locale: str,
    accept_encoding: str | None = None,
    cached: bool = False,
    cached_at: str | None = None,
) -> QueryExecuteResponse | Response:
    """Build the JSON QueryExecuteResponse, or a TSV / Arrow Response.

    ``format_values`` is forced True for TSV; numeric cells are rendered with
    each column's display ``format`` pattern using locale-aware separators. On a
    cache hit ``exec_result`` is reconstructed from the cached data table, with
    ``execution_time_ms`` set to the cache fetch time and ``cached=True``.
    """
    columns_meta, fmt_map, type_map = _columns_and_maps(model, exec_result.columns)

    return _render_response(
        response_format=response_format,
        format_values=format_values,
        locale=locale,
        accept_encoding=accept_encoding,
        columns_meta=columns_meta,
        rows=exec_result.rows,
        fmt_map=fmt_map,
        type_map=type_map,
        sql=format_sql(compile_result.sql, compile_result.dialect),
        dialect=compile_result.dialect,
        explain=_build_explain_response(compile_result),
        warnings=[semantic_error_to_warning(w) for w in compile_result.warnings],
        sql_valid=compile_result.sql_valid,
        resolved=ResolvedInfoResponse(
            fact_tables=compile_result.resolved.fact_tables,
            dimensions=compile_result.resolved.dimensions,
            measures=compile_result.resolved.measures,
        ),
        physical_tables=list(compile_result.physical_tables),
        row_count=exec_result.row_count,
        execution_time_ms=exec_result.execution_time_ms,
        timezone=exec_result.timezone,
        cached=cached,
        cached_at=cached_at,
    )


async def _run_with_cache(
    *,
    store: ModelStore,
    model: Any,
    compile_result: Any,
    session_id: str,
    model_id: str,
    dialect: str,
    cache: Cache,
    cache_config: CacheRuntimeConfig,
    response_format: ExecuteFormat,
    format_values: bool,
    locale: str | None,
    timezone_override: str | None,
    accept_encoding: str | None = None,
) -> QueryExecuteResponse | Response:
    """Cache-aware execute pipeline shared by session and shortcut endpoints.

    Looks up the cache before executing, stores on miss, and surfaces the
    ``cached`` / ``ttl_*`` metadata. The cache holds RAW, locale-neutral rows
    keyed on the query alone, so every surface — raw JSON, value-formatted JSON,
    TSV and Arrow — shares one entry and is rendered on delivery. Formatting is
    never baked into the cache.
    """
    model_default_tz: str | None = None
    override_db_tz = False
    if model.settings:
        model_default_tz = model.settings.default_timezone
        override_db_tz = model.settings.override_database_timezone
    tz = resolve_timezone(default_timezone=timezone_override or model_default_tz)

    # Both the canonical JSON shape and the Arrow stream cache the same typed,
    # locale-neutral rows, so they share entries. TSV and value-formatted JSON
    # skip caching (locale-keyed proliferation). Skip the whole cache machinery
    # (key + freshness TTL + get) when the backend is a no-op, so the default
    # deployment doesn't pay a per-query model scan for a cache that never hits.
    # Caching stores RAW, locale-neutral rows keyed on the query only;
    # presentation (format_values / tsv / arrow) is a delivery-time leaf op.
    # So cacheability is format-independent — every surface reads/writes the
    # same raw entry. Determinism + freshness TTL still gate the actual store
    # inside execute_query_with_cache.
    cacheable = getattr(cache, "backend_name", "noop") != "noop"

    # A raw ``format=arrow`` request (no value formatting) only needs the stored
    # blob to hand back verbatim, so tell the cache to skip the gzip + Arrow
    # decode on a hit (true zero-copy). Every other surface needs decoded rows.
    decode_payload = not (response_format == "arrow" and not format_values)

    try:
        cached = await execute_query_with_cache(
            store=store,
            model=model,
            compile_result=compile_result,
            model_id=model_id,
            dialect=dialect,
            cache=cache,
            cache_config=cache_config,
            tz=tz,
            override_db_tz=override_db_tz,
            cacheable=cacheable,
            decode_payload=decode_payload,
        )
    except ExecutionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    # Locale resolution order: request ?locale= -> model settings.defaultLocale
    # -> DEFAULT_LOCALE env. Drives value-formatting separators. Resolved before
    # the hit/miss split because cache hits now format on delivery too.
    model_default_locale = model.settings.default_locale if model.settings else None
    effective_locale = (
        locale if locale is not None else (model_default_locale or get_default_locale())
    )

    if cached.cached:
        # Only row data is cached; rebuild the response envelope fresh from the
        # compile result so per-request fields (timing, ``cached``) are correct
        # for every surface. ``execution_time_ms`` becomes the cache fetch time.
        assert cached.fetch_elapsed_ms is not None
        fetch_ms = cached.fetch_elapsed_ms
        if response_format == "arrow" and not format_values:
            # Raw-arrow hit: ship the stored DATA blob verbatim (data stays
            # zero-copy) with a fresh JSON envelope prepended. Decoding only
            # recovers the Arrow schema + row count — no python-row
            # materialization and no re-encode of the data bytes.
            assert cached.payload_blob is not None
            from orionbelt.cache import result_codec

            data_table = result_codec.decode_data(cached.payload_blob)
            columns_meta, _, _ = _columns_and_maps(model, exec_columns_from_table(data_table))
            meta = _arrow_envelope_dict(
                columns_meta=columns_meta,
                sql=format_sql(compile_result.sql, compile_result.dialect),
                dialect=compile_result.dialect,
                explain=_build_explain_response(compile_result),
                warnings=[semantic_error_to_warning(w) for w in compile_result.warnings],
                sql_valid=compile_result.sql_valid,
                execution_time_ms=fetch_ms,
                timezone=str(tz) if tz is not None else None,
                resolved=ResolvedInfoResponse(
                    fact_tables=compile_result.resolved.fact_tables,
                    dimensions=compile_result.resolved.dimensions,
                    measures=compile_result.resolved.measures,
                ),
                physical_tables=list(compile_result.physical_tables),
                row_count=data_table.num_rows,
                cached=True,
                cached_at=cached.cached_at_iso,
            )
            return _frame_result(meta, cached.payload_blob)

        # JSON / TSV / value-formatted-arrow hit: reconstruct the executor result
        # from the cached data table and render exactly like a fresh execution.
        assert cached.data_table is not None
        hit_exec_result = execution_result_from_data(
            cached.data_table, execution_time_ms=fetch_ms, tz=tz
        )
        response = _build_execute_response(
            compile_result=compile_result,
            exec_result=hit_exec_result,
            model=model,
            response_format=response_format,
            format_values=format_values,
            locale=effective_locale,
            accept_encoding=accept_encoding,
            cached=True,
            cached_at=cached.cached_at_iso,
        )
        if (
            isinstance(response, QueryExecuteResponse)
            and cached.ttl_outcome is not None
            and cached.ttl_outcome.ttl is not None
        ):
            _apply_ttl_metadata(response, cached.ttl_outcome)
        return response

    response = _build_execute_response(
        compile_result=compile_result,
        exec_result=cached.exec_result,
        model=model,
        response_format=response_format,
        format_values=format_values,
        locale=effective_locale,
        accept_encoding=accept_encoding,
    )
    ttl_outcome = cached.ttl_outcome
    if (
        cacheable
        and ttl_outcome is not None
        and ttl_outcome.ttl is not None
        and isinstance(response, QueryExecuteResponse)
    ):
        _apply_ttl_metadata(response, ttl_outcome)
    elif cacheable and ttl_outcome is not None and isinstance(response, QueryExecuteResponse):
        _apply_no_cache_metadata(response, ttl_outcome)
    return response


def _apply_ttl_metadata(response: QueryExecuteResponse, ttl_outcome: Any) -> None:
    """Surface TTL fields on a fresh (non-cached) response."""
    ttl = ttl_outcome.ttl
    if ttl is None:
        return
    response.ttl_seconds = ttl.seconds
    response.ttl_source = ttl.source
    response.ttl_limiting_table = ttl.limiting_table


def _apply_no_cache_metadata(response: QueryExecuteResponse, ttl_outcome: Any) -> None:
    """Document why a response was not cached."""
    reason = ttl_outcome.no_cache_reason
    if reason is None:
        return
    response.ttl_source = (
        "no_cache" if reason.value == "unknown_freshness" else f"no_cache:{reason.value}"
    )
    response.ttl_limiting_table = ttl_outcome.no_cache_table
