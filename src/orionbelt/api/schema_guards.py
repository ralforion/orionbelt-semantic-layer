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

import json
from typing import Any

from fastapi import HTTPException, Request

from orionbelt.models.errors import SemanticError
from orionbelt.parser.schema_validation import (
    validate_obml_document,
    validate_obml_yaml,
    validate_query_document,
)


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


def _model_json_errors(model_json: object) -> list[SemanticError]:
    """Schema errors for a ``model_json`` payload (dict or JSON string)."""
    document = model_json
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except ValueError:
            return []  # malformed JSON — let the loader report it precisely
    if not isinstance(document, dict):
        return []
    public = {k: v for k, v in document.items() if not str(k).startswith("_")}
    return validate_obml_document(public)


async def validate_model_body(request: Request) -> None:
    """Validate a request's ``model_yaml`` / ``model_json`` against the schema.

    Both forms reach the same load path, so both must be gated: ``model_json``
    may be a JSON object or an auto-parsed JSON string.
    """
    raw = await _json_body(request)
    if not isinstance(raw, dict):
        return
    errors: list[SemanticError] = []
    text = raw.get("model_yaml")
    if isinstance(text, str) and text.strip():
        errors.extend(validate_obml_yaml(text))
    if raw.get("model_json") is not None:
        errors.extend(_model_json_errors(raw.get("model_json")))
    if errors:
        _raise_422(errors)


def _prefix_path(error: SemanticError, prefix: str) -> SemanticError:
    path = prefix if error.path in (None, "(root)") else f"{prefix}.{error.path}"
    return error.model_copy(update={"path": path})


async def validate_oneshot_body(request: Request) -> None:
    """Validate a oneshot-batch body: its ``model_yaml`` and every query.

    The batch carries an optional inline ``model_yaml`` and a ``queries``
    list of ``{"query": {...}}`` items; each is validated against its schema
    with the offending location reported in ``path``.
    """
    raw = await _json_body(request)
    if not isinstance(raw, dict):
        return
    errors: list[SemanticError] = []
    text = raw.get("model_yaml")
    if isinstance(text, str) and text.strip():
        errors.extend(validate_obml_yaml(text))
    queries = raw.get("queries")
    if isinstance(queries, list):
        for index, item in enumerate(queries):
            if isinstance(item, dict) and isinstance(item.get("query"), dict):
                for error in validate_query_document(item["query"]):
                    errors.append(_prefix_path(error, f"queries[{index}].query"))
    if errors:
        _raise_422(errors)
