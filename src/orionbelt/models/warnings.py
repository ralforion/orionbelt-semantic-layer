"""Stable warning code taxonomy for the OBSL API.

These codes are part of the public API contract: agents may branch on them.
Codes are extended over time; never repurposed.
"""

from __future__ import annotations

from typing import Any

from orionbelt.models.errors import SemanticError


class WarningCode:
    """Stable identifiers used in the ``code`` field of structured warnings.

    Plan: ``design/PLAN_agent_api_improvements.md`` §3.4.
    """

    # Query-time grain / filter-context overrides that can't combine
    GRAIN_OVERRIDE_INCOMPATIBLE = "GRAIN_OVERRIDE_INCOMPATIBLE"
    FILTER_CONTEXT_OVERRIDE_INCOMPATIBLE = "FILTER_CONTEXT_OVERRIDE_INCOMPATIBLE"

    # PoP / Cumulative metric constraint violations
    POP_CONSTRAINT_VIOLATED = "POP_CONSTRAINT_VIOLATED"
    CUMULATIVE_CONSTRAINT_VIOLATED = "CUMULATIVE_CONSTRAINT_VIOLATED"

    # Multi-fact / fan-trap / structural risks
    FAN_TRAP_RISK = "FAN_TRAP_RISK"
    ORPHAN_DATA_OBJECT = "ORPHAN_DATA_OBJECT"
    SHARED_TABLE_CONTRACT_DISAGREEMENT = "SHARED_TABLE_CONTRACT_DISAGREEMENT"

    # Result / cache / cost guards
    LARGE_RESULT_SET = "LARGE_RESULT_SET"
    CACHE_TTL_FLOOR_HIT = "CACHE_TTL_FLOOR_HIT"

    # Compile-time, post-codegen SQL validator emissions
    SQL_VALIDATION = "SQL_VALIDATION"

    # Generic merge-time warning (extends/inherits)
    MERGE_WARNING = "MERGE_WARNING"

    # Combination of options the planner ignored (e.g. totals + PoP)
    INCOMPATIBLE_COMBINATION = "INCOMPATIBLE_COMBINATION"


def warning(
    code: str,
    message: str,
    *,
    path: str | None = None,
    hint: str | None = None,
    context: dict[str, Any] | None = None,
) -> SemanticError:
    """Build a structured warning with severity='warning'."""
    return SemanticError(
        code=code,
        message=message,
        path=path,
        hint=hint,
        context=context,
        severity="warning",
    )
