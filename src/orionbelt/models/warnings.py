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
    CONFORMED_GRAIN_ASSUMED = "CONFORMED_GRAIN_ASSUMED"
    ORPHAN_DATA_OBJECT = "ORPHAN_DATA_OBJECT"
    SHARED_TABLE_CONTRACT_DISAGREEMENT = "SHARED_TABLE_CONTRACT_DISAGREEMENT"

    # Result / cache / cost guards
    LARGE_RESULT_SET = "LARGE_RESULT_SET"
    CACHE_TTL_FLOOR_HIT = "CACHE_TTL_FLOOR_HIT"

    # An expression calls a function the portable catalog does not carry
    NON_PORTABLE_FUNCTION = "NON_PORTABLE_FUNCTION"

    # A measure declares a dataType too narrow for values its own source column
    # is allowed to hold
    NARROWING_DATA_TYPE = "NARROWING_DATA_TYPE"

    # A model asks for a query time zone but leaves naive columns undeclared
    UNDECLARED_TIMESTAMP_ZONE = "UNDECLARED_TIMESTAMP_ZONE"

    # A nested data object was read from its ``code`` table because the dialect
    # has no FROM-clause unnest. The two sources are not guaranteed to agree.
    NESTED_SOURCE_FALLBACK = "NESTED_SOURCE_FALLBACK"

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
