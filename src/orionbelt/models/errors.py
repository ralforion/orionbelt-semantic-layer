"""Structured error and warning models with YAML source position tracking."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SourceSpan(BaseModel):
    """Points to exact location in YAML source for error reporting."""

    file: str
    line: int
    column: int
    end_line: int | None = None
    end_column: int | None = None


class SemanticError(BaseModel):
    """A structured error or warning with optional source position and remediation.

    Used uniformly for errors (``severity="error"``) and warnings (``severity="warning"``).
    See ``models/warnings.py`` for the stable warning code taxonomy.
    """

    code: str
    message: str
    path: str | None = None
    span: SourceSpan | None = None
    suggestions: list[str] = Field(default_factory=list)
    severity: str = "error"
    hint: str | None = Field(
        default=None,
        description="Optional remediation suggestion (single sentence)",
    )
    context: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional structured detail (e.g. which measure / dataObject / column) so "
            "agents can branch on the data without parsing the message."
        ),
    )


class ValidationResult(BaseModel):
    """Result of semantic model validation."""

    valid: bool
    errors: list[SemanticError] = Field(default_factory=list)
    warnings: list[SemanticError] = Field(default_factory=list)
