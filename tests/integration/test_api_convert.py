"""Integration tests for the /v1/convert endpoints."""

from __future__ import annotations

import json

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import init_session_manager, reset_session_manager
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings


@pytest.fixture
def app():
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        max_age_seconds=settings.session_max_age_seconds,
        max_sessions=settings.max_sessions,
        max_models_per_session=settings.max_models_per_session,
        cleanup_interval=settings.session_cleanup_interval,
    )
    init_session_manager(mgr)
    yield app
    reset_session_manager()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Fixtures: minimal OSI v0.2 and v0.1.1 inputs
# ---------------------------------------------------------------------------


_OSI_V02_MINIMAL = {
    "version": "0.2.0.dev0",
    "semantic_model": [
        {
            "name": "demo",
            "datasets": [
                {
                    "name": "Orders",
                    "source": "WAREHOUSE.PUBLIC.orders",
                    "primary_key": ["order_id"],
                    "fields": [
                        {
                            "name": "order_id",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "order_id"}]
                            },
                        },
                        {
                            "name": "amount",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "amount"}]
                            },
                        },
                    ],
                }
            ],
            "metrics": [
                {
                    "name": "total_revenue",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(Orders.amount)"}]
                    },
                }
            ],
        }
    ],
}


# A document that's structurally wrong for the OSI schema: ``datasets`` set to
# a scalar instead of an array. Used to assert input_validation surfaces the
# schema error rather than crashing.
_OSI_MALFORMED = {
    "version": "0.2.0.dev0",
    "semantic_model": [
        {
            "name": "broken",
            "datasets": "not-an-array",
        }
    ],
}


