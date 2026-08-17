"""Reference endpoints: OBML / OBSQL grammars + JSON schemas.

``GET /v1/reference`` lists all available references. ``GET /v1/reference/{name}``
returns the named reference (text or JSON Schema as appropriate). Used by
LLM / MCP integrations to discover the model and query languages without
scraping Swagger UI.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orionbelt.models.functions import FUNCTION_CATALOG, catalog_names, markdown_prose
from orionbelt.obml_reference import OBML_REFERENCE
from orionbelt.obsql_reference import OBSQL_REFERENCE
from orionbelt.parser.schema_validation import read_schema_text

# Prefix on the constructor keeps the root index route ("") at /v1/reference with
# no trailing slash (FastAPI 0.137+ rejects empty paths via include_router prefix).
router = APIRouter(prefix="/reference")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class ReferenceIndexEntry(BaseModel):
    """One row in the reference index."""

    name: str
    kind: str = Field(description="'markdown', 'json-schema', or 'json'")
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
        name="functions",
        kind="json",
        description=(
            "Portable scalar-function catalog for OBML expressions — canonical "
            "names, arity, and the semantics OBSL pins across dialects"
        ),
        path="/v1/reference/functions",
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
# Portable function catalog
# ---------------------------------------------------------------------------


class FunctionExampleEntry(BaseModel):
    """A canonical call and the value the catalog guarantees it returns."""

    call: str
    expect: str | int | float | bool | None = Field(
        default=None,
        description="The documented result. ``null`` is a documented value, not a gap.",
    )


class FunctionReferenceEntry(BaseModel):
    """One catalog entry, as clients see it."""

    name: str
    signature: str
    group: str
    min_args: int
    max_args: int | None = Field(
        default=None, description="``null`` for a variadic function such as concat"
    )
    result_type: str
    summary: str
    semantics: str | None = Field(
        default=None,
        description=(
            "The behaviour OBSL pins where engines disagree on the answer rather "
            "than the spelling. Read this before assuming your warehouse's own rule."
        ),
    )
    examples: list[FunctionExampleEntry] = Field(default_factory=list)


class FunctionReferenceResponse(BaseModel):
    """Response for GET /reference/functions."""

    functions: list[FunctionReferenceEntry]
    escape_hatch: str = Field(
        description="What happens to a function the catalog does not carry.",
    )


_ESCAPE_HATCH = (
    "A function outside the catalog is emitted verbatim and not rewritten, so "
    "vendor SQL keeps working — at the cost of pinning the model to the engines "
    "that spell it that way. Its arity and meaning are still not checked, since "
    "they belong to the engine, but the call itself is reported: a "
    "NON_PORTABLE_FUNCTION warning by default, and an error when the model sets "
    "settings.expressionMode to 'portable', so the dependency cannot be acquired "
    "by accident. A catalog function called with the wrong number of arguments is "
    "rejected at model validation with WRONG_FUNCTION_ARITY."
)


@router.get("/functions", response_model=FunctionReferenceResponse)
async def get_function_reference() -> FunctionReferenceResponse:
    """Return the portable scalar-function catalog for OBML expressions.

    Structured rather than markdown: an agent composing an expression needs to
    know which names are portable and what they mean, and a BI client
    generating OBML needs the same list without scraping prose.
    """
    return FunctionReferenceResponse(
        functions=[
            FunctionReferenceEntry(
                name=spec.name,
                signature=spec.signature,
                group=spec.group,
                min_args=spec.min_args,
                max_args=spec.max_args,
                result_type=spec.result_type,
                summary=markdown_prose(spec.summary),
                semantics=markdown_prose(spec.semantics) if spec.semantics else None,
                examples=[
                    FunctionExampleEntry(call=e.call, expect=e.expect) for e in spec.examples
                ],
            )
            for spec in (FUNCTION_CATALOG[name] for name in catalog_names())
        ],
        escape_hatch=_ESCAPE_HATCH,
    )


# ---------------------------------------------------------------------------
# JSON Schemas
# ---------------------------------------------------------------------------


_SCHEMA_FILES: dict[str, str] = {
    "obml": "obml-schema.json",
    "query": "query-schema.json",
}


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
    text = read_schema_text(filename)
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
