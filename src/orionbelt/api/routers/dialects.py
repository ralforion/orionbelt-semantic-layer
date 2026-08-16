"""Dialect listing endpoint: GET /dialects."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from orionbelt.api.schemas import DialectInfo, DialectListResponse
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.functions import FUNCTION_CATALOG
from orionbelt.models.semantic import AggregationType

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
        # A dialect declares what it *cannot* do, so that an aggregation or a
        # catalog entry added later needs no edit in the seven dialects that
        # handle it. Clients are asking the opposite question, so the response
        # publishes the complement rather than making every caller fetch both
        # vocabularies and subtract.
        unsupported_aggs = {a.lower() for a in caps.pop("unsupported_aggregations", [])}
        unsupported_funcs = {f.lower() for f in caps.pop("unsupported_functions", [])}
        dialects.append(
            DialectInfo(
                name=name,
                capabilities=caps,
                supported_aggregations=sorted(
                    a.value for a in AggregationType if a.value.lower() not in unsupported_aggs
                ),
                supported_functions=sorted(
                    f for f in FUNCTION_CATALOG if f.lower() not in unsupported_funcs
                ),
            )
        )
    return DialectListResponse(dialects=dialects)
