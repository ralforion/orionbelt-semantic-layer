"""Regression guards for OBML model <-> JSON-schema alignment.

These pin the v2.9 schema fixes: top-level ``name`` is accepted, the vestigial
``locale`` object and ``measure.functions`` are gone, measure filters are
trimmed to the implemented surface, and ``settings.defaultLocale`` (BCP-47)
exists in both the model and the schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from orionbelt.models.semantic import ModelSettings

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schema" / "obml-schema.json").read_text()
)
_V = jsonschema.Draft7Validator(_SCHEMA)

_BASE = {
    "version": 1.0,
    "dataObjects": {
        "O": {
            "code": "O",
            "database": "d",
            "schema": "s",
            "columns": {"C": {"code": "C", "abstractType": "string"}},
        }
    },
}


def _errors(extra: dict) -> list[str]:
    doc = {**_BASE, **extra}
    return [e.message for e in _V.iter_errors(doc)]


class TestSchemaAcceptsImplementedFields:
    def test_name_accepted(self) -> None:
        assert _errors({"name": "sales_model"}) == []

    def test_settings_default_locale_accepted(self) -> None:
        assert _errors({"settings": {"defaultLocale": "de-DE"}}) == []


class TestSchemaRejectsRemovedFields:
    def test_root_locale_rejected(self) -> None:
        # The vestigial top-level ``locale`` object was removed.
        assert _errors({"locale": {"timezone": "UTC"}})

    def test_measure_functions_rejected(self) -> None:
        assert _errors(
            {
                "measures": {
                    "M": {
                        "aggregation": "sum",
                        "columns": [{"dataObject": "O", "column": "C"}],
                        "functions": ["x"],
                    }
                }
            }
        )

    def test_locale_definition_removed(self) -> None:
        assert "locale" not in _SCHEMA["definitions"]
        assert "locale" not in _SCHEMA["properties"]

    def test_measure_filter_dynamic_dates_removed(self) -> None:
        filter_props = _SCHEMA["definitions"]["filter"]["properties"]
        assert set(filter_props) == {"column", "operator", "values"}

    def test_parameter_value_trimmed_to_model(self) -> None:
        pv = set(_SCHEMA["definitions"]["parameterValue"]["properties"])
        assert pv == {
            "dataType",
            "isNull",
            "valueString",
            "valueInt",
            "valueFloat",
            "valueDate",
            "valueBoolean",
        }


class TestModelSettingsDefaultLocale:
    def test_valid_bcp47_accepted(self) -> None:
        assert ModelSettings(defaultLocale="de-DE").default_locale == "de-DE"
        assert ModelSettings(defaultLocale="en").default_locale == "en"

    def test_malformed_locale_rejected(self) -> None:
        with pytest.raises(ValueError):
            ModelSettings(defaultLocale="!!bad")

    def test_settings_props_match_schema(self) -> None:
        model_aliases = {(f.alias or n) for n, f in ModelSettings.model_fields.items()}
        schema_props = set(_SCHEMA["properties"]["settings"]["properties"])
        assert model_aliases == schema_props
