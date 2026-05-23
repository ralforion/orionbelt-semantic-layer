"""OSI ↔ OBML conversion endpoints."""

from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException

from orionbelt.api.schemas import (
    ConvertRequest,
    ConvertResponse,
    OBMLtoOSIRequest,
    ValidationDetail,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_converter_module() -> types.ModuleType:
    """Lazy-import the OSI ↔ OBML converter module from ``osi-obml/``."""
    candidates = [
        Path(__file__).resolve().parents[4] / "osi-obml",
        Path("/app/osi-obml"),
    ]
    for candidate in candidates:
        converter_dir = str(candidate)
        if candidate.is_dir() and converter_dir not in sys.path:
            sys.path.insert(0, converter_dir)
    return importlib.import_module("osi_obml_converter")


def _parse_yaml(raw: str) -> dict[str, Any]:
    """Parse YAML string to dict, raising HTTPException on failure."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="YAML must be a mapping (dict)")
    return data


def _run_validation(validate_fn: Any, data: dict[str, Any]) -> ValidationDetail:
    """Run a converter validation function, return structured result."""
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


@router.post(
    "/osi-to-obml",
    response_model=ConvertResponse,
    summary="Convert OSI YAML to OBML",
    description="Converts an Open Semantic Interchange (OSI) model to OBML format.",
)
async def osi_to_obml(body: ConvertRequest) -> ConvertResponse:
    """Convert OSI YAML → OBML YAML."""
    data = _parse_yaml(body.input_yaml)
    mod = _get_converter_module()

    # Validate the OSI input against the vendored OSI v0.2 schema before
    # we touch the converter. Advisory by default — the result lands in
    # ``input_validation`` on the response so callers can surface or
    # ignore as they prefer. v0.1.x inputs run through the legacy shim
    # inside ``OSItoOBML.convert`` so schema_errors here may still be
    # spurious for legacy docs; in that case the conversion still runs.
    input_validation = _run_validation(mod.validate_osi, data)

    try:
        converter = mod.OSItoOBML(data)
        result = converter.convert()
        warnings = list(converter.warnings)
    except Exception as exc:
        logger.exception("OSI → OBML conversion failed")
        raise HTTPException(status_code=422, detail=f"OSI → OBML conversion failed: {exc}") from exc

    output_yaml = yaml.dump(
        result, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )

    validation = _run_validation(mod.validate_obml, result)

    return ConvertResponse(
        output_yaml=output_yaml,
        warnings=warnings,
        validation=validation,
        input_validation=input_validation,
    )


@router.post(
    "/obml-to-osi",
    response_model=ConvertResponse,
    summary="Convert OBML YAML to OSI",
    description="Converts an OBML semantic model to Open Semantic Interchange (OSI) format.",
)
async def obml_to_osi(body: OBMLtoOSIRequest) -> ConvertResponse:
    """Convert OBML YAML → OSI YAML."""
    data = _parse_yaml(body.input_yaml)
    mod = _get_converter_module()

    try:
        converter = mod.OBMLtoOSI(
            data,
            model_name=body.model_name,
            model_description=body.model_description,
            ai_instructions=body.ai_instructions,
        )
        result = converter.convert()
        warnings = list(converter.warnings)
    except Exception as exc:
        logger.exception("OBML → OSI conversion failed")
        raise HTTPException(status_code=422, detail=f"OBML → OSI conversion failed: {exc}") from exc

    output_yaml = yaml.dump(
        result, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
    )

    validation = _run_validation(mod.validate_osi, result)

    return ConvertResponse(
        output_yaml=output_yaml,
        warnings=warnings,
        validation=validation,
    )
