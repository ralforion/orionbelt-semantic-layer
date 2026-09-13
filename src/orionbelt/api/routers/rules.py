"""Business rule endpoints: list with statistics, describe, compile one, compile all.

Session-scoped under /sessions/{session_id}/models/{model_id}/rules. A rule
compiles to an ordinary query whose rows are its findings (members for
classification and eligibility, violations for validation and constraint);
these endpoints show that query and its SQL. Evaluation is a separate step.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from orionbelt.api.deps import get_db_vendor, get_session_manager
from orionbelt.api.routers.model_api import _concept_mappings, _get_model, _get_store
from orionbelt.api.schemas import (
    RuleCompileAllResponse,
    RuleCompileRequest,
    RuleCompileResponse,
    RuleCompileStatus,
    RuleDetail,
    RuleListResponse,
    RuleStatistics,
    RuleSummary,
)
from orionbelt.api.services.query_compilation import _resolve_dialect, compile_query_or_raise
from orionbelt.api.warnings_adapter import semantic_error_to_warning
from orionbelt.compiler.rules import RuleCompiler, RulePlan
from orionbelt.models.semantic import Rule, SemanticModel
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import SessionManager

router = APIRouter()


def _error_text(exc: HTTPException) -> str:
    """One line for a compile failure, whatever shape the detail has."""
    detail = exc.detail
    if isinstance(detail, dict):
        errors = detail.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(str(e.get("message", e)) for e in errors)
        return str(detail.get("message") or detail.get("error") or detail)
    return str(detail)


def _try_compile(
    store: ModelStore, model_id: str, plan: RulePlan, dialect: str
) -> tuple[Any | None, str | None]:
    """``(result, None)`` when the rule's query compiles, ``(None, why)`` when not."""
    try:
        return compile_query_or_raise(
            store=store, model_id=model_id, query=plan.query, dialect=dialect
        ), None
    except HTTPException as exc:
        return None, _error_text(exc)


def _summary(plan: RulePlan, rule: Rule, executable: bool, error: str | None) -> RuleSummary:
    return RuleSummary(
        name=rule.name,
        type=rule.type.value,
        level=plan.level,
        findings=plan.findings,
        severity=rule.severity.value if rule.severity else None,
        grain=list(rule.grain),
        description=rule.description,
        owner=rule.owner,
        dimensions=plan.dimensions,
        measures=plan.measures,
        depends_on=plan.depends_on,
        executable=executable,
        error=error,
    )


def _dialect_for(model: SemanticModel, requested: str | None, db_vendor: str | None) -> str:
    return _resolve_dialect(request_dialect=requested, model=model, fallback=db_vendor)


def build_rule_list(
    store: ModelStore, model_id: str, model: SemanticModel, dialect: str
) -> RuleListResponse:
    """Every rule with its plan facts and whether it compiles, plus counts."""
    compiler = RuleCompiler(model)
    summaries: list[RuleSummary] = []
    stats = RuleStatistics(total=len(model.rules))
    for rule in model.rules.values():
        plan = compiler.plan(rule)
        _, error = _try_compile(store, model_id, plan, dialect)
        summary = _summary(plan, rule, error is None, error)
        summaries.append(summary)
        stats.by_type[summary.type] = stats.by_type.get(summary.type, 0) + 1
        stats.by_level[summary.level] = stats.by_level.get(summary.level, 0) + 1
        if summary.severity:
            stats.by_severity[summary.severity] = stats.by_severity.get(summary.severity, 0) + 1
        if summary.executable:
            stats.executable += 1
        else:
            stats.not_executable += 1
    return RuleListResponse(dialect=dialect, rules=summaries, statistics=stats)


def build_rule_detail(
    store: ModelStore, model_id: str, model: SemanticModel, name: str, dialect: str
) -> RuleDetail:
    rule = model.rules.get(name)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule '{name}' not found")
    plan = RuleCompiler(model).plan(rule)
    _, error = _try_compile(store, model_id, plan, dialect)
    summary = _summary(plan, rule, error is None, error)
    return RuleDetail(
        **summary.model_dump(),
        condition=rule.condition.model_dump(by_alias=True, exclude_none=True),
        synonyms=list(rule.synonyms),
        external_concept_mappings=_concept_mappings(rule.external_concept_mappings),
        query=plan.query.model_dump(by_alias=True, mode="json", exclude_defaults=True),
    )


