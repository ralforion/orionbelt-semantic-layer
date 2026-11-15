"""Prometheus metrics (``service/metrics.py``): off by default, then what they count.

REST requests by route template and status, queries by surface, dialect, data
source and outcome, and the caches and sessions read at each scrape. The
pools' own families are tested with the pool (``test_duckdb_pool.py``).
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("prometheus_client")
pytest.importorskip("ob_flight")
duckdb = pytest.importorskip("duckdb")

from ob_flight.datasources import DataSource, implicit_datasource  # noqa: E402
from prometheus_client.parser import text_string_to_metric_families  # noqa: E402

from orionbelt.cache.protocol import CacheStats  # noqa: E402
from orionbelt.service import db_executor, metrics  # noqa: E402
from orionbelt.service.db_executor import (  # noqa: E402
    ExecutionError,
    ExecutionUnavailableError,
    execute_sql,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Scraper:
    def __init__(self, port: int) -> None:
        self.port = port

    def __call__(self) -> dict[str, list[Any]]:
        url = f"http://127.0.0.1:{self.port}/metrics"
        with urllib.request.urlopen(url, timeout=5) as response:
            text = response.read().decode()
        return {f.name: f.samples for f in text_string_to_metric_families(text)}


def value(found: dict[str, list[Any]], name: str, **labels: str) -> float | None:
    """The sample named ``name`` (a counter's with ``_total``) with these labels."""
    for samples in found.values():
        for sample in samples:
            if sample.name not in (name, f"{name}_total"):
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return None


@pytest.fixture
def scrape() -> Iterator[_Scraper]:
    """Metrics on, with no sessions or cache to read; off again after."""
    port = _free_port()
    metrics.start(enabled=True, host="127.0.0.1", port=port)
    try:
        yield _Scraper(port)
    finally:
        metrics.stop()


@pytest.fixture
def lake(tmp_path: Any) -> DataSource:
    path = tmp_path / "lake.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t AS SELECT 1 AS x")
    con.close()
    return DataSource(
        name="lake",
        dialect="duckdb",
        prefix="DB_LAKE_",
        environ={"DB_LAKE_DUCKDB_DATABASE": str(path)},
    )


@pytest.fixture(autouse=True)
def _fresh_tz_cache() -> Iterator[None]:
    db_executor._reset_db_tz_cache()
    yield
    db_executor._reset_db_tz_cache()


class TestOff:
    def test_off_by_default_nothing_is_imported_or_recorded(self) -> None:
        # A process of its own: other tests import prometheus_client.
        code = (
            "import sys\n"
            "from fastapi.testclient import TestClient\n"
            "from orionbelt.api.app import create_app\n"
            "from orionbelt.service import metrics\n"
            "from orionbelt.settings import Settings\n"
            "app = create_app(settings=Settings())\n"
            "assert TestClient(app).get('/health').status_code == 200\n"
            "metrics.observe_query(dialect='duckdb', source='x', outcome='ok', seconds=1.0)\n"
            "assert not metrics.enabled()\n"
            "assert 'prometheus_client' not in sys.modules, 'imported'\n"
            "print('ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
        )
        assert result.stdout.strip().endswith("ok"), result.stderr[-2000:]

    def test_stopping_stops_recording(self) -> None:
        port = _free_port()
        metrics.start(enabled=True, host="127.0.0.1", port=port)
        metrics.stop()
        assert not metrics.enabled()
        metrics.observe_http("GET", "/x", 200, 0.1)  # no error, nothing recorded


class TestRequests:
    def test_requests_by_route_template_and_status(self, scrape: _Scraper) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from orionbelt.api.middleware import MetricsMiddleware

        app = FastAPI()
        seen: list[str] = []

        @app.get("/v1/items/{item_id}")
        def item(item_id: str) -> dict[str, str]:
            seen.append(metrics.SURFACE.get())
            return {"id": item_id}

        app.add_middleware(MetricsMiddleware)
        client = TestClient(app)
        for item_id in ("1", "2"):
            assert client.get(f"/v1/items/{item_id}").status_code == 200
        assert client.get("/nowhere").status_code == 404
        found = scrape()
        assert (
            value(
                found, "obsl_http_requests", method="GET", route="/v1/items/{item_id}", status="200"
            )
            == 2.0
        )
        assert value(found, "obsl_http_requests", route="unmatched", status="404") == 1.0
        assert (
            value(found, "obsl_http_request_duration_seconds_count", route="/v1/items/{item_id}")
            == 2.0
        )
        # The request's queries count as the rest surface.
        assert seen == ["rest", "rest"]

    def test_the_apps_routes_keep_their_prefix_and_never_an_id(self, scrape: _Scraper) -> None:
        from fastapi.testclient import TestClient

        from orionbelt.api.app import create_app
        from orionbelt.api.deps import init_session_manager, reset_session_manager
        from orionbelt.service.session_manager import SessionManager
        from orionbelt.settings import Settings

        app = create_app(settings=Settings())
        mgr = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
        init_session_manager(mgr)
        try:
            client = TestClient(app)
            assert client.get("/v1/models").status_code == 200
            sid = client.post("/v1/sessions").json()["session_id"]
            assert client.get(f"/v1/sessions/{sid}").status_code == 200
        finally:
            reset_session_manager()
            mgr.stop()
        routes = {s.labels["route"] for s in scrape()["obsl_http_requests"]}
        assert {"/v1/models", "/v1/sessions", "/v1/sessions/{session_id}"} <= routes
        assert not any(sid in route for route in routes)


