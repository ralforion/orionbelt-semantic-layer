"""Flight SQL catalog calls list and describe only the models a connection reaches.

A connection whose ``database`` header selects ``commerce`` sees ``commerce``;
one without a header sees every loaded model.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from orionbelt.service.session_manager import SessionManager

from ob_flight.flight_sql import CMD_GET_DB_SCHEMAS
from ob_flight.server import OBFlightServer
from ob_flight.server_catalog import build_catalog_table, resolve_model_for_catalog

_MODEL_YAML = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Amount:
        code: AMOUNT
        abstractType: float
measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
"""


class _Context:
    """A call context whose routing middleware selects ``model``, as the header does."""

    def __init__(self, model: str) -> None:
        self._middleware = SimpleNamespace(selected_model=model)

    def get_middleware(self, key: str) -> Any:
        return self._middleware


@pytest.fixture
def server() -> OBFlightServer:
    """A server over a SessionManager holding ``commerce`` and ``shop``, no socket bound."""

    mgr = SessionManager()
    for name in ("commerce", "shop"):
        mgr.get_or_create_named(name).load_model(_MODEL_YAML)
    srv = OBFlightServer.__new__(OBFlightServer)
    srv._session_manager = mgr
    srv._default_dialect = "duckdb"
    srv._lock = threading.Lock()
    return srv


@pytest.mark.parametrize(
    ("context", "schemas"),
    [
        (_Context("commerce"), ["commerce"]),
        (_Context("shop"), ["shop"]),
        (None, ["commerce", "shop"]),
    ],
)
def test_db_schemas_list_the_models_a_connection_reaches(
    server: OBFlightServer, context: Any, schemas: list[str]
) -> None:
    table = build_catalog_table(server, CMD_GET_DB_SCHEMAS, b"", context=context)
    assert sorted(table.column("db_schema_name").to_pylist()) == schemas


def test_another_models_schema_describes_nothing(server: OBFlightServer) -> None:
    commerce = _Context("commerce")
    assert resolve_model_for_catalog(server, None, commerce, db_schema_filter="shop") is None
    assert resolve_model_for_catalog(server, "shop", commerce) is None


def test_the_connections_model_is_described(server: OBFlightServer) -> None:
    commerce = _Context("commerce")
    own = resolve_model_for_catalog(server, "orionbelt", commerce, db_schema_filter="commerce")
    assert own is not None
    assert own._ob_model_id == "commerce"


def test_without_a_header_every_model_is_described(server: OBFlightServer) -> None:
    shop = resolve_model_for_catalog(server, None, None, db_schema_filter="shop")
    assert shop is not None
    assert shop._ob_model_id == "shop"


def test_without_a_header_a_session_filter_describes_that_session(
    server: OBFlightServer,
) -> None:
    """A REST session is not listed, but a schema filter naming its id describes it."""

    session_id = server._session_manager.create_session().session_id
    server._session_manager.get_store(session_id).load_model(_MODEL_YAML)
    described = resolve_model_for_catalog(server, None, None, db_schema_filter=session_id)
    assert described is not None
    commerce = _Context("commerce")
    assert resolve_model_for_catalog(server, None, commerce, db_schema_filter=session_id) is None
