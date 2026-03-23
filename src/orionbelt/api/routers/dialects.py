"""Dialect listing endpoint: GET /dialects."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter

from orionbelt.api.schemas import DialectInfo, DialectListResponse
from orionbelt.dialect.registry import DialectRegistry

router = APIRouter()


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
