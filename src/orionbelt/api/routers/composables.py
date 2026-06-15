"""Artefacts Composability Resolution (ACR) endpoints.

Given an anchor (a whole in-progress query, or one or more named artefacts),
return the artefacts that can still be added to the query and yield a valid,
fanout-free result. See ``design/PLAN_graph_reasoning.md``.

Session-scoped routes under /sessions/{session_id}/models/{model_id}/.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from orionbelt.api.deps import get_session_manager
from orionbelt.api.routers.model_api import _get_model
from orionbelt.api.schemas import ComposablesResponse
from orionbelt.compiler.composability import (
    ComposablesResult,
    resolve_composables_for_anchors,
    resolve_composables_for_query,
)
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


def _to_response(result: ComposablesResult) -> ComposablesResponse:
    return ComposablesResponse(
        anchorObjects=result.anchor_objects,
        dimensions=result.dimensions,
        measures=result.measures,
        metrics=result.metrics,
        cflMeasures=result.cfl_measures,
        cflMetrics=result.cfl_metrics,
    )


def build_composables(
    model: SemanticModel,
    *,
    query: QueryObject | None = None,
    anchors: list[str] | None = None,
    anchor_type: str | None = None,
) -> ComposablesResponse:
    """Resolve composables from either a query body or named anchors."""
    if query is not None:
        return _to_response(resolve_composables_for_query(model, query))
    return _to_response(resolve_composables_for_anchors(model, anchors or [], anchor_type))


@router.post(
    "/{session_id}/models/{model_id}/composables",
    response_model=ComposablesResponse,
    tags=["model-discovery"],
)
async def composables_for_query(
    session_id: str,
    model_id: str,
    query: QueryObject,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ComposablesResponse:
    """Resolve artefacts composable with an in-progress query (query-as-anchor)."""
    model = _get_model(session_id, model_id, mgr)
    return build_composables(model, query=query)


@router.get(
    "/{session_id}/models/{model_id}/composables",
    response_model=ComposablesResponse,
    tags=["model-discovery"],
)
async def composables_for_anchors(
    session_id: str,
    model_id: str,
    anchor: Annotated[list[str], Query()] = [],  # noqa: B006 — FastAPI query list
    anchor_type: Annotated[str | None, Query(alias="anchorType")] = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ComposablesResponse:
    """Resolve artefacts composable with one or more named anchors."""
    model = _get_model(session_id, model_id, mgr)
    return build_composables(model, anchors=anchor, anchor_type=anchor_type)
