"""Validation helpers for OBML / OSI documents.

Extracted verbatim from ``converter.py``. Schema files are vendored beside the
package under ``schemas/``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_SCHEMAS_DIR = _SCRIPT_DIR / "schemas"

# The OBML schema is OrionBelt's own format, always vendored beside the package
# and kept in sync with the repo-root schema/obml-schema.json by a drift-guard
# test.
_OBML_SCHEMA_PATH = _SCHEMAS_DIR / "obml-schema.json"


def _osi_schema_path(filename: str) -> Path:
    """Resolve an OSI core-spec schema without forcing a duplicate copy where a
    canonical one already exists.

    Resolution order:
    1. A copy vendored beside this package (``schemas/<filename>``). The
       standalone PyPI wheel and the OrionBelt product API rely on this, so the
       package validates OSI documents self-contained and offline.
    2. Otherwise ``core-spec/<filename>`` in an enclosing Ossie monorepo
       checkout, found by walking up from this file. This lets the in-tree
       converter drop its vendored copy and link the single canonical schema
       rather than duplicating it.

    Returns the vendored path unchanged when neither exists, so downstream
    ``.exists()`` checks degrade to skip-with-warning rather than raising.
    """
    vendored = _SCHEMAS_DIR / filename
    if vendored.exists():
        return vendored
    for parent in _SCRIPT_DIR.parents:
        candidate = parent / "core-spec" / filename
        if candidate.exists():
            return candidate
    return vendored


_OSI_SCHEMA_PATH = _osi_schema_path("osi-schema.json")


class ValidationResult:
    """Collects schema errors, semantic errors, and warnings."""

    def __init__(self, format_name: str = "OBML") -> None:
        self.format_name = format_name
        # False when jsonschema or the schema file is unavailable: ``valid`` then
        # covers only the semantic checks, not required fields or types.
        self.schema_validation_performed = False
        self.schema_errors: list[str] = []
        self.semantic_errors: list[str] = []
        self.semantic_warnings: list[str] = []

    @property
    def valid(self) -> bool:
        """Whether the checks that ran found no errors; a skipped schema check is not a pass."""
        return not self.schema_errors and not self.semantic_errors

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        if self.schema_errors:
            lines.append(f"  JSON Schema: {len(self.schema_errors)} error(s)")
            for e in self.schema_errors:
                lines.append(f"    - {e}")
        elif self.schema_validation_performed:
            lines.append("  JSON Schema: ✓ valid")
        else:
            lines.append("  JSON Schema: skipped")
        if self.semantic_errors:
            lines.append(f"  Semantic:    {len(self.semantic_errors)} error(s)")
            for e in self.semantic_errors:
                lines.append(f"    - {e}")
        else:
            lines.append("  Semantic:    ✓ valid")
        if self.semantic_warnings:
            lines.append(f"  Warnings:    {len(self.semantic_warnings)}")
            for w in self.semantic_warnings:
                lines.append(f"    - {w}")
        return lines


def _validate_json_schema(
    data: dict[str, Any],
    schema_path: Path,
    result: ValidationResult,
    draft: str = "draft7",
    registry: Any | None = None,
) -> None:
    """Run JSON Schema validation, appending errors to *result*.

    *registry* is an optional ``referencing.Registry`` used to resolve external
    ``$ref`` URIs against local resources (avoids network fetches).
    """
    try:
        import jsonschema
    except ImportError:
        result.semantic_warnings.append(
            "jsonschema package not installed — skipping JSON Schema validation"
        )
        return

    if not schema_path.exists():
        result.semantic_warnings.append(
            f"Schema file not found at {schema_path} — skipping JSON Schema validation"
        )
        return

    with open(schema_path) as f:
        schema = json.load(f)

    validator_cls = (
        jsonschema.Draft202012Validator if draft == "draft2020" else jsonschema.Draft7Validator
    )
    validator = validator_cls(schema, registry=registry) if registry else validator_cls(schema)
    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        result.schema_errors.append(f"[{path}] {error.message}")
    result.schema_validation_performed = True


# ── OBML Validation ──────────────────────────────────────────────────────


def validate_obml(obml_dict: dict[str, Any], schema_path: Path | None = None) -> ValidationResult:
    """Validate an OBML dict against JSON Schema and semantic rules.

    Runs two layers of validation:
    1. **JSON Schema** — structural correctness (types, required fields,
       allowed properties) against ``schema/obml-schema.json``
    2. **Semantic** — reference integrity, cycle detection, duplicate
       identifiers via OrionBelt's ``ReferenceResolver`` + ``SemanticValidator``

    Both layers are optional — if ``jsonschema`` or ``orionbelt`` packages are
    not installed the corresponding checks are skipped with a warning.
    """
    result = ValidationResult("OBML")

    # 1. JSON Schema validation
    _validate_json_schema(obml_dict, schema_path or _OBML_SCHEMA_PATH, result, draft="draft7")

    # 2. Semantic validation (ReferenceResolver + SemanticValidator)
    try:
        from orionbelt.parser.resolver import ReferenceResolver
        from orionbelt.parser.validator import SemanticValidator
    except ImportError:
        result.semantic_warnings.append(
            "orionbelt package not installed — skipping semantic validation"
        )
    else:
        resolver = ReferenceResolver()
        model, resolve_result = resolver.resolve(obml_dict)
        if not resolve_result.valid:
            for err in resolve_result.errors:
                path_info = f" (at {err.path})" if err.path else ""
                suggestions = ""
                if err.suggestions:
                    suggestions = f" Did you mean: {', '.join(err.suggestions)}?"
                result.semantic_errors.append(f"[{err.code}] {err.message}{path_info}{suggestions}")
        for warn in resolve_result.warnings:
            result.semantic_warnings.append(f"[{warn.code}] {warn.message}")

        # Run SemanticValidator only if resolution produced a usable model.
        # It returns advisories alongside errors — a call to a function outside
        # the portable catalog, a query time zone that cannot reach an
        # undeclared column — so severity decides which list they land in.
        # Flattening them all into errors reported a model invalid that the
        # semantic layer itself accepts.
        if resolve_result.valid:
            sem_validator = SemanticValidator()
            for reported in sem_validator.validate(model):
                path_info = f" (at {reported.path})" if reported.path else ""
                entry = f"[{reported.code}] {reported.message}{path_info}"
                if reported.severity == "warning":
                    result.semantic_warnings.append(entry)
                else:
                    result.semantic_errors.append(entry)

    return result


# ── OSI Validation ───────────────────────────────────────────────────────


def _duplicates(names: Iterable[str | None]) -> list[str]:
    """The names that occur more than once, each once, in first-appearance order.

    A name repeated three times is one problem, not two. ``None`` stands for a
    missing or non-string name, which is a schema error, and is skipped.
    """
    counts = Counter(name for name in names if name is not None)
    return [name for name, count in counts.items() if count > 1]


def validate_osi(osi_dict: dict[str, Any], schema_path: Path | None = None) -> ValidationResult:
    """Validate an OSI dict against JSON Schema and semantic rules.

    Runs three layers of validation (mirroring OSI's own ``validate.py``):
    1. **JSON Schema** — structural correctness against ``osi-schema.json``
       (Draft 2020-12)
    2. **Unique names** — datasets, fields, metrics, relationships
    3. **References** — relationship from/to reference existing datasets

    Without ``jsonschema`` or the schema file the schema layer is skipped with
    a warning and ``schema_validation_performed`` is False; the semantic checks
    still run but do not check required fields or types, so require both that
    flag and ``valid`` for structural validity.
    """
    result = ValidationResult("OSI")

    # 1. JSON Schema validation (OSI uses Draft 2020-12)
    _validate_json_schema(osi_dict, schema_path or _OSI_SCHEMA_PATH, result, draft="draft2020")

    if not isinstance(osi_dict, dict):
        result.semantic_errors.append("[INVALID_DOCUMENT] An OSI document must be an object")
        return result

    # The semantic checks below assume a well-formed structure (lists of dicts).
    # Guard every level rather than raising on malformed input, even when the
    # schema layer was skipped.
    def _as_dict_list(value: Any) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    # An identifier that is not a non-empty string is a schema error already;
    # the checks below skip it rather than compare or hash it.
    def _ident(obj: dict[str, Any], key: str = "name") -> str | None:
        value = obj.get(key)
        return value if isinstance(value, str) and value else None

    # A current document is one model at the root. The earlier
    # ``semantic_model`` array is a schema error, but its models are still
    # checked so a legacy document reports every problem at once.
    if "semantic_model" in osi_dict:
        if not result.schema_validation_performed:
            result.semantic_warnings.append(
                "[LEGACY_WRAPPER] The model is wrapped in a 'semantic_model' array; "
                "Apache Ossie puts the model properties at the document root"
            )
        models = _as_dict_list(osi_dict.get("semantic_model"))
    else:
        models = [osi_dict]

    # 2. Unique name checks
    for model in models:
        model_name = _ident(model) or "<unnamed>"
        datasets = _as_dict_list(model.get("datasets", []))

        # Unique dataset names
        for name in _duplicates(_ident(ds) for ds in datasets):
            result.semantic_errors.append(
                f"[DUPLICATE_DATASET] Duplicate dataset name '{name}' in model '{model_name}'"
            )

        # Unique field names within each dataset
        for ds in datasets:
            ds_name = _ident(ds) or "<unnamed>"
            fields = _as_dict_list(ds.get("fields", []))
            for fname in _duplicates(_ident(field) for field in fields):
                result.semantic_errors.append(
                    f"[DUPLICATE_FIELD] Duplicate field name '{fname}' in dataset '{ds_name}'"
                )

        # Unique metric names
        metrics = _as_dict_list(model.get("metrics", []))
        for mname in _duplicates(_ident(m) for m in metrics):
            result.semantic_errors.append(
                f"[DUPLICATE_METRIC] Duplicate metric name '{mname}' in model '{model_name}'"
            )

        # Unique relationship names
        relationships = _as_dict_list(model.get("relationships", []))
        for rname in _duplicates(_ident(r) for r in relationships):
            result.semantic_errors.append(
                f"[DUPLICATE_RELATIONSHIP] Duplicate relationship name "
                f"'{rname}' in model '{model_name}'"
            )

    # 3. Reference checks — relationships reference existing datasets
    for model in models:
        datasets = _as_dict_list(model.get("datasets", []))
        ds_name_set = {ident for ds in datasets if (ident := _ident(ds)) is not None}
        for rel in _as_dict_list(model.get("relationships", [])):
            rel_name = _ident(rel) or "<unnamed>"
            from_ds = _ident(rel, "from")
            to_ds = _ident(rel, "to")
            if from_ds and from_ds not in ds_name_set:
                result.semantic_errors.append(
                    f"[UNKNOWN_DATASET_REF] Relationship '{rel_name}' "
                    f"references unknown dataset '{from_ds}'"
                )
            if to_ds and to_ds not in ds_name_set:
                result.semantic_errors.append(
                    f"[UNKNOWN_DATASET_REF] Relationship '{rel_name}' "
                    f"references unknown dataset '{to_ds}'"
                )

    return result
