"""Structured error models with YAML source position tracking."""

from __future__ import annotations

from pydantic import BaseModel


class SourceSpan(BaseModel):
    """Points to exact location in YAML source for error reporting."""

    file: str
    line: int
    column: int
    end_line: int | None = None
    end_column: int | None = None


class SemanticError(BaseModel):
    """A structured error with optional source position and suggestions."""

    code: str
    message: str
    path: str | None = None
    span: SourceSpan | None = None
    suggestions: list[str] = []
    severity: str = "error"


class ValidationResult(BaseModel):
    """Result of semantic model validation."""

    valid: bool
    errors: list[SemanticError] = []
    warnings: list[SemanticError] = []
