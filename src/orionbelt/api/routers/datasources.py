"""Data source endpoints: GET /datasources, and what each one holds."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query

from orionbelt.api.deps import is_query_execute_enabled
from orionbelt.api.schemas import (
    DataSourceInfo,
    DataSourceListResponse,
    DataSourceSchemasResponse,
    InspectedColumn,
    InspectedForeignKey,
    InspectedTable,
    SchemaInspectionResponse,
)
from orionbelt.service.datasource_selection import (
    configured_datasource_list,
    describe_datasources,
)
from orionbelt.service.db_executor import ExecutionError, ExecutionUnavailableError
from orionbelt.service.schema_inspection import (
    InspectionNotSupportedError,
    TableInfo,
    inspect_schema,
    list_schemas,
)

if TYPE_CHECKING:
    from ob_flight.datasources import DataSource

# Prefix on the constructor keeps the root route ("") at /v1/datasources with no
# trailing slash (FastAPI 0.137+ rejects empty paths via include_router prefix).
router = APIRouter(prefix="/datasources")


@router.get("", response_model=DataSourceListResponse)
async def list_datasources() -> DataSourceListResponse:
    """List the configured data sources a model can run on.

    Named sources (``OBSL_DATABASES``) in their declared order, then each
    dialect configured without a name. Shows the name, dialect, description
    and the database each reads, never connection secrets.
    """
    try:
        described = describe_datasources()
    except ExecutionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return DataSourceListResponse(datasources=[DataSourceInfo(**d) for d in described])


@router.get("/{name}/schemas", response_model=DataSourceSchemasResponse)
async def get_datasource_schemas(name: str) -> DataSourceSchemasResponse:
    """List the schemas of a data source, without the engine's own.

    A schema is a database on MySQL and ClickHouse and a dataset on BigQuery.
    Reads the database's catalog, so it requires ``QUERY_EXECUTE=true``.
    """
    source = _source(name)
    try:
        schemas = await asyncio.to_thread(list_schemas, source)
    except Exception as exc:
        raise _http_error(exc) from None
    return DataSourceSchemasResponse(
        datasource=source.name, dialect=source.dialect, schemas=schemas
    )


@router.get("/{name}/schemas/{schema}/tables", response_model=SchemaInspectionResponse)
async def get_schema_tables(
    name: str,
    schema: str,
    table: Annotated[
        list[str] | None, Query(description="Only these tables; repeat for several.")
    ] = None,
    views: Annotated[bool, Query(description="List views and materialized views.")] = True,
    view_definitions: Annotated[bool, Query(description="Include each view's SQL.")] = False,
) -> SchemaInspectionResponse:
    """Describe the tables of a schema: columns, types, comments and keys.

    Primary and foreign keys come from the catalog's declared constraints,
    where the engine has them (not Dremio; ClickHouse has primary keys only).
    Keys or view bodies the catalog will not show this user are left out with
    a warning rather than failing the request. Requires ``QUERY_EXECUTE=true``.
    """
    source = _source(name)
    try:
        info = await asyncio.to_thread(
            inspect_schema,
            source,
            schema,
            tables=table,
            include_views=views,
            view_definitions=view_definitions,
        )
    except Exception as exc:
        raise _http_error(exc) from None
    if info is None:
        raise HTTPException(
            status_code=404, detail=f"Data source '{source.name}' has no schema '{schema}'"
        )
    return SchemaInspectionResponse(
        datasource=source.name,
        dialect=source.dialect,
        schema_name=info.schema,
        tables=[_table(t) for t in info.tables],
        warnings=info.warnings,
    )


def _source(name: str) -> DataSource:
    """The configured data source *name*, or the HTTP error saying why not."""
    if not is_query_execute_enabled():
        raise HTTPException(
            status_code=503,
            detail="Schema inspection connects to the database. Set QUERY_EXECUTE=true "
            "and configure the data source's credentials.",
        )
    try:
        from ob_flight.datasources import DataSourceNotFoundError, find_datasource
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="ob-flight-extension is not installed. Install with: uv sync --extra flight",
        ) from None
    try:
        return find_datasource(name, configured_datasource_list())
    except ExecutionUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except DataSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _http_error(exc: Exception) -> Exception:
    """The HTTP error for a failed catalog read; anything unexpected as it is."""
    if isinstance(exc, InspectionNotSupportedError):
        return HTTPException(status_code=501, detail=str(exc))
    if isinstance(exc, ExecutionUnavailableError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ExecutionError):
        return HTTPException(status_code=502, detail=str(exc))
    return exc


def _table(table: TableInfo) -> InspectedTable:
    return InspectedTable(
        name=table.name,
        kind=table.kind,
        comment=table.comment,
        columns=[
            InspectedColumn(
                name=c.name,
                data_type=c.data_type,
                ordinal=c.ordinal,
                nullable=c.nullable,
                comment=c.comment,
                primary_key=c.primary_key,
            )
            for c in table.columns
        ],
        primary_key=table.primary_key,
        foreign_keys=[
            InspectedForeignKey(
                name=fk.name,
                columns=list(fk.columns),
                referred_schema=fk.referred_schema,
                referred_table=fk.referred_table,
                referred_columns=list(fk.referred_columns),
            )
            for fk in table.foreign_keys
        ],
        view_definition=table.view_definition,
    )
