"""Dialect listing endpoint: GET /dialects."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from orionbelt.api.schemas import DialectInfo, DialectListResponse
from orionbelt.dialect.registry import DialectRegistry

# Prefix on the constructor keeps the root route ("") at /v1/dialects with no
# trailing slash (FastAPI 0.137+ rejects empty paths via include_router prefix).
router = APIRouter(prefix="/dialects")


@router.get("", response_model=DialectListResponse)
async def list_dialects() -> DialectListResponse:
    """List all available SQL dialects and their capabilities."""
    dialects = []
    for name in DialectRegistry.available():
        dialect = DialectRegistry.get(name)
        caps = asdict(dialect.capabilities)
        unsupported_aggs = caps.pop("unsupported_aggregations", [])
        dialects.append(
            DialectInfo(
                name=name,
                capabilities=caps,
                unsupported_aggregations=unsupported_aggs,
            )
        )
    return DialectListResponse(dialects=dialects)
