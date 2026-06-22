"""OBML contract drift gate (Phase 2).

``schema/obml-contract.yml`` is the single, hand-maintained manifest of the
OBML field surface. These tests keep it honest against the three artifacts
it summarizes:

* **Pydantic models** (``orionbelt.models.semantic``) — every modeled OBML
  field and enum must appear in the manifest (or an explicit exclusion),
  and the manifest must not list fields that no longer exist.
* **JSON schema** (``schema/obml-schema.json``) — every field the manifest
  marks ``json_schema: true`` must resolve to a property in the schema.
* **Ontology** (``ontology/obsl.ttl``) — every ``ontology_property`` /
  ``ontology_class`` the manifest declares must exist in the ontology.

Adding an OBML field on the Pydantic models without updating the manifest
fails ``test_every_pydantic_field_is_in_manifest``. The pre-existing schema
and OSI drift tests remain the authority on those artifacts' internal
integrity; this gate only checks the manifest's cross-references.
"""

from __future__ import annotations

import enum
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import BaseModel

import orionbelt.models.semantic as semantic

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "schema" / "obml-contract.yml"
SCHEMA_PATH = REPO_ROOT / "schema" / "obml-schema.json"
ONTOLOGY_PATH = REPO_ROOT / "ontology" / "obsl.ttl"

# Pydantic fields intentionally absent from the manifest, keyed by class
# name. Empty today; every field is currently modeled. Add entries here
# (with a comment) if a field should never be part of the OBML contract.
MANIFEST_FIELD_EXCLUSIONS: dict[str, set[str]] = {}

# JSON-schema properties that intentionally have no matching manifest field
# alias. The schema is camelCase-only; the only remaining exceptions are the
# two top-level merge keys, which the parser consumes (they map to the
# private extends_sources / inherits_source model fields).
SCHEMA_ONLY_PROPERTIES: dict[str, set[str]] = {
    "SemanticModel": {"extends", "inherits"},
}


# ─────────────────────────── fixtures / loaders ────────────────────────────


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _obml_model_classes() -> dict[str, type[BaseModel]]:
    return {
        name: obj
        for name, obj in vars(semantic).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj.__module__ == semantic.__name__
    }


def _obml_enums() -> dict[str, type[enum.Enum]]:
    return {
        name: obj
        for name, obj in vars(semantic).items()
        if isinstance(obj, type)
        and issubclass(obj, enum.Enum)
        and obj.__module__ == semantic.__name__
    }


def _schema_property_names(schema: dict[str, Any], node: Any) -> set[str]:
    """Resolve the property aliases reachable from a schema node.

    Follows ``$ref`` into ``definitions`` and merges ``allOf`` / ``anyOf`` /
    ``oneOf`` branches, matching how the schema composes object shapes.
    """
    defs = schema["definitions"]
    out: set[str] = set()
    if not isinstance(node, dict):
        return out
    if "$ref" in node:
        return _schema_property_names(schema, defs[node["$ref"].split("/")[-1]])
    out |= set((node.get("properties") or {}).keys())
    for key in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(key, []):
            out |= _schema_property_names(schema, sub)
    return out


def _resolve_pointer(schema: dict[str, Any], pointer: str) -> Any:
    node: Any = schema
    for key in pointer.split("/"):
        node = node[key]
    return node


def _schema_props_for_class(schema: dict[str, Any], entry: dict[str, Any]) -> set[str] | None:
    """Property aliases the schema exposes for a manifest class entry."""
    if entry.get("json_schema_root"):
        return set((schema.get("properties") or {}).keys())
    if "json_schema_def" in entry:
        return _schema_property_names(schema, schema["definitions"][entry["json_schema_def"]])
    if "json_schema_pointer" in entry:
        node = _resolve_pointer(schema, entry["json_schema_pointer"])
        return _schema_property_names(schema, node)
    return None


def _ontology_text() -> str:
    return ONTOLOGY_PATH.read_text(encoding="utf-8")


def _ontology_names(kind: str) -> set[str]:
    """Local names declared in the ontology for the given OWL ``kind``."""
    pattern = re.compile(rf"obsl:([A-Za-z_]+)\s+a\s+owl:{kind}\b")
    return set(pattern.findall(_ontology_text()))


def _ontology_property_names() -> set[str]:
    return _ontology_names("DatatypeProperty") | _ontology_names("ObjectProperty")


# ─────────────────────────── 2.2 Pydantic ↔ manifest ───────────────────────


