"""Model-loading helpers extracted from the session router."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from orionbelt.api.deps import get_db_vendor, is_query_execute_enabled
from orionbelt.api.warnings_adapter import (
    error_info_to_detail,
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
    surface capacity (429) and validation (422) failures identically. Where
    queries execute, a data source the model names must be configured.
    """
    execution_dialect = get_db_vendor() if is_query_execute_enabled() else None
    try:
        return store.load_model(yaml_str, execution_dialect=execution_dialect, **kwargs)
    except ModelCapacityError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except ModelValidationError as exc:
        raise model_validation_http_error(exc) from None


def model_validation_http_error(exc: ModelValidationError) -> HTTPException:
    """The 422 for a model that fails validation, keeping each error's repair.

    ``hint``, ``suggestions`` and ``context`` reach the caller in ``errors``
    (the validation endpoint's shape), and the one-line ``message`` carries
    them too for clients that only print it.
    """
    error_lines = "; ".join(e.as_text() for e in exc.errors)
    return HTTPException(
        status_code=422,
        detail={
            "message": f"Invalid OBML model: {error_lines}",
            "errors": [error_info_to_detail(e).model_dump(exclude_none=True) for e in exc.errors],
            "warnings": [
                error_info_to_detail(w).model_dump(exclude_none=True) for w in exc.warnings
            ],
        },
    )


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
