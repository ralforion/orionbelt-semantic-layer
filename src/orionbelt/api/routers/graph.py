"""OBSL graph and SPARQL endpoints.

Session-scoped routes under /sessions/{session_id}/models/{model_id}/.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from orionbelt.api.deps import get_session_manager
from orionbelt.api.schemas import SPARQLRequest, SPARQLResponse
from orionbelt.obsl.sparql import SPARQLUpdateError
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import (
    SessionExpiredError,
    SessionManager,
    SessionNotFoundError,
)

router = APIRouter()


# -- helpers -----------------------------------------------------------------


def _get_store(session_id: str, mgr: SessionManager) -> ModelStore:
    try:
        return mgr.get_store(session_id)
    except SessionExpiredError:
        raise HTTPException(status_code=410, detail=f"Session '{session_id}' has expired") from None
    except SessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found") from None


# -- endpoints ---------------------------------------------------------------


@router.get(
    "/{session_id}/models/{model_id}/graph",
    tags=["graph"],
    response_class=Response,
    responses={200: {"content": {"text/turtle": {}}}},
)
async def get_graph(
    session_id: str,
    model_id: str,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> Response:
    """Return the OBSL-Core RDF graph as Turtle."""
    store = _get_store(session_id, mgr)
    try:
        artifact = store.get_graph(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    return Response(content=artifact.turtle, media_type="text/turtle")


@router.post(
    "/{session_id}/models/{model_id}/sparql",
    response_model=SPARQLResponse,
    tags=["graph"],
)
async def sparql_query(
    session_id: str,
    model_id: str,
    body: SPARQLRequest,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> SPARQLResponse:
    """Execute a read-only SPARQL query against the model's OBSL graph."""
    store = _get_store(session_id, mgr)
    try:
        result = store.query_graph(model_id, body.query)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    except SPARQLUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SPARQL error: {exc}") from None
    return SPARQLResponse(
        type=result.type,
        variables=result.variables,
        results=result.results,
        boolean=result.boolean,
    )
