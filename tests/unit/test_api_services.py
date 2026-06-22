"""Direct unit tests for the extracted API service layer (Phase 3).

These exercise ``api/services/*`` without any ASGI / FastAPI setup — the
point of the extraction is that the compilation, model-loading, and
dialect-resolution logic is now testable as plain functions against a
``ModelStore``.
"""

from __future__ import annotations

from types import SimpleNamespace

from orionbelt.api.services.model_loading import _model_load_fields
from orionbelt.api.services.query_compilation import (
    _resolve_dialect,
    build_compile_response,
    compile_query_or_raise,
)
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.service.model_store import ModelStore

_MODEL = """\
version: 1.0
dataObjects:
  Orders:
    code: orders
    database: db
    schema: public
    columns:
      Amount: {code: amount, abstractType: float}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    resultType: float
    aggregation: sum
"""


def test_resolve_dialect_precedence() -> None:
    # Explicit request dialect wins.
    assert _resolve_dialect(request_dialect="snowflake", model=None) == "snowflake"
    # Then the model's declared default.
    model = SimpleNamespace(settings=SimpleNamespace(default_dialect="bigquery"))
    assert _resolve_dialect(request_dialect=None, model=model) == "bigquery"
    # Then the supplied fallback (typically DB_VENDOR).
    no_default = SimpleNamespace(settings=None)
    assert _resolve_dialect(request_dialect=None, model=no_default, fallback="mysql") == "mysql"
    # Finally postgres.
    assert _resolve_dialect(request_dialect=None, model=no_default) == "postgres"


def test_compile_query_service_builds_response() -> None:
    store = ModelStore()
    loaded = store.load_model(_MODEL)
    query = QueryObject(select=QuerySelect(measures=["Revenue"]))
    result = compile_query_or_raise(
        store=store, model_id=loaded.model_id, query=query, dialect="postgres"
    )
    response = build_compile_response(result)
    assert "SELECT" in response.sql.upper()
    assert "REVENUE" in response.sql.upper()


def test_model_load_fields_shape() -> None:
    store = ModelStore()
    loaded = store.load_model(_MODEL)
    fields = _model_load_fields(loaded)
    assert fields["model_id"] == loaded.model_id
    assert fields["measures"] == 1
    assert fields["data_objects"] == 1
