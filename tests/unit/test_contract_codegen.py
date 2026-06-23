"""Read-only OBML-contract code generation + drift check (Phase 6, read-only slice).

This is the *generate-and-compare* slice of Phase 6: a deterministic
generator derives a subset of the dependent artifacts from the canonical
manifest ``schema/obml-contract.yml``, and tests assert the current
hand-maintained artifacts are consistent with what the manifest would
generate. It deliberately does NOT check in generated files or let them
*own* the schema/docs/ontology yet — that (Phase 6 "generated artifacts")
waits until the manifest has survived a real OBML change. For now the
generator proves the manifest can reproduce the artifacts and flags drift.

The generator functions live here (test-only) so they can be promoted to a
shipped ``uv run`` codegen command in the full Phase 6 without rework.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "schema" / "obml-contract.yml"
SCHEMA_PATH = REPO_ROOT / "schema" / "obml-schema.json"


def _load_manifest() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))


def _load_schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _camel(name: str) -> str:
    """PascalCase enum/class name -> camelCase JSON-schema definition name."""
    return name[:1].lower() + name[1:]


# ── generator (deterministic; pure functions of the manifest) ───────────────


def generate_enum_definitions(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """JSON-schema ``definitions`` fragments for every manifest enum.

    Keyed by the camelCase definition name the schema uses.
    """
    return {
        _camel(name): {"type": "string", "enum": list(info["values"])}
        for name, info in sorted(manifest["enums"].items())
    }


def generate_reference_markdown(manifest: dict[str, Any]) -> str:
    """A deterministic OBML field-reference table derived from the manifest."""
    lines = ["| Class | Field | Alias | Type |", "| --- | --- | --- | --- |"]
    for class_name in sorted(manifest["classes"]):
        fields = manifest["classes"][class_name].get("fields") or {}
        for field_name in sorted(fields):
            meta = fields[field_name]
            lines.append(
                f"| {class_name} | {field_name} | {meta['alias']} | {meta.get('type', '')} |"
            )
    return "\n".join(lines) + "\n"


# ── schema navigation: resolve a field's enum (inline OR via $ref) ──────────


def _class_property_nodes(schema: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Property schema nodes for a manifest class entry (merging allOf/$ref)."""
    defs = schema["definitions"]
    if entry.get("json_schema_root"):
        root: Any = schema
    elif "json_schema_def" in entry:
        root = defs[entry["json_schema_def"]]
    elif "json_schema_pointer" in entry:
        root = schema
        for key in entry["json_schema_pointer"].split("/"):
            root = root[key]
    else:
        return {}

    props: dict[str, Any] = {}

    def collect(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            collect(defs.get(node["$ref"].split("/")[-1], {}))
            return
        props.update(node.get("properties") or {})
        for key in ("allOf", "anyOf", "oneOf"):
            for sub in node.get(key, []):
                collect(sub)

    collect(root)
    return props


def _property_enum(schema: dict[str, Any], node: Any) -> set[str] | None:
    """Enum value set declared by a property node — inline, via ``$ref`` to a
    named definition, or inside an ``anyOf`` (e.g. optional ``[enum, null]``)."""
    if not isinstance(node, dict):
        return None
    if "enum" in node:
        return set(node["enum"])
    if "$ref" in node:
        return _property_enum(schema, schema["definitions"].get(node["$ref"].split("/")[-1], {}))
    for key in ("anyOf", "allOf", "oneOf"):
        for sub in node.get(key, []):
            found = _property_enum(schema, sub)
            if found is not None:
                return found
    return None


def _enum_in_type(type_str: str, enum_names: Iterable[str]) -> str | None:
    for name in enum_names:
        if re.search(rf"\b{re.escape(name)}\b", type_str):
            return name
    return None


# ── generate-and-compare drift checks ───────────────────────────────────────

# Enums that are NOT part of the OBML YAML contract, so no schema-facing field
# carries them. ``JoinType`` (left/inner/right/full) is the SQL join direction
# used internally by the AST/compiler — ``DataObjectJoin.joinType`` is a
# ``Cardinality``, not a ``JoinType``.
_NON_SCHEMA_ENUMS = {"JoinType"}


def test_schema_enum_fields_match_manifest() -> None:
    """Every enum-typed OBML field must declare, in the schema, exactly the
    manifest's value set — whether the schema models it inline or via a named
    ``$ref``. Resolving through the field (not just named definitions) means an
    inline enum drift (e.g. dropping ``measure`` from ``aggregation``) is caught.
    Compared as sets (the schema may order members differently).
    """
    manifest = _load_manifest()
    schema = _load_schema()
    enum_values = {name: set(info["values"]) for name, info in manifest["enums"].items()}

    covered: set[str] = set()
    for entry in manifest["classes"].values():
        props = _class_property_nodes(schema, entry)
        if not props:
            continue
        for meta in (entry.get("fields") or {}).values():
            if not meta.get("json_schema"):
                continue
            enum_name = _enum_in_type(meta.get("type", ""), enum_values)
            if enum_name is None:
                continue
            found = _property_enum(schema, props.get(meta["alias"]))
            assert found is not None, (
                f"field alias {meta['alias']!r} is enum-typed ({enum_name}) but its "
                f"schema property declares no enum"
            )
            assert found == enum_values[enum_name], (
                f"enum {enum_name} via field {meta['alias']!r}: manifest "
                f"{sorted(enum_values[enum_name])} != schema {sorted(found)}"
            )
            covered.add(enum_name)

    # Every schema-facing enum must actually be validated by at least one field,
    # so the gate can't silently stop covering one.
    expected = set(enum_values) - _NON_SCHEMA_ENUMS
    assert expected <= covered, f"schema-facing enums not validated: {sorted(expected - covered)}"


def test_generator_is_deterministic() -> None:
    manifest = _load_manifest()
    assert generate_enum_definitions(manifest) == generate_enum_definitions(manifest)
    assert generate_reference_markdown(manifest) == generate_reference_markdown(manifest)


def test_reference_markdown_covers_all_classes() -> None:
    manifest = _load_manifest()
    table = generate_reference_markdown(manifest)
    for class_name in manifest["classes"]:
        assert f"| {class_name} |" in table, f"{class_name} missing from reference table"
    # Well-formed header + at least one row per class.
    assert table.startswith("| Class | Field | Alias | Type |\n")