def test_every_obml_class_is_in_manifest(manifest: dict[str, Any]) -> None:
    classes = manifest["classes"]
    for name in _obml_model_classes():
        assert name in classes, f"OBML class {name!r} missing from {MANIFEST_PATH.name}"


def test_manifest_has_no_unknown_classes(manifest: dict[str, Any]) -> None:
    known = set(_obml_model_classes())
    for name in manifest["classes"]:
        assert name in known, f"manifest lists class {name!r} that no longer exists"


def test_every_pydantic_field_is_in_manifest(manifest: dict[str, Any]) -> None:
    classes = manifest["classes"]
    for name, model in _obml_model_classes().items():
        listed = set(classes[name].get("fields") or {})
        excluded = MANIFEST_FIELD_EXCLUSIONS.get(name, set())
        for field_name in model.model_fields:
            assert field_name in listed or field_name in excluded, (
                f"{name}.{field_name} is not in the manifest. Add it to "
                f"schema/obml-contract.yml (or MANIFEST_FIELD_EXCLUSIONS)."
            )


def test_manifest_has_no_stale_fields(manifest: dict[str, Any]) -> None:
    classes = manifest["classes"]
    for name, model in _obml_model_classes().items():
        listed = set(classes[name].get("fields") or {})
        for field_name in listed:
            assert field_name in model.model_fields, (
                f"manifest lists {name}.{field_name} which no longer exists on the model"
            )


def test_manifest_field_aliases_match_models(manifest: dict[str, Any]) -> None:
    classes = manifest["classes"]
    for name, model in _obml_model_classes().items():
        fields = classes[name].get("fields") or {}
        for field_name, meta in fields.items():
            expected = model.model_fields[field_name].alias or field_name
            assert meta["alias"] == expected, (
                f"{name}.{field_name}: manifest alias {meta['alias']!r} != model alias {expected!r}"
            )


def test_every_enum_is_in_manifest(manifest: dict[str, Any]) -> None:
    enums = manifest["enums"]
    declared = _obml_enums()
    assert set(enums) == set(declared), (
        f"manifest enums {sorted(enums)} != model enums {sorted(declared)}"
    )
    for name, enum_cls in declared.items():
        assert enums[name]["values"] == [e.value for e in enum_cls], (
            f"enum {name} values drifted from the model"
        )


# ─────────────────────────── 2.3 manifest ↔ JSON schema ────────────────────


def test_json_schema_fields_exist_in_schema(
    manifest: dict[str, Any], schema: dict[str, Any]
) -> None:
    for name, entry in manifest["classes"].items():
        props = _schema_props_for_class(schema, entry)
        if props is None:
            continue
        for field_name, meta in (entry.get("fields") or {}).items():
            if meta.get("json_schema"):
                assert meta["alias"] in props, (
                    f"{name}.{field_name} is marked json_schema: true but alias "
                    f"{meta['alias']!r} is not in the schema definition"
                )


def test_schema_properties_are_covered_by_manifest(
    manifest: dict[str, Any], schema: dict[str, Any]
) -> None:
    """Every schema property of a mapped class is a manifest field alias.

    Catches a property added to the JSON schema without a matching manifest
    entry (the other drift direction).
    """
    for name, entry in manifest["classes"].items():
        props = _schema_props_for_class(schema, entry)
        if props is None:
            continue
        manifest_aliases = {meta["alias"] for meta in (entry.get("fields") or {}).values()}
        missing = props - manifest_aliases - SCHEMA_ONLY_PROPERTIES.get(name, set())
        assert not missing, f"{name}: schema properties not in manifest: {sorted(missing)}"


# ─────────────────────────── 2.4 manifest ↔ ontology ───────────────────────


def test_declared_ontology_properties_exist(manifest: dict[str, Any]) -> None:
    available = _ontology_property_names()
    for name, entry in manifest["classes"].items():
        for field_name, meta in (entry.get("fields") or {}).items():
            prop = meta.get("ontology_property")
            if prop:
                assert prop in available, (
                    f"{name}.{field_name}: ontology_property {prop!r} is not declared in obsl.ttl"
                )


def test_declared_ontology_classes_exist(manifest: dict[str, Any]) -> None:
    available = _ontology_names("Class")
    for name, entry in manifest["classes"].items():
        ont_class = entry.get("ontology_class")
        if ont_class:
            assert ont_class in available, (
                f"{name}: ontology_class {ont_class!r} is not declared in obsl.ttl"
            )
