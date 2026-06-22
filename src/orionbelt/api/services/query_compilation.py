"""Query-compilation helpers and core logic extracted from the session router."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from orionbelt.api.schemas import (
    ExplainCflLegResponse,
    ExplainJoinResponse,
    ExplainPlanResponse,
    JoinPathStep,
    QueryCompileResponse,
    QueryPlanResponse,
    ResolvedInfoResponse,
    SemanticQLCompileResponse,
    StructuredWarning,
)
from orionbelt.api.warnings_adapter import semantic_error_to_warning
from orionbelt.compiler.fanout import FanoutError
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.compiler.sql_translator import SQLTranslationError
from orionbelt.compiler.validator import format_sql
from orionbelt.dialect.base import UnsupportedAggregationError, UnsupportedGroupingError
from orionbelt.dialect.registry import UnsupportedDialectError
from orionbelt.service.model_store import ModelStore


def _resolve_dialect(
    *, request_dialect: str | None, model: Any, fallback: str | None = None
) -> str:
    """Resolve the dialect for a query.

    Order: explicit request dialect → model.settings.default_dialect →
    ``fallback`` (typically ``DB_VENDOR``) → ``"postgres"``.
    """
    if request_dialect:
        return request_dialect
    settings = getattr(model, "settings", None)
    model_default = getattr(settings, "default_dialect", None) if settings else None
    if model_default:
        return str(model_default)
    if fallback:
        return fallback
    return "postgres"


def _obsql_translation_errors(exc: SQLTranslationError) -> HTTPException:
    """Map a SQLTranslationError to an HTTP 400 with structured error list."""
    return HTTPException(
        status_code=400,
        detail={
            "error": "OrionBelt Semantic QL translation failed",
            "errors": [
                {"code": e.code, "message": e.message, "context": e.context} for e in exc.errors
            ],
        },
    )


def _join_path_steps(result: Any) -> list[JoinPathStep]:
    """Convert ExplainPlan joins → API JoinPathStep list."""
    if not result.explain:
        return []
    steps: list[JoinPathStep] = []
    for j in result.explain.joins:
        steps.append(
            JoinPathStep(
                from_object=j.from_object,
                to_object=j.to_object,
                cardinality=j.cardinality or "many-to-one",
                fk=", ".join(j.join_columns) if j.join_columns else None,
            )
        )
    return steps


def compile_query_or_raise(*, store: ModelStore, model_id: str, query: Any, dialect: str) -> Any:
    """Compile a query, translating compile-stage domain errors to HTTPException.

    Used by the ``query/sql`` and ``query/execute`` handlers, which surface
    compile failures as 4xx HTTP errors.
    """
    try:
        return store.compile_query(model_id, query, dialect)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found") from None
    except UnsupportedDialectError:
        raise HTTPException(status_code=400, detail=f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Query resolution failed",
                "errors": [
                    {"code": e.code, "message": e.message, "path": e.path} for e in exc.errors
                ],
            },
        ) from None
    except FanoutError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "Query fanout detected", "message": exc.message},
        ) from None
    except UnsupportedAggregationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported aggregation",
                "message": str(exc),
                "dialect": exc.dialect,
                "aggregation": exc.aggregation,
            },
        ) from None
    except UnsupportedGroupingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Unsupported grouping",
                "message": str(exc),
                "dialect": exc.dialect,
                "grouping": exc.grouping,
            },
        ) from None


def build_compile_response(result: Any) -> QueryCompileResponse:
    """Build the ``query/sql`` response payload from a compilation result."""
    explain_resp = None
    if result.explain:
        explain_resp = ExplainPlanResponse(
            planner=result.explain.planner,
            planner_reason=result.explain.planner_reason,
            base_object=result.explain.base_object,
            base_object_reason=result.explain.base_object_reason,
            joins=[
                ExplainJoinResponse(
                    from_object=j.from_object,
                    to_object=j.to_object,
                    join_columns=j.join_columns,
                    reason=j.reason,
                )
                for j in result.explain.joins
            ],
            where_filter_count=result.explain.where_filter_count,
            having_filter_count=result.explain.having_filter_count,
            has_totals=result.explain.has_totals,
            cfl_legs=[
                ExplainCflLegResponse(
                    measure_source=leg.measure_source,
                    common_root=leg.common_root,
                    reason=leg.reason,
                    measures=leg.measures,
                    joins=leg.joins,
                )
                for leg in result.explain.cfl_legs
            ],
        )
    return QueryCompileResponse(
        sql=format_sql(result.sql, result.dialect),
        dialect=result.dialect,
        resolved=ResolvedInfoResponse(
            fact_tables=result.resolved.fact_tables,
            dimensions=result.resolved.dimensions,
            measures=result.resolved.measures,
        ),
        warnings=[semantic_error_to_warning(w) for w in result.warnings],
        sql_valid=result.sql_valid,
        explain=explain_resp,
        physical_tables=list(result.physical_tables),
    )


def compile_query_for_plan(
    *, store: ModelStore, model_id: str, query: Any, dialect: str
) -> tuple[Any, QueryPlanResponse | None]:
    """Compile for the ``query/plan`` endpoint.

    Returns ``(result, None)`` on success, or ``(None, error_response)`` when
    compilation fails — the plan endpoint reports compile failures as a
    200 ``QueryPlanResponse`` with ``would_compile=False`` rather than raising.
    The 400 ``UnsupportedDialectError`` path still raises.
    """
    try:
        result = store.compile_query(model_id, query, dialect)
    except UnsupportedDialectError:
        raise HTTPException(status_code=400, detail=f"Unsupported dialect: '{dialect}'") from None
    except ResolutionError as exc:
        return None, QueryPlanResponse(
            status="error",
            warnings=[semantic_error_to_warning(e) for e in exc.errors],
            would_compile=False,
        )
    except FanoutError as exc:
        return None, QueryPlanResponse(
            status="error",
            warnings=[
                StructuredWarning(
                    code="FANOUT_ERROR",
                    severity="error",
                    message=exc.message,
                )
            ],
            would_compile=False,
        )
    except UnsupportedAggregationError as exc:
        return None, QueryPlanResponse(
            status="error",
            warnings=[
                StructuredWarning(
                    code="UNSUPPORTED_AGGREGATION",
                    severity="error",
                    message=str(exc),
                    context={"dialect": exc.dialect, "aggregation": exc.aggregation},
                )
            ],
            would_compile=False,
        )
    except UnsupportedGroupingError as exc:
        return None, QueryPlanResponse(
            status="error",
            warnings=[
                StructuredWarning(
                    code="UNSUPPORTED_GROUPING",
                    severity="error",
                    message=str(exc),
                    context={"dialect": exc.dialect, "grouping": exc.grouping},
                )
            ],
            would_compile=False,
        )
    return result, None


def build_semantic_ql_compile_response(result: Any, query: Any) -> SemanticQLCompileResponse:
    """Build the ``semantic-ql/compile`` response payload."""
    from orionbelt.api.services.query_execution import _build_explain_response

    return SemanticQLCompileResponse(
        sql=format_sql(result.sql, result.dialect),
        dialect=result.dialect,
        query=query.model_dump(by_alias=True, mode="json"),
        resolved=ResolvedInfoResponse(
            fact_tables=result.resolved.fact_tables,
            dimensions=result.resolved.dimensions,
            measures=result.resolved.measures,
        ),
        warnings=[semantic_error_to_warning(w) for w in result.warnings],
        sql_valid=result.sql_valid,
        explain=_build_explain_response(result),
        physical_tables=list(result.physical_tables),
    )
