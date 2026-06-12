"""Shared helpers for the OSI <-> OBML conversion endpoints.

Both the stateless ``/convert`` router and the session-scoped model
endpoints (load-from-OSI / export-to-OSI) lean on the ``osi_orionbelt``
converter package (a uv workspace member under ``packages/osi-orionbelt``,
also published standalone to PyPI). The import, YAML parsing, and
validation-result adaptation live here so the two call sites stay in sync
rather than duplicating the logic.
"""

from __future__ import annotations

import importlib
import logging
import types
from typing import Any

import yaml
from fastapi import HTTPException

from orionbelt.api.schemas import ValidationDetail

logger = logging.getLogger(__name__)


def get_converter_module() -> types.ModuleType:
    """Import the OSI <-> OBML converter package.

    The converter ships as the ``osi_orionbelt`` package and exposes the same
    public symbols at the top level (``OSItoOBML``, ``OBMLtoOSI``,
    ``OBMLtoOSIOntology``, ``validate_obml``, ``validate_osi``,
    ``validate_osi_ontology``).
    """
    return importlib.import_module("osi_orionbelt")


def parse_yaml(raw: str) -> dict[str, Any]:
    """Parse a YAML string to a dict, raising HTTPException on failure."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="YAML must be a mapping (dict)")
    return data


def run_validation(validate_fn: Any, data: dict[str, Any]) -> ValidationDetail:
    """Run a converter validation function, return a structured result."""
    try:
        vr = validate_fn(data)
        return ValidationDetail(
            schema_valid=not vr.schema_errors,
            semantic_valid=not vr.semantic_errors,
            schema_errors=list(vr.schema_errors),
            semantic_errors=list(vr.semantic_errors),
            semantic_warnings=list(vr.semantic_warnings),
        )
    except Exception:
        logger.warning("Validation skipped due to error", exc_info=True)
        return ValidationDetail(
            schema_valid=True,
            semantic_valid=True,
            schema_errors=[],
            semantic_errors=[],
            semantic_warnings=["Validation skipped (validator unavailable)"],
        )
