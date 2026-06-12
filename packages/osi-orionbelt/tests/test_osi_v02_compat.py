"""OSI v0.2 compatibility tests.

Covers the v2.6 spec bump from OSI v0.1.1 → v0.2.0.dev0:

- Emitted ``version`` is the v0.2 constant
- Top-level ``dialects`` / ``vendors`` informational arrays are present
- Dataset ``primary_key`` is promoted from per-column ``primaryKey: true``
- Dataset ``unique_keys`` round-trips lossly via OBSL custom_extensions
- Field ``label`` round-trips via OBSL custom_extensions
- Legacy v0.1.1 inputs are normalized in place by the shim
- Every emitted document validates against the vendored v0.2 schema
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import osi_orionbelt.converter as conv

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "osi_orionbelt" / "schemas" / "osi-schema.json"
)


@pytest.fixture(scope="module")
def schema_validator() -> Any:
    """Draft 2020-12 validator pinned to the vendored OSI v0.2 schema."""
    jsonschema = pytest.importorskip("jsonschema")
    with open(_SCHEMA_PATH) as f:
        schema = json.load(f)
    return jsonschema.Draft202012Validator(schema)


_OBML_WITH_PK_AND_LABEL: dict[str, Any] = {
    "version": 1.0,
    "dataObjects": {
        "Orders": {
            "code": "orders",
            "database": "WAREHOUSE",
            "schema": "PUBLIC",
            "columns": {
                "Order ID": {
                    "code": "order_id",
                    "abstractType": "string",
                    "primaryKey": True,
                    "customExtensions": [
                        {
                            "vendor": "OBSL",
                            "data": json.dumps({"obml_field_label": "filter"}),
                        }
                    ],
                },
                "Line Number": {
                    "code": "line_no",
                    "abstractType": "int",
                    "primaryKey": True,
                },
                "Amount": {"code": "amount", "abstractType": "float"},
            },
            "customExtensions": [
                {
                    "vendor": "OBSL",
                    "data": json.dumps(
                        {"obml_unique_keys": [["order_id"], ["order_id", "line_no"]]}
                    ),
                }
            ],
        }
    },
    "measures": {
        "Revenue": {
            "columns": [{"dataObject": "Orders", "column": "Amount"}],
            "resultType": "float",
            "aggregation": "sum",
        }
    },
}


_OSI_V01_INPUT: dict[str, Any] = {
    "version": "0.1.1",
    "semantic_model": [
        {
            "name": "ecommerce",
            "datasets": [
                {
                    "name": "Orders",
                    "source": "WAREHOUSE.PUBLIC.orders",
                    # Legacy: PK stashed in custom_extensions (pre-v0.2 shape)
                    "custom_extensions": [
                        {
                            "vendor_name": "COMMON",
                            "data": json.dumps(
                                {
                                    "obml_primary_key": ["order_id"],
                                    "obml_unique_keys": [["order_id"], ["order_number"]],
                                }
                            ),
                        }
                    ],
                    "fields": [
                        {
                            "name": "order_id",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "order_id"}]
                            },
                            "data_type": "string",
                        },
                        {
                            "name": "amount",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "amount"}]
                            },
                            "data_type": "number",
                        },
                    ],
                }
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Version + top-level shape
# ---------------------------------------------------------------------------


class TestEmittedVersion:
    def test_top_level_version_is_v02(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        assert osi["version"] == conv._OSI_VERSION
        assert osi["version"].startswith("0.2")

    def test_dialects_array_present(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        assert osi["dialects"] == ["ANSI_SQL"]

    def test_vendors_array_present(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        # OBSL appears because the model has obml_field_label customExtensions
        assert "COMMON" in osi["vendors"]
        assert "OBSL" in osi["vendors"]


# ---------------------------------------------------------------------------
# primary_key + unique_keys (first-class in v0.2)
# ---------------------------------------------------------------------------


class TestPrimaryKey:
    def test_composite_pk_emitted_in_declaration_order(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        ds = osi["semantic_model"][0]["datasets"][0]
        # Two columns flagged primaryKey: emit composite in declaration order
        assert ds["primary_key"] == ["order_id", "line_no"]

    def test_pk_roundtrip_restores_per_column_flag(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        obml = conv.OSItoOBML(osi).convert()
        # OSI fields surface as OBML columns keyed by physical code
        # (display names are an OBML-side concept).
        cols = obml["dataObjects"]["Orders"]["columns"]
        assert cols["order_id"].get("primaryKey") is True
        assert cols["line_no"].get("primaryKey") is True
        assert cols["amount"].get("primaryKey") is None or not cols["amount"]["primaryKey"]

    def test_unknown_pk_column_emits_warning(self) -> None:
        bad = {
            "version": "0.2.0.dev0",
            "semantic_model": [
                {
                    "name": "x",
                    "datasets": [
                        {
                            "name": "Orders",
                            "source": "a.b.c",
                            "primary_key": ["no_such_column"],
                            "fields": [
                                {
                                    "name": "amount",
                                    "expression": {
                                        "dialects": [
                                            {"dialect": "ANSI_SQL", "expression": "amount"}
                                        ]
                                    },
                                    "data_type": "number",
                                },
                            ],
                        }
                    ],
                }
            ],
        }
        converter = conv.OSItoOBML(bad)
        converter.convert()
        joined = " ".join(converter.warnings)
        assert "no_such_column" in joined


class TestUniqueKeys:
    def test_unique_keys_roundtrip(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        ds = osi["semantic_model"][0]["datasets"][0]
        assert ds.get("unique_keys") == [["order_id"], ["order_id", "line_no"]]


# ---------------------------------------------------------------------------
# Field label
# ---------------------------------------------------------------------------


class TestFieldLabel:
    def test_label_emitted_from_custom_extensions(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        fields = osi["semantic_model"][0]["datasets"][0]["fields"]
        order_id_field = next(f for f in fields if f["name"] == "order_id")
        assert order_id_field["label"] == "filter"

    def test_label_roundtrip(self) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        obml = conv.OSItoOBML(osi).convert()
        # OSI label round-trips back into OBSL customExtensions
        col = obml["dataObjects"]["Orders"]["columns"]["order_id"]
        exts = col.get("customExtensions", [])
        assert any(
            e.get("vendor") == "OBSL"
            and json.loads(e.get("data", "{}")).get("obml_field_label") == "filter"
            for e in exts
        )


# ---------------------------------------------------------------------------
# Legacy v0.1.x shim
# ---------------------------------------------------------------------------


class TestLegacyShim:
    def test_v01_input_promotes_primary_key(self) -> None:
        # The shim mutates self.osi in place — capture the dataset shape
        # before parsing finishes by snapshotting after _normalize_legacy_v01.
        converter = conv.OSItoOBML(_OSI_V01_INPUT.copy())
        # Manually invoke the shim
        converter.osi = json.loads(json.dumps(_OSI_V01_INPUT))  # deep copy
        converter._normalize_legacy_v01()
        ds = converter.osi["semantic_model"][0]["datasets"][0]
        assert ds["primary_key"] == ["order_id"]
        assert ds["unique_keys"] == [["order_id"], ["order_number"]]

    def test_v01_input_warns(self) -> None:
        converter = conv.OSItoOBML(json.loads(json.dumps(_OSI_V01_INPUT)))
        converter.convert()
        joined = " ".join(converter.warnings)
        assert "0.1.1" in joined and "shim" in joined.lower()

    def test_v02_input_not_normalized(self) -> None:
        # v0.2 input declaring primary_key directly — shim must be a no-op
        v02 = {
            "version": "0.2.0.dev0",
            "semantic_model": [
                {
                    "name": "x",
                    "datasets": [
                        {
                            "name": "Orders",
                            "source": "a.b.c",
                            "primary_key": ["order_id"],
                            "fields": [
                                {
                                    "name": "order_id",
                                    "expression": {
                                        "dialects": [
                                            {"dialect": "ANSI_SQL", "expression": "order_id"}
                                        ]
                                    },
                                    "data_type": "string",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        converter = conv.OSItoOBML(v02)
        converter.convert()
        # No shim warning for v0.2 inputs
        shim_warnings = [w for w in converter.warnings if "shim" in w.lower()]
        assert shim_warnings == []


# ---------------------------------------------------------------------------
# Schema validation (every emitted doc must validate against v0.2)
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_minimal_obml_passes_v02_validation(self, schema_validator: Any) -> None:
        osi = conv.OBMLtoOSI(_OBML_WITH_PK_AND_LABEL).convert()
        errors = list(schema_validator.iter_errors(osi))
        assert errors == [], [e.message for e in errors[:5]]

    def test_v01_via_shim_revalidates_clean(self, schema_validator: Any) -> None:
        obml = conv.OSItoOBML(json.loads(json.dumps(_OSI_V01_INPUT))).convert()
        osi = conv.OBMLtoOSI(obml).convert()
        errors = list(schema_validator.iter_errors(osi))
        assert errors == [], [e.message for e in errors[:5]]

    def test_tpcds_fixture_passes_v02_validation(self, schema_validator: Any) -> None:
        """Real-world TPC-DS OBML model must emit v0.2-clean OSI."""
        yaml = pytest.importorskip("yaml")
        fixture = Path(__file__).resolve().parent / "fixtures" / "tpcds_as_obml.yaml"
        with open(fixture) as f:
            obml = yaml.safe_load(f)
        osi = conv.OBMLtoOSI(obml).convert()
        errors = list(schema_validator.iter_errors(osi))
        assert errors == [], [e.message for e in errors[:5]]


# ---------------------------------------------------------------------------
# MAQL dialect passthrough
# ---------------------------------------------------------------------------


class TestMAQLDialect:
    def test_maql_only_metric_does_not_raise(self) -> None:
        osi_in: dict[str, Any] = {
            "version": "0.2.0.dev0",
            "semantic_model": [
                {
                    "name": "gd",
                    "datasets": [
                        {
                            "name": "Sales",
                            "source": "a.b.sales",
                            "fields": [
                                {
                                    "name": "amount",
                                    "expression": {
                                        "dialects": [
                                            {"dialect": "ANSI_SQL", "expression": "amount"}
                                        ]
                                    },
                                    "data_type": "number",
                                }
                            ],
                        }
                    ],
                    "metrics": [
                        {
                            "name": "total_revenue_maql",
                            "expression": {
                                "dialects": [
                                    {"dialect": "MAQL", "expression": "SELECT SUM(amount)"},
                                ]
                            },
                        }
                    ],
                }
            ],
        }
        # The MAQL dialect is OBSL-unparseable but must not raise; it
        # surfaces as a warning that the MAQL expression couldn't be
        # decomposed.
        converter = conv.OSItoOBML(osi_in)
        converter.convert()
        # No crash is the contract; warnings are acceptable.
