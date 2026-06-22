"""Model-loading helpers extracted from the session router."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from orionbelt.api.warnings_adapter import (
    error_info_to_warning,
    health_summary_to_response,
)
from orionbelt.service.model_store import (
    LoadResult,
    ModelCapacityError,
    ModelStore,
    ModelValidationError,
)


def _load_obml(store: ModelStore, yaml_str: str | None = None, **kwargs: Any) -> LoadResult:
    """Load OBML into the store, mapping store errors to HTTP responses.

    Shared by the plain OBML upload and the OSI-converted upload so both
    surface capacity (429) and validation (422) failures identically.
    """
    try:
        return store.load_model(yaml_str, **kwargs)
    except ModelCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except ModelValidationError as exc:
        error_lines = "; ".join(
            f"[{e.code}] {e.message}" + (f" (at {e.path})" if e.path else "") for e in exc.errors
        )
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"Invalid OBML model: {error_lines}",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
                "warnings": [
                    {"code": w.code, "message": w.message, "path": w.path} for w in exc.warnings
                ],
            },
        ) from None


def _model_load_fields(result: LoadResult) -> dict[str, Any]:
    """Shared kwargs for (OSI)ModelLoadResponse from a store LoadResult."""
    return {
        "model_id": result.model_id,
        "data_objects": result.data_objects,
        "dimensions": result.dimensions,
        "measures": result.measures,
        "metrics": result.metrics,
        "warnings": [error_info_to_warning(w) for w in result.warnings],
        "model_load": result.model_load,
        "health": health_summary_to_response(result.health),
    }
