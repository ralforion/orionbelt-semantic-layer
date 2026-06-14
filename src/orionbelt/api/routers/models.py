"""Public model discovery endpoint.

``GET /v1/models`` lists the admin-pre-loaded models (via ``MODEL_FILES``)
so BI tools, MCP clients, and LLM agents can discover what's available
without scraping the Flight catalog or the session-scoped routes. This
is the recommended way to pick a model for the Flight ``database`` header
/ pgwire ``database=`` URL parameter.

Dynamic (user-created) sessions are intentionally *not* listed — those
are programmatic-only and have their own ``GET /v1/sessions`` surface.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from orionbelt.api.deps import get_session_manager
from orionbelt.service.session_manager import SessionManager

# Prefix on the constructor keeps the root list route ("") at /v1/models with no
# trailing slash (FastAPI 0.137+ rejects empty paths via include_router prefix).
router = APIRouter(prefix="/models")


class ModelInfo(BaseModel):
    """One pre-loaded model entry in the discovery response."""

    name: str = Field(
        description=(
            "Addressing name. Use as the Flight `database` header value, "
            "the pgwire `database=` URL parameter, or in the OBSQL session-"
            "scoped REST paths (e.g. POST /v1/sessions/<name>/query/semantic-ql)."
        )
    )
    description: str | None = Field(
        default=None,
        description="OBML model `description:` field — short human summary.",
    )
    dimensions: int = 0
    measures: int = 0
    metrics: int = 0
    data_objects: int = 0


class ModelsResponse(BaseModel):
    """Response for ``GET /v1/models``."""

    models: list[ModelInfo] = Field(default_factory=list)
    count: int = 0


@router.get("", response_model=ModelsResponse)
async def list_models(
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
) -> ModelsResponse:
    """List all admin-pre-loaded models.

    Returns models loaded at startup via ``MODEL_FILES``. Each entry
    includes its addressing name, OBML description, and counts of
    declared dims / measures / metrics / data objects.

    The list is **stable**: model names are fixed at server startup, so
    BI tools can hardcode them in connection configs without worrying
    about drift between requests.
    """
    items: list[ModelInfo] = []
    protected_ids = mgr.list_protected_session_ids()

    # One entry per protected (admin-loaded) named session.
    for session_id in protected_ids:
        try:
            store = mgr.get_store(session_id)
        except Exception:
            continue
        for ms in store.list_models():
            model = store.get_model(ms.model_id)
            items.append(_to_info(session_id, model, ms))

    return ModelsResponse(models=items, count=len(items))


def _to_info(name: str, model: object, summary: object) -> ModelInfo:
    """Build a ModelInfo from a SemanticModel + ModelSummary pair."""
    description = getattr(model, "description", None)
    return ModelInfo(
        name=name,
        description=description,
        dimensions=getattr(summary, "dimensions", 0),
        measures=getattr(summary, "measures", 0),
        metrics=getattr(summary, "metrics", 0),
        data_objects=getattr(summary, "data_objects", 0),
    )
