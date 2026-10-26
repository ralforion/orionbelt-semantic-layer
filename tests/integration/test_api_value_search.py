"""POST /values and /sessions/{id}/models/{model}/values over the REST API.

Each value query runs through the same compile and cached-execute path as
``/query/execute``; DuckDB stands in for the warehouse, as in
``test_duckdb_execution.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import patch

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb package required for execution tests")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from orionbelt.api.app import create_app  # noqa: E402
from orionbelt.api.deps import (  # noqa: E402
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.cache.file import FileCache  # noqa: E402
from orionbelt.service.session_manager import SessionManager  # noqa: E402
from orionbelt.service.value_search import ValueSearchConfig  # noqa: E402
from orionbelt.settings import Settings  # noqa: E402
from tests.conftest import SAMPLE_MODEL_YAML  # noqa: E402
from tests.integration.test_duckdb_execution import (  # noqa: E402
    _SETUP_SAMPLE_SQL,
    _make_execute_sql,
)

# The sample model, with the country kept from being listed.
PRIVATE_COUNTRY_YAML = SAMPLE_MODEL_YAML.replace(
    "    column: Country\n    resultType: string\n",
    "    column: Country\n    resultType: string\n    valueSearch: false\n",
    1,
)


@pytest.fixture
def warehouse() -> Iterator[duckdb.DuckDBPyConnection]:
    conn = duckdb.connect(":memory:")
    conn.execute(_SETUP_SAMPLE_SQL)
    yield conn
    conn.close()


async def _client(
    warehouse: duckdb.DuckDBPyConnection,
    *,
    query_execute: bool = True,
    config: ValueSearchConfig | None = None,
    cache_dir: str | None = None,
) -> AsyncIterator[AsyncClient]:
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
    cache_kwargs: dict = {}
    if cache_dir is not None:
        cache_kwargs = {
            "cache": FileCache(
                cache_dir=cache_dir,
                max_value_bytes=10 * 1024 * 1024,
                max_disk_bytes=50 * 1024 * 1024,
                max_ttl_seconds=3600,
                sweep_interval_seconds=3600,
            ),
            "cache_config": CacheRuntimeConfig(
                backend="file",
                min_ttl_seconds=1,
                max_ttl_seconds=3600,
                unknown_policy="default_ttl",
                unknown_default_ttl_seconds=300,
            ),
        }
    init_session_manager(
        mgr,
        query_execute_enabled=query_execute,
        db_vendor="duckdb",
        value_search_config=config,
        **cache_kwargs,
    )
    try:
        with patch("orionbelt.api.query_cache.execute_sql", _make_execute_sql(warehouse)):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                yield c
    finally:
        reset_session_manager()


async def _load(client: AsyncClient, yaml_text: str = SAMPLE_MODEL_YAML) -> tuple[str, str]:
    sid = (await client.post("/v1/sessions")).json()["session_id"]
    load = await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": yaml_text})
    return sid, load.json()["model_id"]


async def test_session_route_lists_and_searches(warehouse: duckdb.DuckDBPyConnection) -> None:
    async for client in _client(warehouse):
        sid, mid = await _load(client)
        url = f"/v1/sessions/{sid}/models/{mid}/values"

        listing = await client.post(url, json={"dimension": "Customer Country"})
        assert listing.status_code == 200, listing.text
        assert [v["value"] for v in listing.json()["values"]] == ["UK", "US"]

        found = await client.post(url, json={"dimension": "Customer Country", "search": "us"})
        assert found.status_code == 200
        body = found.json()
        assert [(v["value"], v["match"]) for v in body["values"]] == [("US", "exact")]
        assert body["sql"] and "ILIKE" in body["sql"][0].upper()


async def test_shortcut_route_resolves_the_single_model(
    warehouse: duckdb.DuckDBPyConnection,
) -> None:
    async for client in _client(warehouse):
        await _load(client)
        response = await client.post(
            "/v1/values", json={"dimension": "Customer Country", "search": "u"}
        )
        assert response.status_code == 200
        assert [(v["value"], v["match"]) for v in response.json()["values"]] == [
            ("UK", "prefix"),
            ("US", "prefix"),
        ]


async def test_an_opted_out_dimension_is_refused(warehouse: duckdb.DuckDBPyConnection) -> None:
    async for client in _client(warehouse):
        sid, mid = await _load(client, PRIVATE_COUNTRY_YAML)
        response = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/values", json={"dimension": "Customer Country"}
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "VALUE_SEARCH_DISABLED"

        schema = await client.get(f"/v1/sessions/{sid}/models/{mid}/schema")
        dimension = next(d for d in schema.json()["dimensions"] if d["name"] == "Customer Country")
        assert dimension["value_search"] is False


@pytest.mark.parametrize(
    ("query_execute", "config"),
    [(True, ValueSearchConfig(enabled=False)), (False, None)],
    ids=["value-search-disabled", "query-execute-disabled"],
)
async def test_unavailable_is_503(
    warehouse: duckdb.DuckDBPyConnection,
    query_execute: bool,
    config: ValueSearchConfig | None,
) -> None:
    async for client in _client(warehouse, query_execute=query_execute, config=config):
        sid, mid = await _load(client)
        response = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/values", json={"dimension": "Customer Country"}
        )
        assert response.status_code == 503


async def test_a_limit_over_the_cap_is_400(warehouse: duckdb.DuckDBPyConnection) -> None:
    async for client in _client(warehouse, config=ValueSearchConfig(max_limit=5)):
        sid, mid = await _load(client)
        response = await client.post(
            f"/v1/sessions/{sid}/models/{mid}/values",
            json={"dimension": "Customer Country", "limit": 6},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "INVALID_LIMIT"


TIMESTAMP_YAML = """
version: 1.0
settings:
  defaultTimezone: Europe/Berlin
dataObjects:
  Events:
    code: events
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Event Id: {code: EVENT_ID, abstractType: string, primaryKey: true}
      Happened At: {code: HAPPENED_AT, abstractType: timestamp}
dimensions:
  Happened At: {dataObject: Events, column: Happened At, resultType: timestamp}
"""


async def test_a_cached_range_keeps_the_time_zone(
    warehouse: duckdb.DuckDBPyConnection, tmp_path
) -> None:
    warehouse.execute(
        "CREATE TABLE PUBLIC.EVENTS AS SELECT * FROM (VALUES"
        " ('e1', TIMESTAMP '2024-01-01 12:00:00'), ('e2', TIMESTAMP '2024-06-01 08:30:00'))"
        " t(EVENT_ID, HAPPENED_AT)"
    )
    async for client in _client(warehouse, cache_dir=str(tmp_path)):
        sid, mid = await _load(client, TIMESTAMP_YAML)
        url = f"/v1/sessions/{sid}/models/{mid}/values"
        fresh = (await client.post(url, json={"dimension": "Happened At"})).json()
        cached = (await client.post(url, json={"dimension": "Happened At"})).json()
        # A cache hit reads in the model's time zone as the fresh result does.
        assert (cached["minimum"], cached["maximum"]) == (fresh["minimum"], fresh["maximum"])
        assert fresh["minimum"].endswith("+01:00")
