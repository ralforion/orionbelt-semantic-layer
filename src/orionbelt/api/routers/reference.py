"""Reference endpoints: OBML / OBSQL grammars + JSON schemas.

``GET /v1/reference`` lists all available references. ``GET /v1/reference/{name}``
returns the named reference (text or JSON Schema as appropriate). Used by
LLM / MCP integrations to discover the model and query languages without
scraping Swagger UI.
"""

from __future__ import annotations

import json
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orionbelt.obml_reference import OBML_REFERENCE
from orionbelt.obsql_reference import OBSQL_REFERENCE

# Prefix on the constructor keeps the root index route ("") at /v1/reference with
# no trailing slash (FastAPI 0.137+ rejects empty paths via include_router prefix).
router = APIRouter(prefix="/reference")


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


_SCHEMA_FILES: dict[str, str] = {
    "obml": "obml-schema.json",
    "query": "query-schema.json",
}


def _read_schema_text(filename: str) -> str | None:
    """Return the contents of a JSON Schema file, or ``None`` if not found.

    The schema files are shipped inside the wheel as package data under
    ``orionbelt/schema/`` (see ``force-include`` in ``pyproject.toml``), so
    they resolve via :mod:`importlib.resources` for PyPI and Docker installs.
    Editable / source checkouts have no packaged copy, so fall back to the
    repo-root ``schema/`` directory.

    Module path: src/orionbelt/api/routers/reference.py
    parents[0..4]: routers / api / orionbelt / src / repo_root
    """
    try:
        resource = resource_files("orionbelt") / "schema" / filename
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        pass

    src_path = Path(__file__).resolve().parents[4] / "schema" / filename
    if src_path.is_file():
        return src_path.read_text(encoding="utf-8")
    return None


def _load_schema(name: str) -> dict[str, Any]:
    """Read a JSON Schema file by reference name.

    The files are tiny (KB-sized); reading per request keeps reload
    semantics clean for tests. Raises HTTPException 404 for unknown names
    and 500 when the file is absent from the deployment.
    """
    filename = _SCHEMA_FILES.get(name)
    if filename is None:
        raise HTTPException(
            status_code=404,
            detail=(f"Unknown schema '{name}'. Available: {', '.join(sorted(_SCHEMA_FILES))}"),
        )
    text = _read_schema_text(filename)
    if text is None:
        raise HTTPException(
            status_code=500,
            detail=f"Schema file '{filename}' is missing from this deployment.",
        )
    loaded: dict[str, Any] = json.loads(text)
    return loaded


@router.get("/schemas/{name}")
async def get_schema(name: str) -> JSONResponse:
    """Return a JSON Schema file by name (``obml`` or ``query``).

    The response is the raw JSON Schema document with ``Content-Type:
    application/schema+json`` so JSON Schema validators recognise it.
    """
    schema = _load_schema(name)
    return JSONResponse(content=schema, media_type="application/schema+json")
