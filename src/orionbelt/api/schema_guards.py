"""Request-body JSON Schema guards for the REST ingestion boundary.

These FastAPI dependencies validate incoming model and query payloads
against the published JSON Schemas (``schema/obml-schema.json`` /
``query-schema.json``) *before* they are processed. Validating here, at the
external boundary, makes the schema the contract every real request is held
to — so the schema stays provably correct and external consumers rely on
the same rules the engine enforces — without imposing JSON-Schema strictness
on the internal, coercion-tolerant model/query construction paths.

A violation returns HTTP 422 with the structured schema errors, alongside
the Pydantic validation FastAPI already performs.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from orionbelt.models.errors import SemanticError
from orionbelt.parser.schema_validation import validate_obml_yaml, validate_query_document


async def _json_body(request: Request) -> Any:
    """Parsed JSON body, or ``None`` if absent/unparseable.

    A malformed body is left to FastAPI's own request parsing to reject.
    """
    try:
        return await request.json()
    except ValueError:
        return None


def _raise_422(errors: list[SemanticError]) -> None:
    raise HTTPException(
        status_code=422,
        detail={
            "message": "Request failed JSON Schema validation.",
            "errors": [{"code": e.code, "message": e.message, "path": e.path} for e in errors],
        },
    )


async def validate_query_body(request: Request) -> None:
    """Validate the query payload of a request against ``query-schema.json``.

    Handles both wrapped bodies (``{"query": {...}, ...}``) and bare
    ``QueryObject`` bodies (identified by a top-level ``select``).
    """
    raw = await _json_body(request)
    if not isinstance(raw, dict):
        return
    candidate = raw.get("query")
    if not isinstance(candidate, dict):
        candidate = raw if "select" in raw else None
    if isinstance(candidate, dict):
        errors = validate_query_document(candidate)
        if errors:
            _raise_422(errors)


async def validate_model_body(request: Request) -> None:
    """Validate a request's ``model_yaml`` against ``obml-schema.json``."""
    raw = await _json_body(request)
    if not isinstance(raw, dict):
        return
    text = raw.get("model_yaml")
    if isinstance(text, str) and text.strip():
        errors = validate_obml_yaml(text)
        if errors:
            _raise_422(errors)