def build_rule_compile(
    store: ModelStore, model_id: str, model: SemanticModel, name: str, dialect: str
) -> RuleCompileResponse:
    rule = model.rules.get(name)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Rule '{name}' not found")
    plan = RuleCompiler(model).plan(rule)
    result = compile_query_or_raise(
        store=store, model_id=model_id, query=plan.query, dialect=dialect
    )
    return RuleCompileResponse(
        name=name,
        level=plan.level,
        findings=plan.findings,
        dialect=dialect,
        sql=result.sql,
        query=plan.query.model_dump(by_alias=True, mode="json", exclude_defaults=True),
        warnings=[semantic_error_to_warning(w) for w in result.warnings],
    )


def build_rule_compile_all(
    store: ModelStore, model_id: str, model: SemanticModel, dialect: str
) -> RuleCompileAllResponse:
    """Compile every rule; a failure is a row with its reason, not a 4xx."""
    compiler = RuleCompiler(model)
    out = RuleCompileAllResponse(dialect=dialect)
    for rule in model.rules.values():
        plan = compiler.plan(rule)
        result, error = _try_compile(store, model_id, plan, dialect)
        out.results.append(
            RuleCompileStatus(
                name=rule.name,
                status="compiled" if result is not None else "failed",
                level=plan.level,
                findings=plan.findings,
                sql=result.sql if result is not None else None,
                error=error,
            )
        )
        if result is not None:
            out.compiled += 1
        else:
            out.failed += 1
    return out


@router.get(
    "/{session_id}/models/{model_id}/rules",
    response_model=RuleListResponse,
    tags=["rules"],
)
async def list_rules(
    session_id: str,
    model_id: str,
    dialect: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleListResponse:
    """List the model's business rules with statistics.

    Each rule carries its level (row or aggregate), what an evaluation
    returns (matches or violations), its grain, what it reads, the rules it
    depends on, and whether its query compiles for the dialect (with the
    reason when it does not). ``statistics`` counts by type, level and
    severity, and executable versus not.
    """
    store = _get_store(session_id, mgr)
    model = _get_model(session_id, model_id, mgr)
    return build_rule_list(store, model_id, model, _dialect_for(model, dialect, db_vendor))


@router.post(
    "/{session_id}/models/{model_id}/rules/compile",
    response_model=RuleCompileAllResponse,
    tags=["rules"],
)
async def compile_all_rules(
    session_id: str,
    model_id: str,
    body: RuleCompileRequest | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleCompileAllResponse:
    """Compile every rule and report per-rule status, never hiding a failure."""
    store = _get_store(session_id, mgr)
    model = _get_model(session_id, model_id, mgr)
    requested = body.dialect if body else None
    return build_rule_compile_all(store, model_id, model, _dialect_for(model, requested, db_vendor))


@router.get(
    "/{session_id}/models/{model_id}/rules/{name}",
    response_model=RuleDetail,
    tags=["rules"],
)
async def get_rule(
    session_id: str,
    model_id: str,
    name: str,
    dialect: str | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleDetail:
    """One rule: its authored condition, what it reads, and the query behind it."""
    store = _get_store(session_id, mgr)
    model = _get_model(session_id, model_id, mgr)
    return build_rule_detail(store, model_id, model, name, _dialect_for(model, dialect, db_vendor))


@router.post(
    "/{session_id}/models/{model_id}/rules/{name}/compile",
    response_model=RuleCompileResponse,
    tags=["rules"],
)
async def compile_rule(
    session_id: str,
    model_id: str,
    name: str,
    body: RuleCompileRequest | None = None,
    mgr: SessionManager = Depends(get_session_manager),  # noqa: B008
    db_vendor: str | None = Depends(get_db_vendor),  # noqa: B008
) -> RuleCompileResponse:
    """Compile one rule to the SQL whose rows are its findings.

    Compile failures come back the way ``query/sql`` reports them (422 with
    structured errors, 400 for an unsupported dialect).
    """
    store = _get_store(session_id, mgr)
    model = _get_model(session_id, model_id, mgr)
    requested = body.dialect if body else None
    return build_rule_compile(
        store, model_id, model, name, _dialect_for(model, requested, db_vendor)
    )