class TestQueries:
    def test_a_query_by_dialect_source_and_outcome(
        self, scrape: _Scraper, lake: DataSource
    ) -> None:
        assert execute_sql("SELECT x FROM t", dialect="duckdb", source=lake).rows == [[1]]
        with pytest.raises(ExecutionError):
            execute_sql("SELECT nothing FROM nowhere", dialect="duckdb", source=lake)
        with pytest.raises(ExecutionUnavailableError):
            execute_sql("SELECT 1", dialect="duckdb", source=implicit_datasource("postgres", {}))
        found = scrape()
        common = {"surface": "other", "dialect": "duckdb"}
        assert value(found, "obsl_queries", source="lake", outcome="ok", **common) == 1.0
        assert value(found, "obsl_queries", source="lake", outcome="error", **common) == 1.0
        assert (
            value(found, "obsl_queries", source="postgres", outcome="unavailable", **common) == 1.0
        )
        assert value(found, "obsl_query_duration_seconds_count", source="lake", **common) == 2.0

    def test_a_query_counts_under_the_surface_it_came_in_through(
        self, scrape: _Scraper, lake: DataSource
    ) -> None:
        token = metrics.SURFACE.set("pgwire")
        try:
            execute_sql("SELECT x FROM t", dialect="duckdb", source=lake)
        finally:
            metrics.SURFACE.reset(token)
        assert value(scrape(), "obsl_queries", surface="pgwire", source="lake") == 1.0

    def test_flight_counts_its_queries_as_flight(
        self, scrape: _Scraper, lake: DataSource, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ob_flight import server as flight_server
        from ob_flight import server_execution

        con = duckdb.connect()

        class Connection:
            def cursor(self) -> Any:
                return con.cursor()

            def close(self) -> None:
                pass

        monkeypatch.setattr(flight_server, "db_connect", lambda target: Connection())
        server = SimpleNamespace(
            _detect_virtual_table=lambda sql: None, _cache=None, _batch_size=1000
        )
        try:
            server_execution.execute_sql(server, "SELECT 42 AS x", "duckdb", None, None, lake)
        finally:
            con.close()
        assert value(scrape(), "obsl_queries", surface="flight", source="lake", outcome="ok") == 1.0
        assert metrics.SURFACE.get() == "other"  # reset for the thread's next call


class TestState:
    def test_the_caches_and_sessions_are_read_at_each_scrape(self) -> None:
        from orionbelt.service.session_manager import SessionManager

        # The real session manager: a stand-in of another shape once hid that
        # its active_count is a property.
        sessions = SessionManager(ttl_seconds=3600, cleanup_interval=9999)
        for _ in range(3):
            sessions.create_session()
        compiled = sessions.compilation_cache.stats()

        class Cache:
            async def stats(self) -> CacheStats:
                return CacheStats(
                    backend="sqlite",
                    entry_count=9,
                    total_size_bytes=1234,
                    hit_count_total=11,
                    miss_count_total=6,
                )

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        port = _free_port()
        metrics.start(
            enabled=True, host="127.0.0.1", port=port, sessions=sessions, cache=Cache(), loop=loop
        )
        try:
            found = _Scraper(port)()
        finally:
            metrics.stop()
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()
            sessions.stop()
        from orionbelt import __version__

        assert value(found, "obsl_build_info", version=__version__) == 1.0
        assert value(found, "obsl_sessions") == 3.0
        assert value(found, "obsl_compilation_cache_entries") == float(compiled.entries)
        assert value(found, "obsl_compilation_cache_lookups", result="hit") == float(compiled.hits)
        assert value(found, "obsl_compilation_cache_lookups", result="bypass") is not None
        assert value(found, "obsl_compilation_cache_evictions") == float(compiled.evictions)
        assert value(found, "obsl_result_cache_backend", backend="sqlite") == 1.0
        assert value(found, "obsl_result_cache_entries") == 9.0
        assert value(found, "obsl_result_cache_lookups", result="miss") == 6.0

    def test_a_scrape_reports_the_rest_when_the_cache_does_not_answer(self) -> None:
        class Cache:
            async def stats(self) -> CacheStats:
                raise RuntimeError("cache down")

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        port = _free_port()
        metrics.start(enabled=True, host="127.0.0.1", port=port, cache=Cache(), loop=loop)
        try:
            found = _Scraper(port)()
        finally:
            metrics.stop()
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()
        assert "obsl_build_info" in found
        assert "obsl_result_cache_entries" not in found
