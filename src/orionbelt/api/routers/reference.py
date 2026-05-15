"""Reference endpoints: OBML / OBSQL grammars + JSON schemas.

``GET /v1/reference`` lists all available references. ``GET /v1/reference/{name}``
returns the named reference (text or JSON Schema as appropriate). Used by
LLM / MCP integrations to discover the model and query languages without
scraping Swagger UI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orionbelt.obml_reference import OBML_REFERENCE
from orionbelt.obsql_reference import OBSQL_REFERENCE

router = APIRouter()


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class ReferenceIndexEntry(BaseModel):
    """One row in the reference index."""

    name: str
    kind: str = Field(description="'markdown' or 'json-schema'")
    description: str
    path: str = Field(description="URL path under /v1/reference")


class ReferenceIndexResponse(BaseModel):
    """Response for GET /v1/reference."""

    references: list[ReferenceIndexEntry]


_INDEX: list[ReferenceIndexEntry] = [
    ReferenceIndexEntry(
        name="obml",
        kind="markdown",
        description="OBML (OrionBelt Modeling Language) reference — YAML model format",
        path="/v1/reference/obml",
    ),
    ReferenceIndexEntry(
        name="obsql",
        kind="markdown",
        description=(
            "OBSQL (OrionBelt Semantic Query Language) reference — natural SQL "
            "surface against the model's virtual table"
        ),
        path="/v1/reference/obsql",
    ),
    ReferenceIndexEntry(
        name="obml-schema",
        kind="json-schema",
        description="JSON Schema for OBML model documents",
        path="/v1/reference/schemas/obml",
    ),
    ReferenceIndexEntry(
        name="query-schema",
        kind="json-schema",
        description="JSON Schema for the QueryObject (input to /query/execute)",
        path="/v1/reference/schemas/query",
    ),
]


@router.get("", response_model=ReferenceIndexResponse)
async def list_references() -> ReferenceIndexResponse:
    """List all reference documents available to clients."""
    return ReferenceIndexResponse(references=_INDEX)


# ---------------------------------------------------------------------------
# Markdown references
# ---------------------------------------------------------------------------


class ReferenceResponse(BaseModel):
    """Response for GET /reference/obml and /reference/obsql."""

    reference: str = Field(description="Reference text (markdown)")


@router.get("/obml", response_model=ReferenceResponse)
async def get_obml_reference() -> ReferenceResponse:
    """Return the full OBML format reference."""
    return ReferenceResponse(reference=OBML_REFERENCE)


@router.get("/obsql", response_model=ReferenceResponse)
async def get_obsql_reference() -> ReferenceResponse:
    """Return the full OBSQL grammar reference."""
    return ReferenceResponse(reference=OBSQL_REFERENCE)


# ---------------------------------------------------------------------------
# JSON Schemas
# ---------------------------------------------------------------------------


# Path to the ``schema/`` directory at the repo root. The schema files
# ship in the source distribution; locate them relative to this module
# so the lookup works whether OBSL runs from the repo, an installed
# wheel, or inside a Docker image.
#
# Module path: src/orionbelt/api/routers/reference.py
# parents[0..4]: routers / api / orionbelt / src / repo_root
_SCHEMA_DIR = (Path(__file__).resolve().parents[4] / "schema").resolve()


_SCHEMA_FILES: dict[str, str] = {
    "obml": "obml-schema.json",
    "query": "query-schema.json",
}


def _load_schema(name: str) -> dict[str, Any]:
    """Read a JSON Schema file from the ``schema/`` directory.

    Cached at module load would be ideal for production, but the files
    are tiny (KB-sized) and loaded per request keeps reload semantics
    clean for tests. Raises HTTPException 404 for unknown names.
    """
    filename = _SCHEMA_FILES.get(name)
    if filename is None:
        raise HTTPException(
            status_code=404,
            detail=(f"Unknown schema '{name}'. Available: {', '.join(sorted(_SCHEMA_FILES))}"),
        )
    path = _SCHEMA_DIR / filename
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Schema file '{filename}' is missing from this deployment.",
        )
    with path.open(encoding="utf-8") as f:
        loaded: dict[str, Any] = json.load(f)
        return loaded


@router.get("/schemas/{name}")
async def get_schema(name: str) -> JSONResponse:
    """Return a JSON Schema file by name (``obml`` or ``query``).

    The response is the raw JSON Schema document with ``Content-Type:
    application/schema+json`` so JSON Schema validators recognise it.
    """
    schema = _load_schema(name)
    return JSONResponse(content=schema, media_type="application/schema+json")