# Legacy v0.1.1 input — fails strict v0.2 validation (version const mismatch)
# but the legacy shim still produces a valid OBML on conversion.
_OSI_V01_LEGACY = {
    "version": "0.1.1",
    "semantic_model": [
        {
            "name": "legacy",
            "datasets": [
                {
                    "name": "Orders",
                    "source": "WAREHOUSE.PUBLIC.orders",
                    "custom_extensions": [
                        {
                            "vendor_name": "COMMON",
                            "data": json.dumps({"obml_primary_key": ["order_id"]}),
                        }
                    ],
                    "fields": [
                        {
                            "name": "order_id",
                            "expression": {
                                "dialects": [{"dialect": "ANSI_SQL", "expression": "order_id"}]
                            },
                        }
                    ],
                }
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOsiToObmlInputValidation:
    async def test_valid_v02_input_passes(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/convert/osi-to-obml",
            json={"input_yaml": yaml.safe_dump(_OSI_V02_MINIMAL)},
        )
        assert response.status_code == 200
        body = response.json()
        assert "input_validation" in body
        iv = body["input_validation"]
        assert iv is not None
        assert iv["schema_valid"] is True
        assert iv["schema_errors"] == []

    async def test_malformed_input_surfaces_schema_errors(self, client: AsyncClient) -> None:
        """A doc that violates the OSI v0.2 schema should still get a 200
        (advisory mode), with the schema errors surfaced in input_validation
        so callers can inspect them. The conversion itself may also fail —
        we don't assert on output here, only on the input-side check."""
        response = await client.post(
            "/v1/convert/osi-to-obml",
            json={"input_yaml": yaml.safe_dump(_OSI_MALFORMED)},
        )
        # Either 200 with input_validation populated, or 422 if the converter
        # raised on the malformed payload. Both are acceptable; the contract
        # is that the validation error is *not silent*.
        if response.status_code == 200:
            body = response.json()
            iv = body.get("input_validation")
            assert iv is not None
            assert iv["schema_valid"] is False
            assert iv["schema_errors"], "expected at least one schema error"
        else:
            # If the converter raised, the 422 detail mentions the failure
            assert response.status_code == 422

    async def test_legacy_v01_input_surfaces_schema_mismatch(self, client: AsyncClient) -> None:
        """A v0.1.1 doc fails strict v0.2 validation on the const ``version``
        field. The legacy shim still produces valid OBML, so the endpoint
        returns 200 — but input_validation flags the version mismatch."""
        response = await client.post(
            "/v1/convert/osi-to-obml",
            json={"input_yaml": yaml.safe_dump(_OSI_V01_LEGACY)},
        )
        assert response.status_code == 200
        body = response.json()
        iv = body.get("input_validation")
        assert iv is not None
        assert iv["schema_valid"] is False
        # The const mismatch on `version` is the canonical schema error here
        joined = " ".join(iv["schema_errors"]).lower()
        assert "0.1.1" in joined or "version" in joined
        # Output still valid OBML (legacy shim ran)
        assert body["validation"]["schema_valid"] is True


class TestObmlToOsiInputValidation:
    """The obml-to-osi endpoint validates its OBML input against the schema
    (advisory), mirroring osi-to-obml — a violation is surfaced in
    ``input_validation`` rather than silently coerced away."""

    _VALID_OBML = {
        "version": 1.0,
        "dataObjects": {
            "Orders": {
                "code": "orders",
                "database": "WAREHOUSE",
                "schema": "PUBLIC",
                "columns": {
                    "Amount": {"code": "amount", "abstractType": "float"},
                },
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

    async def test_valid_input_reports_schema_valid(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/convert/obml-to-osi",
            json={"input_yaml": yaml.safe_dump(self._VALID_OBML)},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["input_validation"] is not None
        assert body["input_validation"]["schema_valid"] is True
        # No ontology unless explicitly requested.
        assert body["ontology_yaml"] is None
        assert body["ontology_validation"] is None

    async def test_authored_label_surfaced_but_conversion_runs(self, client: AsyncClient) -> None:
        obml = json.loads(json.dumps(self._VALID_OBML))
        obml["measures"]["Revenue"]["label"] = "Authored"
        response = await client.post(
            "/v1/convert/obml-to-osi",
            json={"input_yaml": yaml.safe_dump(obml)},
        )
        # Advisory: conversion still succeeds (200), but the schema violation
        # is surfaced rather than silently dropped.
        assert response.status_code == 200
        body = response.json()
        iv = body["input_validation"]
        assert iv is not None
        assert iv["schema_valid"] is False
        assert any("label" in e for e in iv["schema_errors"]), iv["schema_errors"]
        # The conversion still produced OSI output.
        assert body["output_yaml"]


class TestObmlToOsiOntology:
    """include_ontology=true adds a separate, individually-valid OSI ontology
    document alongside the unchanged core-spec export."""

    _OBML = {
        "version": 1.0,
        "dataObjects": {
            "Orders": {
                "code": "ORDERS",
                "columns": {
                    "Order ID": {"code": "ORDER_ID", "primaryKey": True},
                    "Customer Ref": {"code": "CUSTOMER_ID"},
                },
                "joins": [
                    {
                        "joinType": "many-to-one",
                        "joinTo": "Customers",
                        "columnsFrom": ["Customer Ref"],
                        "columnsTo": ["Customer ID"],
                    }
                ],
            },
            "Customers": {
                "code": "CUSTOMERS",
                "columns": {"Customer ID": {"code": "CUSTOMER_ID", "primaryKey": True}},
            },
        },
    }

    async def test_include_ontology_emits_valid_document(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/convert/obml-to-osi",
            json={"input_yaml": yaml.safe_dump(self._OBML), "include_ontology": True},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # Core export still present and valid.
        assert "semantic_model" in yaml.safe_load(body["output_yaml"])
        assert body["validation"]["schema_valid"] is True
        # Ontology is a distinct, valid document (not merged into the core doc).
        onto = yaml.safe_load(body["ontology_yaml"])
        assert onto["version"] == "0.2.0.dev0"
        assert {c["concept"]["name"] for c in onto["ontology"]} == {"Orders", "Customers"}
        assert "semantic_model" not in onto
        assert body["ontology_validation"]["schema_valid"] is True
        assert body["ontology_validation"]["semantic_valid"] is True
