"""Adapters between internal error/warning shapes and API response shapes."""

from __future__ import annotations

from typing import Any

from orionbelt.api.schemas import (
    ErrorDetail,
    FanTrapRisk,
    ModelHealth,
    StructuredWarning,
)
from orionbelt.models.errors import SemanticError
from orionbelt.service.model_store import ErrorInfo, ModelHealthSummary


def semantic_error_to_warning(e: SemanticError) -> StructuredWarning:
    """Convert internal :class:`SemanticError` → public :class:`StructuredWarning`."""
    return StructuredWarning(
        code=e.code,
        severity=e.severity or "warning",
        message=e.message,
        path=e.path,
        hint=e.hint,
        context=e.context,
    )


def semantic_error_to_detail(e: SemanticError) -> ErrorDetail:
    """Convert a compile error to :class:`ErrorDetail`, keeping its repair fields.

    ``hint``, ``suggestions`` and ``context`` tell an agent how to fix the
    query. Same shape as the validation endpoint's errors.
    """
    return ErrorDetail(
        code=e.code,
        message=e.message,
        path=e.path,
        severity=e.severity,
        hint=e.hint,
        context=e.context,
        suggestions=list(e.suggestions),
    )


def semantic_error_details(errors: list[SemanticError]) -> list[dict[str, Any]]:
    """Compile errors as the dicts a 4xx ``detail`` body carries, unset fields omitted."""
    return [semantic_error_to_detail(e).model_dump(exclude_none=True) for e in errors]


def error_info_to_warning(info: ErrorInfo) -> StructuredWarning:
    """Convert service-layer :class:`ErrorInfo` → public :class:`StructuredWarning`."""
    return StructuredWarning(
        code=info.code,
        severity=info.severity or "warning",
        message=info.message,
        path=info.path,
        hint=info.hint,
        context=info.context,
    )


def error_info_to_detail(info: ErrorInfo) -> ErrorDetail:
    """Convert service-layer :class:`ErrorInfo` → :class:`ErrorDetail`."""
    return ErrorDetail(
        code=info.code,
        message=info.message,
        path=info.path,
        severity=info.severity,
        hint=info.hint,
        context=info.context,
        suggestions=list(info.suggestions),
    )


def health_summary_to_response(summary: ModelHealthSummary | None) -> ModelHealth | None:
    """Convert service-layer :class:`ModelHealthSummary` → public :class:`ModelHealth`."""
    if summary is None:
        return None
    return ModelHealth(
        status=summary.status,
        data_objects=summary.data_objects,
        joins=summary.joins,
        orphan_data_objects=summary.orphan_data_objects,
        fan_trap_risks=[
            FanTrapRisk(
                tables=r.tables,
                reason=r.reason,
                suggested_pattern=r.suggested_pattern,
            )
            for r in summary.fan_trap_risks
        ],
        unreachable_dimensions=summary.unreachable_dimensions,
        warnings_count=summary.warnings_count,
    )
