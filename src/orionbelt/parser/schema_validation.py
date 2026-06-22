"""JSON Schema validation for OBML models and query payloads.

The JSON Schemas in ``schema/`` are the published, language-agnostic
contract for OBML model documents and ``QueryObject`` payloads. Validating
raw input against them *first* — before Pydantic parsing — makes the schema
a load-bearing gate rather than a published-but-unused artifact: every real
document exercises it, so the schema is continuously proven correct, and
external consumers can rely on the same contract the engine enforces.

This is the single place that loads and runs those schemas. The schema
files ship inside the wheel as package data under ``orionbelt/schema/``
(see ``force-include`` in ``pyproject.toml``); source checkouts fall back
to the repo-root ``schema/`` directory.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from orionbelt.models.errors import SemanticError

SCHEMA_VALIDATION_CODE = "SCHEMA_VALIDATION"

_SCHEMA_FILES: dict[str, str] = {
    "obml": "obml-schema.json",
    "query": "query-schema.json",
}


def read_schema_text(filename: str) -> str | None:
    """Return the contents of a JSON Schema file, or ``None`` if not found.

    Resolves via :mod:`importlib.resources` for installed wheels, falling
    back to the repo-root ``schema/`` directory for editable/source
    checkouts (``parents[3]`` from ``src/orionbelt/parser/``).
    """
    try:
        resource = resource_files("orionbelt") / "schema" / filename
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        pass

    src_path = Path(__file__).resolve().parents[3] / "schema" / filename
    if src_path.is_file():
        return src_path.read_text(encoding="utf-8")
    return None


@cache
def _validator(name: str) -> Any:
    """Build (and cache) the JSON Schema validator for ``name``.

    Returns ``None`` when the schema file is absent from the deployment, so
    validation degrades to a no-op rather than blocking model loading.
    """
    text = read_schema_text(_SCHEMA_FILES[name])
    if text is None:
        return None
    schema = json.loads(text)
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def _format_path(error: jsonschema.ValidationError) -> str:
    parts = [str(p) for p in error.absolute_path]
    return ".".join(parts) if parts else "(root)"


def _validate(name: str, document: object) -> list[SemanticError]:
    validator = _validator(name)
    if validator is None:  # pragma: no cover - only if schema is unpackaged
        return []
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    return [
        SemanticError(
            code=SCHEMA_VALIDATION_CODE,
            message=error.message,
            path=_format_path(error),
            severity="error",
        )
        for error in errors
    ]


def validate_obml_document(document: object) -> list[SemanticError]:
    """Validate a raw OBML model document against ``obml-schema.json``."""
    return _validate("obml", document)


def validate_query_document(document: object) -> list[SemanticError]:
    """Validate a raw ``QueryObject`` payload against ``query-schema.json``."""
    return _validate("query", document)


def _json_native(obj: object) -> object:
    """Coerce a YAML-parsed structure to JSON-native types.

    ruamel/PyYAML parse ISO dates and timestamps into Python ``date`` /
    ``datetime`` objects, which JSON Schema cannot validate. A JSON
    round-trip (``default=str``) renders them as strings, matching how the
    same document is transmitted as JSON.
    """
    return json.loads(json.dumps(obj, default=str))


def validate_obml_yaml(text: str) -> list[SemanticError]:
    """Validate an OBML model YAML string against ``obml-schema.json``.

    Returns no schema errors when the text is not parseable YAML or not a
    mapping; the normal loader reports those with precise source positions.
    Merger-injected private keys (``_extends_sources`` etc.) are stripped.
    """
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(document, dict):
        return []
    public = {k: v for k, v in document.items() if not str(k).startswith("_")}
    return validate_obml_document(_json_native(public))
