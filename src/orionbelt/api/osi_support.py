"""Shared helpers for the OSI <-> OBML conversion endpoints.

Both the stateless ``/convert`` router and the session-scoped model
endpoints (load-from-OSI / export-to-OSI) lean on the vendored
``osi_obml_converter`` module under ``osi-obml/``. The lazy import, YAML
parsing, and validation-result adaptation live here so the two call sites
stay in sync rather than duplicating the logic.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from orionbelt.api.schemas import ValidationDetail

logger = logging.getLogger(__name__)


def get_converter_module() -> types.ModuleType:
    """Lazy-import the OSI <-> OBML converter module.

    Searches, in order: the copy bundled into the wheel as package data
    (``orionbelt/_osi_obml``, see pyproject force-include), the repo-root
    ``osi-obml/`` used by editable/dev installs, and the legacy
    ``/app/osi-obml`` Docker layout. The first existing directory wins so
    a non-editable wheel install can still import the converter.
    """
    pkg_root = Path(__file__).resolve().parents[1]
    candidates = [
        pkg_root / "_osi_obml",
        Path(__file__).resolve().parents[3] / "osi-obml",
        Path("/app/osi-obml"),
    ]
    for candidate in candidates:
        converter_dir = str(candidate)
        if candidate.is_dir() and converter_dir not in sys.path:
            sys.path.insert(0, converter_dir)
    return importlib.import_module("osi_obml_converter")


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
