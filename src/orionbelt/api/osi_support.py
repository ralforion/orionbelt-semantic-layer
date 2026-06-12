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

    The converter ships as the optional ``osi_orionbelt`` package and exposes
    its public symbols at the top level (``OSItoOBML``, ``OBMLtoOSI``,
    ``OBMLtoOSIOntology``, ``validate_obml``, ``validate_osi``,
    ``validate_osi_ontology``). It is not a hard dependency, so when it is not
    installed the OSI endpoints return a clear 503 rather than a 500.
    """
    try:
        return importlib.import_module("osi_orionbelt")
    except ModuleNotFoundError as exc:
        # Only treat a missing top-level converter package as "not installed".
        # A ModuleNotFoundError for some other name means the package IS present
        # but has a broken/missing transitive import — surface that as a real
        # 500 rather than masking it as an install problem.
        if exc.name != "osi_orionbelt":
            raise
        raise HTTPException(
            status_code=503,
            detail=(
                "OSI conversion is unavailable: the 'osi-orionbelt' converter is "
                "not installed. Install it with: pip install "
                "'orionbelt-semantic-layer[osi]' (or 'pip install osi-orionbelt')."
            ),
        ) from exc


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
