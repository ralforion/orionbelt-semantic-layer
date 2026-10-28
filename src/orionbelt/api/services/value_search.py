"""REST adapter for the dimension-value search (``service/value_search.py``).

Each query the search builds is compiled by the model store and executed
through the result cache, exactly as ``POST /query/execute`` runs a query:
same compile errors, same static filters, same cache entries.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException

from orionbelt.api.deps import (
    get_cache,
    get_cache_config,
    get_db_vendor,
    get_value_search_config,
    is_query_execute_enabled,
)
from orionbelt.api.query_cache import execute_query_with_cache, execution_result_from_data
from orionbelt.api.schemas import ValueMatchItem, ValueSearchRequest, ValueSearchResponse
from orionbelt.api.services.query_compilation import _resolve_dialect, compile_query_or_raise
from orionbelt.models.query import QueryObject
from orionbelt.service.datasource_selection import resolve_query_source
from orionbelt.service.db_executor import (
    ExecutionError,
    ExecutionUnavailableError,
    resolve_timezone,
)
from orionbelt.service.model_store import ModelStore
from orionbelt.service.result_schema import declared_arrow_types, reconcile_to_declared
from orionbelt.service.value_search import QueryRun, ValueSearchError, search_values


def _first_column(cached: Any, tz: Any, declared: dict[str, Any]) -> list[Any]:
    """The first column of a cached execution, fresh or a cache hit.

    A hit is rebuilt the way ``/query/execute`` rebuilds one: reconciled to the
    model's declared types, then read in the model's time zone, so both paths
    return the same values.
    """
    if cached.exec_result is not None:
        return [row[0] for row in cached.exec_result.rows]
    if cached.data_table is not None:
        table, _ = reconcile_to_declared(cached.data_table, declared)
        hit = execution_result_from_data(
            table, execution_time_ms=0.0, tz=tz, columns=cached.hit_columns
        )
        return [row[0] for row in hit.rows]
    return []


async def run_value_search(
    store: ModelStore, model_id: str, body: ValueSearchRequest
) -> ValueSearchResponse:
    """Run a value search against *model_id* in *store*."""
    config = get_value_search_config()
    if not config.enabled:
        raise HTTPException(
            status_code=503,
            detail="Value search is disabled. Set VALUE_SEARCH_ENABLED=true.",
        )
    if not is_query_execute_enabled():
        raise HTTPException(
            status_code=503,
            detail="Query execution is not available. Set QUERY_EXECUTE=true "
            "and configure DB_VENDOR + credentials.",
        )
    try:
        model = store.get_model(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    dialect = _resolve_dialect(request_dialect=body.dialect, model=model, fallback=get_db_vendor())
    cache, cache_config = get_cache(), get_cache_config()
    settings = model.settings
    tz = resolve_timezone(default_timezone=settings.default_timezone if settings else None)

    async def run(query: QueryObject) -> QueryRun:
        compiled = compile_query_or_raise(
            store=store, model_id=model_id, query=query, dialect=dialect
        )
        declared = declared_arrow_types(model, query)
        try:
            cached = await execute_query_with_cache(
                store=store,
                model=model,
                compile_result=compiled,
                model_id=model_id,
                # The source's dialect, where the model reads several.
                dialect=compiled.dialect,
                source=resolve_query_source(model, compiled),
                cache=cache,
                cache_config=cache_config,
                tz=tz,
                override_db_tz=bool(settings and settings.override_database_timezone),
                cacheable=getattr(cache, "backend_name", "noop") != "noop",
                declared_types=declared,
                query=query,
            )
        except ExecutionUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except ExecutionError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return QueryRun(
            values=_first_column(cached, tz, declared),
            sql=compiled.sql,
            warnings=[w.message for w in compiled.warnings],
        )

    try:
        result = await asyncio.wait_for(
            search_values(
                model,
                body.dimension,
                run=run,
                config=config,
                search=body.search,
                where=list(body.where),
                limit=body.limit,
            ),
            timeout=config.timeout_ms / 1000,
        )
    except ValueSearchError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"code": exc.code, "message": exc.message}
        ) from None
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Value search took longer than {config.timeout_ms} ms (VALUE_SEARCH_TIMEOUT_MS)"
            ),
        ) from None
    return ValueSearchResponse(
        dimension=result.dimension,
        values=[ValueMatchItem(value=m.value, match=m.match, score=m.score) for m in result.values],
        truncated=result.truncated,
        minimum=result.minimum,
        maximum=result.maximum,
        sql=result.sql,
        warnings=result.warnings,
    )
