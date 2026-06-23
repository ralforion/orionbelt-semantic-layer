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
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "schema" / "obml-contract.yml"
SCHEMA_PATH = REPO_ROOT / "schema" / "obml-schema.json"


def _load_manifest() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))


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


# ── generate-and-compare drift checks ───────────────────────────────────────


def test_generated_enums_match_schema_definitions() -> None:
    """Every enum the manifest can generate AND the schema names must agree.

    Compared as value *sets* (the schema lists members in a different order),
    so this catches a member added/removed on one side but not the other.
    """
    manifest = _load_manifest()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema_defs = schema["definitions"]
    generated = generate_enum_definitions(manifest)

    compared = []
    for name, definition in generated.items():
        schema_def = schema_defs.get(name)
        if not isinstance(schema_def, dict) or "enum" not in schema_def:
            continue  # enum has no named schema definition (inline / absent)
        compared.append(name)
        assert set(definition["enum"]) == set(schema_def["enum"]), (
            f"enum '{name}': manifest {sorted(definition['enum'])} != "
            f"schema {sorted(schema_def['enum'])}"
        )
    # Guard: the mapping must actually exercise a meaningful subset, else a
    # rename would make this test silently vacuous.
    assert len(compared) >= 9, f"only {len(compared)} enums compared: {compared}"


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
