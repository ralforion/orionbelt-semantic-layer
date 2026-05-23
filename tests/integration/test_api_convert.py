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


class TestObmlToOsiBackwardCompat:
    """The reverse endpoint must NOT regress — input_validation should be
    None when no input-side check runs (the obml-to-osi endpoint doesn't
    validate input yet)."""

    async def test_obml_to_osi_input_validation_absent(self, client: AsyncClient) -> None:
        obml = {
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
        response = await client.post(
            "/v1/convert/obml-to-osi",
            json={"input_yaml": yaml.safe_dump(obml)},
        )
        assert response.status_code == 200
        body = response.json()
        # input_validation field exists but is None — the obml-to-osi endpoint
        # doesn't currently run input-side schema validation.
        assert "input_validation" in body
        assert body["input_validation"] is None
