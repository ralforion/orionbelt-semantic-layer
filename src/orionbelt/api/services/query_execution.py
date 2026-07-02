"""Query-execution helpers and core logic extracted from the session router."""

from __future__ import annotations

import gzip
from typing import Any, Literal, cast

from fastapi import HTTPException, Response

from orionbelt.api.deps import (
    CacheRuntimeConfig,
    get_default_locale,
)
from orionbelt.api.query_cache import (
    build_explain_response,
    build_format_map,
    build_result_columns,
    build_type_map,
    execute_query_with_cache,
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

# Response formats the execute endpoints can emit. ``arrow`` is an uncompressed
# Arrow IPC stream (see design/PLAN_arrow_cache.md), gzip'd at the HTTP layer.
ExecuteFormat = Literal["json", "tsv", "arrow"]

ARROW_STREAM_MEDIA_TYPE = "application/vnd.apache.arrow.stream"


def negotiate_execute_format(format_param: ExecuteFormat, accept: str | None) -> ExecuteFormat:
    """Resolve the effective response format from ``?format=`` + the Accept header.

    An explicit ``?format=`` (``tsv``/``arrow``) always wins. Otherwise, a
    client asking for the Arrow media type via ``Accept`` gets the Arrow stream;
    everything else stays JSON.
    """
    if format_param in ("tsv", "arrow"):
        return format_param
    if accept and ARROW_STREAM_MEDIA_TYPE in accept.lower():
        return "arrow"
    return "json"


def _negotiate_content_encoding(accept_encoding: str | None) -> str:
    """Pick a blob codec for the Arrow body from the client's Accept-Encoding.

    gzip is the universal default (every HTTP client and browser auto-decodes
    it). A client that advertises no codec we store gets identity (the raw,
    uncompressed IPC stream).
    """
    tokens = {t.strip().split(";")[0].lower() for t in (accept_encoding or "").split(",")}
    if "gzip" in tokens or "*" in tokens:
        return "gzip"
    return "identity"


def _build_arrow_response(
    *,
    column_names: list[str],
    rows: list[list[Any]],
    accept_encoding: str | None,
) -> Response:
    """Serialize a result as an Arrow IPC stream, gzip'd via ``Content-Encoding``.

    The Arrow buffers stay uncompressed (universally readable by pyarrow,
    arrow-js, DuckDB); compression is applied at the HTTP layer and advertised
    via ``Content-Encoding`` so the client's HTTP stack transparently decodes it
    before handing uncompressed IPC to its Arrow reader (PLAN_arrow_cache.md §2).
    """
    from orionbelt.cache.result_codec import build_result_table, to_ipc_stream

    raw = to_ipc_stream(build_result_table(column_names, rows))
    headers: dict[str, str] = {}
    if _negotiate_content_encoding(accept_encoding) == "gzip":
        body = gzip.compress(raw, 6)
        headers["Content-Encoding"] = "gzip"
    else:
        body = raw
    return Response(content=body, media_type=ARROW_STREAM_MEDIA_TYPE, headers=headers)


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


def _build_execute_response(
    *,
    compile_result: Any,
    exec_result: Any,
    model: Any,
    response_format: ExecuteFormat,
    format_values: bool,
    locale: str,
    accept_encoding: str | None = None,
) -> QueryExecuteResponse | Response:
    """Build the JSON QueryExecuteResponse, or a TSV / Arrow Response.

    ``format_values`` is forced True for TSV; numeric cells are rendered with
    each column's display ``format`` pattern using locale-aware separators.
    ``arrow`` emits an uncompressed Arrow IPC stream (gzip'd per
    ``accept_encoding``) carrying the typed, locale-neutral values verbatim.
    """
    model_type_map = _build_type_map(model)
    fmt_map = _build_format_map(model)
    # Auto-default for columns without an explicit model-side format — the
    # executor proposes a pattern based on the column's Arrow / driver type
    # (None for ints/strings/dates so they stay as bare ``str(val)``;
    # ``"#,##0.00"`` for floats and decimals). Raw-mode ``select.fields``
    # projections benefit most: physical columns no longer need a measure
    # to inherit a sensible locale-aware render.
    for c in exec_result.columns:
        if fmt_map.get(c.name) is None and getattr(c, "default_format", None):
            fmt_map[c.name] = c.default_format
    column_names = [c.name for c in exec_result.columns]

    # Arrow passes the typed, locale-neutral values through untouched (no
    # locale/format rendering — that's a JSON/TSV presentation concern), so it
    # short-circuits before the display-format machinery below.
    if response_format == "arrow":
        return _build_arrow_response(
            column_names=column_names,
            rows=exec_result.rows,
            accept_encoding=accept_encoding,
        )

    # Canonical column shape (also what the result cache persists) — built via
    # the shared helper so REST, the cache payload, and pgwire all agree. Reuse
    # the maps already built above instead of rebuilding them.
    columns_meta = build_result_columns(
        model, exec_result, type_map=model_type_map, fmt_map=fmt_map
    )
    # Merge the executor's type_hint as a fallback for columns that aren't
    # exposed via the dimension/measure/metric layer — notably raw-mode
    # ``select.fields`` projections, which reference physical columns the
    # model-level type_map doesn't list. Without this merge, a numeric raw
    # column ("decimal(18, 2)" from the driver) wouldn't be classified as
    # numeric in format_row.
    type_map: dict[str, str] = {
        c.name: model_type_map.get(c.name, c.type_hint or "") for c in exec_result.columns
    }

    if response_format == "tsv":
        formatted = [
            format_row(
                row,
                column_names=column_names,
                fmt_map=fmt_map,
                type_map=type_map,
                locale=locale,
            )
            for row in exec_result.rows
        ]
        body = to_tsv(column_names, formatted)
        return Response(content=body, media_type="text/tab-separated-values")

    if format_values:
        rows: list[list[Any]] = [
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
            for row in exec_result.rows
        ]
    else:
        rows = exec_result.rows

    return QueryExecuteResponse(
        sql=format_sql(compile_result.sql, compile_result.dialect),
        dialect=compile_result.dialect,
        columns=columns_meta,
        rows=rows,
        row_count=exec_result.row_count,
        execution_time_ms=exec_result.execution_time_ms,
        timezone=exec_result.timezone,
        resolved=ResolvedInfoResponse(
            fact_tables=compile_result.resolved.fact_tables,
            dimensions=compile_result.resolved.dimensions,
            measures=compile_result.resolved.measures,
        ),
        warnings=[semantic_error_to_warning(w) for w in compile_result.warnings],
        sql_valid=compile_result.sql_valid,
        explain=_build_explain_response(compile_result),
        physical_tables=list(compile_result.physical_tables),
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
    ``cached`` / ``ttl_*`` metadata. The canonical JSON shape and the Arrow
    stream are cached and *share* entries (the key is query-only, both carry
    typed values); TSV + value-formatted JSON skip caching to avoid locale-keyed
    proliferation.
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
    cacheable = getattr(cache, "backend_name", "noop") != "noop" and (
        response_format == "arrow" or (response_format == "json" and not format_values)
    )

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
        )
    except ExecutionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except ExecutionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    if cached.cached:
        # A cache hit always carries a key and a fetch time.
        assert cached.cache_key is not None
        assert cached.fetch_elapsed_ms is not None
        if response_format == "arrow":
            # Serve the cached typed rows as an Arrow stream — the envelope was
            # written by the JSON path, but the rows are format-agnostic.
            assert cached.envelope is not None
            return _build_arrow_response(
                column_names=[str(c.get("name", "")) for c in cached.envelope.columns],
                rows=cached.envelope.rows,
                accept_encoding=accept_encoding,
            )
        return _build_cached_response(
            envelope=cached.envelope,
            cache_key=cached.cache_key,
            ttl_outcome=cached.ttl_outcome,
            fetch_elapsed_ms=cached.fetch_elapsed_ms,
        )

    # Locale resolution order: request ?locale= -> model settings.defaultLocale
    # -> DEFAULT_LOCALE env. Drives result value-formatting separators.
    model_default_locale = model.settings.default_locale if model.settings else None
    effective_locale = (
        locale if locale is not None else (model_default_locale or get_default_locale())
    )
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


def _build_cached_response(
    *,
    envelope: Any,
    cache_key: str,
    ttl_outcome: Any,
    fetch_elapsed_ms: float,
) -> QueryExecuteResponse:
    """Reconstruct a :class:`QueryExecuteResponse` from a cached Parquet entry.

    ``fetch_elapsed_ms`` is the wall-clock time spent reading + decoding the
    cache entry. It replaces the original DB execution time on the wire so
    callers see a realistic "this came from cache" duration; the original is
    preserved on disk in the Parquet sidecar for forensic inspection.
    """
    from orionbelt.api.schemas import StructuredWarning

    columns = [
        ColumnMetadata(
            name=c.get("name", ""),
            type=c.get("type", "string"),
            format=c.get("format"),
        )
        for c in envelope.columns
    ]
    explain_resp: ExplainPlanResponse | None = None
    if envelope.explain:
        try:
            explain_resp = ExplainPlanResponse(**envelope.explain)
        except Exception:
            explain_resp = None
    warnings_resp: list[StructuredWarning] = []
    for w in envelope.warnings or []:
        try:
            warnings_resp.append(StructuredWarning(**w))
        except Exception:
            continue
    cached_at_iso = envelope.cached_at_iso if hasattr(envelope, "cached_at_iso") else None
    response = QueryExecuteResponse(
        sql=envelope.sql,
        dialect=envelope.dialect,
        columns=columns,
        rows=envelope.rows,
        row_count=envelope.row_count,
        execution_time_ms=fetch_elapsed_ms,
        timezone=envelope.timezone,
        resolved=ResolvedInfoResponse(**(envelope.resolved or {})),
        warnings=warnings_resp,
        sql_valid=envelope.sql_valid,
        explain=explain_resp,
        physical_tables=list(envelope.physical_tables),
        cached=True,
        cached_at=cached_at_iso,
    )
    if ttl_outcome.ttl is not None:
        response.ttl_seconds = ttl_outcome.ttl.seconds
        response.ttl_source = ttl_outcome.ttl.source
        response.ttl_limiting_table = ttl_outcome.ttl.limiting_table
    return response
