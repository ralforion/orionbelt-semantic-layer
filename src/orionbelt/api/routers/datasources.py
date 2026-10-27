"""Data source listing endpoint: GET /datasources."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from orionbelt.api.schemas import DataSourceInfo, DataSourceListResponse
from orionbelt.service.datasource_selection import describe_datasources
from orionbelt.service.db_executor import ExecutionUnavailableError

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
