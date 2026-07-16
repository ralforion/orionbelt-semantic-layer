"""JSON Schema ↔ Pydantic model contract (v2.7.6, issue #85).

The hand-maintained JSON Schemas in ``schema/`` exist so external tools
(YAML language servers, JSON Schema validators, IDE autocomplete) can
validate OBML / QueryObject content without spinning up a Python
runtime. Pre-v2.7.6 they had drifted from the Pydantic models in three
ways:

1. ``obml-schema.json``'s root ``additionalProperties: false`` was
   accidentally inside the ``properties`` block — top-level unknown
   keys passed JSON Schema validation but the (v2.7.2) strict Pydantic
   parser rejected them.
2. ``timeGrain`` enum advertised ``year-end`` / ``month-end`` etc. but
   ``TimeGrain`` only ships year / quarter / month / week / day / hour /
   minute / second.
3. Removed fields still advertised: ``dimension.group``,
   ``measure.reduceToRelationDimensionality``. Strict parser rejects.
4. ``query-schema.json`` was missing the ``grouping`` property
   (rollup / cube — shipped in v2.4).

These tests pin the contract direction "if Pydantic accepts X, JSON
Schema must accept X; if Pydantic rejects X, JSON Schema must reject
X". Trip on the next drift instead of waiting for a user to file the
mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema", reason="jsonschema required for contract test")

from orionbelt.models.query import QueryObject  # noqa: E402
from orionbelt.models.semantic import TimeGrain  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_OBML_SCHEMA = _ROOT / "schema" / "obml-schema.json"
_QUERY_SCHEMA = _ROOT / "schema" / "query-schema.json"


@pytest.fixture(scope="module")
def obml_schema():
    return json.loads(_OBML_SCHEMA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def query_schema():
    return json.loads(_QUERY_SCHEMA.read_text(encoding="utf-8"))


# --- 1. Root additionalProperties: false must be at the root ----------------


def test_obml_schema_root_additionalproperties_is_false(obml_schema) -> None:
    """Pre-fix this lived inside ``properties`` and was silently a no-op —
    top-level unknown keys (typos in version, description, etc.) passed
    the JSON Schema but failed the v2.7.2 strict Pydantic parser.
    """
    assert obml_schema.get("additionalProperties") is False, (
        "Root ``additionalProperties: false`` missing from obml-schema.json — "
        "must live at the schema root, not inside ``properties``. See #85."
    )
    # And: must NOT also be a property key
    assert "additionalProperties" not in obml_schema.get("properties", {}), (
        "``additionalProperties`` is a JSON Schema keyword, not a model "
        "property — remove from the ``properties`` block."
    )


def test_obml_schema_rejects_unknown_top_level_key(obml_schema) -> None:
    """End-to-end check: feed a model with a typo top-level key, both
    JSON Schema and Pydantic must reject it.
    """
    bad = {
        "version": 1.0,
        "datObjects": {},  # typo — strict Pydantic catches this in v2.7.2
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, obml_schema)


# --- 2. timeGrain enum must match the TimeGrain Python enum -----------------


def test_timegrain_enum_matches_python_enum(obml_schema) -> None:
    schema_values = set(obml_schema["definitions"]["timeGrain"]["enum"])
    python_values = {g.value for g in TimeGrain}
    assert schema_values == python_values, (
        f"timeGrain JSON Schema enum diverged from TimeGrain Python enum.\n"
        f"  Schema only: {schema_values - python_values}\n"
        f"  Python only: {python_values - schema_values}"
    )


# --- 3. Removed fields must stay removed ------------------------------------


@pytest.mark.parametrize(
    "removed_field",
    [
        "dimension.group",
        "measure.reduceToRelationDimensionality",
        # `label` is not authorable on the analytical types: the identity is the
        # mapping key, which the resolver copies into `label` (like DataObject /
        # Column). Same treatment must hold in the schema. See #221.
        "dimension.label",
        "measure.label",
        "metric.label",
    ],
)
def test_removed_fields_absent_from_schema(obml_schema, removed_field) -> None:
    """Strict parser (v2.7.2) rejects unknown keys; schema must not
    advertise them as legitimate input.
    """
    parent, child = removed_field.split(".")
    defs = obml_schema.get("definitions", {})
    parent_def = defs.get(parent, {})
    props = parent_def.get("properties", {})
    assert child not in props, (
        f"``{removed_field}`` is no longer a model field but the JSON "
        f"Schema still advertises it. Users following the schema get "
        f"``UNKNOWN_PROPERTY`` from the runtime parser. See #85."
    )


@pytest.mark.parametrize("parent", ["dimensions", "measures", "metrics"])
def test_authored_label_fails_schema_validation(parent) -> None:
    """An authored ``label:`` on an analytical type is rejected by schema
    validation rather than silently coerced away. See #221.
    """
    from orionbelt.parser.schema_validation import validate_obml_yaml

    base = {
        "dimensions": {
            "Region": {"dataObject": "Sales", "column": "Region", "resultType": "string"}
        },
        "measures": {
            "Revenue": {
                "columns": [{"dataObject": "Sales", "column": "Amount"}],
                "aggregation": "sum",
            }
        },
        "metrics": {"Margin": {"type": "derived", "expression": "{[Revenue]} * 0.1"}},
    }
    entry = next(iter(base[parent].values()))
    entry["label"] = "Authored Label"
    doc = {
        "version": 1.0,
        "dataObjects": {
            "Sales": {
                "code": "SALES",
                "database": "WH",
                "schema": "PUBLIC",
                "columns": {
                    "Region": {"code": "REGION", "abstractType": "string"},
                    "Amount": {"code": "AMOUNT", "abstractType": "float"},
                },
            }
        },
        **base,
    }
    errors = validate_obml_yaml(yaml.safe_dump(doc))
    assert any(e.code == "SCHEMA_VALIDATION" and "label" in e.message for e in errors), (
        f"authored label on {parent} should fail schema validation, got {errors}"
    )


# --- 4. query-schema.json must expose every QueryObject field ----------------


def test_query_schema_has_grouping(query_schema) -> None:
    """``QueryObject.grouping`` shipped in v2.4 but query-schema.json
    never picked it up — ``WITH ROLLUP`` / ``WITH CUBE`` queries
    validated against the schema would fail.
    """
    props = query_schema.get("properties", {})
    assert "grouping" in props, "query-schema.json missing the ``grouping`` property. See #85."
    assert props["grouping"].get("enum") == ["rollup", "cube"], (
        "``grouping`` enum must match the QueryObject Grouping enum."
    )


def test_query_schema_accepts_grouping_payload(query_schema) -> None:
    payload = {
        "select": {"dimensions": ["Country"], "measures": ["Revenue"]},
        "grouping": "rollup",
    }
    jsonschema.validate(payload, query_schema)
    # And Pydantic agrees
    q = QueryObject.model_validate(payload)
    assert q.grouping == "rollup"


# --- 5. Round-trip: a minimal valid model must validate both ways -----------


def test_minimal_model_validates_under_both(obml_schema) -> None:
    """The smallest legal OBML model. JSON Schema accepts, Pydantic
    resolver accepts. Sanity for the schema's required-field rules.
    """
    minimal = {
        "version": 1.0,
        "dataObjects": {
            "Orders": {
                "code": "orders",
                "columns": {
                    "ID": {"code": "ID", "abstractType": "string"},
                    "Amount": {
                        "code": "AMT",
                        "abstractType": "float",
                        "numClass": "additive",
                    },
                },
            }
        },
        "dimensions": {
            "Order ID": {
                "dataObject": "Orders",
                "column": "ID",
                "resultType": "string",
            }
        },
        "measures": {
            "Revenue": {
                "columns": [{"dataObject": "Orders", "column": "Amount"}],
                "aggregation": "sum",
                "resultType": "float",
            }
        },
    }
    jsonschema.validate(minimal, obml_schema)
    raw, sm = TrackedLoader().load_string(json.dumps(minimal))
    _model, vr = ReferenceResolver().resolve(raw, sm)
    assert vr.valid, vr.errors
