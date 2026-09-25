"""Lineage endpoints: what a dimension, measure, metric, rule or query is built from.

Session-scoped under /sessions/{session_id}/models/{model_id}/{kind}/{name}/lineage
and /sessions/{session_id}/query/lineage. Names are looked up per artefact type,
because a rule may share its name with a measure.
"""

from __future__ import annotations

import enum

from fastapi import APIRouter, Depends, HTTPException, Response

from orionbelt.api.deps import get_db_vendor, get_session_manager
from orionbelt.api.routers.model_api import _get_model, _get_store
from orionbelt.api.schema_guards import validate_query_body
from orionbelt.api.schemas import (
    LineageEdgeItem,
    LineageNodeItem,
    LineageResponse,
    SessionQueryRequest,
)
from orionbelt.api.services.query_compilation import _resolve_dialect, compile_query_or_raise
from orionbelt.models.query import QueryObject
from orionbelt.service.lineage import (
    Lineage,
    LineageBuilder,
    LineageError,
    query_joins,
    to_turtle,
)
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


class LineageFormat(enum.StrEnum):
    """Representation of a lineage response."""

    json = "json"
    mermaid = "mermaid"
    turtle = "turtle"


#: OpenAPI description of the non-JSON bodies the ``format`` parameter selects.
LINEAGE_RESPONSES: dict[int | str, dict[str, object]] = {
    200: {
        "content": {"text/vnd.mermaid": {}, "text/turtle": {}},
        "description": "JSON by default; Mermaid or Turtle text with format=mermaid|turtle",
    }
}


def lineage_response(
    lineage: Lineage, model_id: str, fmt: LineageFormat = LineageFormat.json
) -> LineageResponse | Response:
    """The lineage as JSON, Mermaid text, or Turtle over the OBSL graph's IRIs."""
    if fmt is LineageFormat.mermaid:
        return Response(content=lineage.to_mermaid(), media_type="text/vnd.mermaid")
    if fmt is LineageFormat.turtle:
        return Response(content=to_turtle(lineage, model_id), media_type="text/turtle")
    return LineageResponse(
        root=lineage.root,
        nodes=[LineageNodeItem(**vars(n)) for n in lineage.nodes],
        edges=[LineageEdgeItem(**vars(e)) for e in lineage.edges],
        mermaid=lineage.to_mermaid(),
    )


def artefact_lineage(
    session_id: str,
    model_id: str,
    kind: str,
    name: str,
    mgr: SessionManager,
    fmt: LineageFormat = LineageFormat.json,
) -> LineageResponse | Response:
    """Lineage of the *kind* artefact *name*; 404 when the model has none by that name."""
    builder = LineageBuilder(_get_model(session_id, model_id, mgr))
    build = {
        "dimension": builder.dimension,
        "measure": builder.measure,
        "metric": builder.metric,
        "rule": builder.rule,
    }[kind]
    try:
        return lineage_response(build(name), model_id, fmt)
    except LineageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def query_lineage(
    store: ModelStore,
    model_id: str,
    query: QueryObject,
    dialect: str | None,
    fmt: LineageFormat = LineageFormat.json,
) -> LineageResponse | Response:
    """Lineage of *query*, with the joins the planner chose for it."""
    model = store.get_model(model_id)
    resolved = _resolve_dialect(request_dialect=dialect, model=model, fallback=get_db_vendor())
    result = compile_query_or_raise(store=store, model_id=model_id, query=query, dialect=resolved)
    lineage = LineageBuilder(model).query(query, query_joins(result))
    return lineage_response(lineage, model_id, fmt)


def _register(kind: str, plural: str) -> None:
    @router.get(
        f"/{{session_id}}/models/{{model_id}}/{plural}/{{name}}/lineage",
        response_model=LineageResponse,
        responses=LINEAGE_RESPONSES,
        name=f"{kind}_lineage",
        summary=f"Lineage of a {kind}",
    )
    async def _lineage(
        session_id: str,
        model_id: str,
        name: str,
        format: LineageFormat = LineageFormat.json,  # noqa: A002 - public query parameter
        mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    ) -> LineageResponse | Response:
        return artefact_lineage(session_id, model_id, kind, name, mgr, format)

    _lineage.__doc__ = (
        f"What the {kind} is built from, down to the tables it reads, "
        "as nodes, edges and a Mermaid flowchart."
    )


for _kind, _plural in (
    ("dimension", "dimensions"),
    ("measure", "measures"),
    ("metric", "metrics"),
    ("rule", "rules"),
):
    _register(_kind, _plural)


@router.post(
    "/{session_id}/query/lineage",
    response_model=LineageResponse,
    responses=LINEAGE_RESPONSES,
    dependencies=[Depends(validate_query_body)],
)
async def post_query_lineage(
    session_id: str,
    body: SessionQueryRequest,
    format: LineageFormat = LineageFormat.json,  # noqa: A002 - public query parameter
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> LineageResponse | Response:
    """What a query is built from, including the joins the planner chose for it."""
    store = _get_store(session_id, mgr)
    try:
        store.get_model(body.model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{body.model_id}' not found") from None
    return query_lineage(store, body.model_id, body.query, body.dialect, format)
