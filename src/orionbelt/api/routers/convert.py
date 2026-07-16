"""OSI ↔ OBML conversion endpoints."""

from __future__ import annotations

import logging

import yaml
from fastapi import APIRouter, HTTPException

from orionbelt.api.osi_support import get_converter_module, parse_yaml, run_validation
from orionbelt.api.schemas import (
    ConvertRequest,
    ConvertResponse,
    OBMLtoOSIRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/osi-to-obml",
    response_model=ConvertResponse,
    summary="Convert OSI YAML to OBML",
    description="Converts an Open Semantic Interchange (OSI) model to OBML format.",
)
async def osi_to_obml(body: ConvertRequest) -> ConvertResponse:
    """Convert OSI YAML → OBML YAML."""
    data = parse_yaml(body.input_yaml)
    mod = get_converter_module()

    # Validate the OSI input against the vendored OSI v0.2 schema before
    # we touch the converter. Advisory by default — the result lands in
    # ``input_validation`` on the response so callers can surface or
    # ignore as they prefer. v0.1.x inputs run through the legacy shim
    # inside ``OSItoOBML.convert`` so schema_errors here may still be
    # spurious for legacy docs; in that case the conversion still runs.
    input_validation = run_validation(mod.validate_osi, data)

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

    validation = run_validation(mod.validate_obml, result)

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
    data = parse_yaml(body.input_yaml)
    mod = get_converter_module()

    # Validate the OBML input against the schema before converting. Advisory —
    # surfaced in ``input_validation`` so an authored ``label:`` (or any other
    # schema violation) is reported rather than silently coerced away, matching
    # the osi-to-obml direction.
    input_validation = run_validation(mod.validate_obml, data)

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

    validation = run_validation(mod.validate_osi, result)

    ontology_yaml: str | None = None
    ontology_validation = None
    if body.include_ontology:
        try:
            onto_conv = mod.OBMLtoOSIOntology(
                data,
                model_name=body.model_name,
                model_description=body.model_description,
                ai_instructions=body.ai_instructions,
            )
            onto = onto_conv.convert()
            warnings = warnings + list(onto_conv.warnings)
        except Exception as exc:
            logger.exception("OBML → OSI ontology conversion failed")
            raise HTTPException(
                status_code=422, detail=f"OBML → OSI ontology conversion failed: {exc}"
            ) from exc
        ontology_yaml = yaml.dump(
            onto, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120
        )
        ontology_validation = run_validation(mod.validate_osi_ontology, onto)

    return ConvertResponse(
        output_yaml=output_yaml,
        warnings=warnings,
        validation=validation,
        input_validation=input_validation,
        ontology_yaml=ontology_yaml,
        ontology_validation=ontology_validation,
    )
