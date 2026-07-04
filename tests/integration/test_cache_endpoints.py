"""End-to-end cache-hit tests for shortcut /query/execute and oneshot batch.

Mocks the warehouse execution path (``execute_sql``) so we can verify the
cache wraps every execute-style endpoint, including the single-model
shortcut and the oneshot batch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from orionbelt.api.app import create_app
from orionbelt.api.deps import (
    CacheRuntimeConfig,
    init_session_manager,
    reset_session_manager,
)
from orionbelt.cache.file import FileCache
from orionbelt.service.db_executor import ColumnMeta, ExecutionResult
from orionbelt.service.session_manager import SessionManager
from orionbelt.settings import Settings
from tests.conftest import SAMPLE_MODEL_YAML


def _stub_exec_result() -> ExecutionResult:
    """A small fixed ExecutionResult — independent of dialect/SQL."""
    return ExecutionResult(
        columns=[
            ColumnMeta("Customer Country", "string"),
            ColumnMeta("Total Revenue", "number", "#,##0.00"),
        ],
        raw_rows=[["US", 100.0], ["UK", 200.0]],
        row_count=2,
        execution_time_ms=1.0,
    )


@pytest.fixture
async def cached_client(tmp_path: Path):
    """App + client with FileCache backed by tmp_path and execute_sql mocked."""
    cache = FileCache(
        cache_dir=str(tmp_path),
        max_value_bytes=10 * 1024 * 1024,
        max_disk_bytes=50 * 1024 * 1024,
        max_ttl_seconds=3600,
        sweep_interval_seconds=3600,
    )
    settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
    app = create_app(settings=settings)
    mgr = SessionManager(
        ttl_seconds=settings.session_ttl_seconds,
        cleanup_interval=settings.session_cleanup_interval,
    )
    init_session_manager(
        mgr,
        query_execute_enabled=True,
        db_vendor="duckdb",
        cache=cache,
        cache_config=CacheRuntimeConfig(
            backend="file",
            min_ttl_seconds=1,
            max_ttl_seconds=3600,
            unknown_policy="default_ttl",
            unknown_default_ttl_seconds=300,
            heartbeat_auth_token="test-token",
        ),
    )

    transport = ASGITransport(app=app)
    with (
        patch("orionbelt.api.query_cache.execute_sql", return_value=_stub_exec_result()),
        patch("orionbelt.api.routers.oneshot.execute_sql", return_value=_stub_exec_result()),
    ):
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, cache
    await cache.shutdown()
    reset_session_manager()


_QUERY_BODY = {
    "select": {
        "dimensions": ["Customer Country"],
        "measures": ["Total Revenue"],
    },
}


class TestSessionExecuteCache:
    async def test_session_execute_caches_then_hits(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        load = await client.post(
            f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML}
        )
        mid = load.json()["model_id"]

        first = await client.post(
            f"/v1/sessions/{sid}/query/execute",
            json={"model_id": mid, "query": _QUERY_BODY, "dialect": "duckdb"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["cached"] is False

        second = await client.post(
            f"/v1/sessions/{sid}/query/execute",
            json={"model_id": mid, "query": _QUERY_BODY, "dialect": "duckdb"},
        )
        assert second.status_code == 200
        body = second.json()
        assert body["cached"] is True
        assert body["cached_at"] is not None
        assert body["row_count"] == 2

    async def test_json_miss_populates_entry_that_arrow_hits(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        """JSON and Arrow share one cache entry — the key is query-only.

        A canonical JSON execution writes the typed rows; a subsequent Arrow
        request for the same query reads that same entry and serves it as an
        Arrow IPC stream (PLAN_arrow_cache.md §3).
        """
        pa = pytest.importorskip("pyarrow", reason="pyarrow required for arrow format")
        client, cache = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        mid = (
            await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        ).json()["model_id"]
        body = {"model_id": mid, "query": _QUERY_BODY, "dialect": "duckdb"}

        # JSON miss populates exactly one entry.
        first = await client.post(f"/v1/sessions/{sid}/query/execute", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["cached"] is False
        assert (await cache.stats()).entry_count == 1

        # Arrow request for the same query hits that entry — no new entry.
        arrow = await client.post(
            f"/v1/sessions/{sid}/query/execute?format=arrow",
            json=body,
            headers={"Accept-Encoding": "gzip"},
        )
        assert arrow.status_code == 200
        assert arrow.headers["content-type"].startswith("application/vnd.apache.arrow.stream")
        assert (await cache.stats()).entry_count == 1
        assert (await cache.stats()).hit_count_total >= 1

        table = pa.ipc.open_stream(pa.BufferReader(arrow.content)).read_all()
        assert table.column_names == ["Customer Country", "Total Revenue"]
        assert table.to_pylist() == [
            {"Customer Country": "US", "Total Revenue": 100.0},
            {"Customer Country": "UK", "Total Revenue": 200.0},
        ]
        # Full envelope restored on the Arrow hit, not just the rows.
        from orionbelt.cache.result_codec import read_envelope

        env = read_envelope(table)
        assert env["sql"] == first.json()["sql"]
        assert env["dialect"] == "duckdb"
        assert [c["name"] for c in env["columns"]] == ["Customer Country", "Total Revenue"]

    async def test_arrow_hit_is_byte_passthrough_of_stored_blob(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        """A raw-arrow hit returns the stored gzip'd blob verbatim (no re-encode).

        The blob is stamped ``cached=true`` at write time, so the zero-copy hit
        carries the in-band flag the UI reads for its "cache" source label.
        """
        import glob
        import os

        pa = pytest.importorskip("pyarrow", reason="pyarrow required for arrow format")
        from orionbelt.cache.result_codec import read_envelope

        client, cache = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        mid = (
            await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        ).json()["model_id"]
        body = {"model_id": mid, "query": _QUERY_BODY, "dialect": "duckdb"}
        hdrs = {"Accept-Encoding": "gzip"}

        # Arrow miss populates the entry; the fresh response is NOT flagged cached.
        miss = await client.post(
            f"/v1/sessions/{sid}/query/execute?format=arrow", json=body, headers=hdrs
        )
        assert miss.status_code == 200
        miss_tbl = pa.ipc.open_stream(pa.BufferReader(miss.content)).read_all()
        assert read_envelope(miss_tbl)["cached"] is False

        # The stored blob on disk (single .arrow payload) is what a hit serves.
        stored_files = glob.glob(
            os.path.join(str(cache._results_dir), "**", "*.arrow"), recursive=True
        )
        assert len(stored_files) == 1
        import gzip as _gzip

        stored_tbl = pa.ipc.open_stream(
            pa.BufferReader(_gzip.decompress(Path(stored_files[0]).read_bytes()))
        ).read_all()
        # Stored blob is stamped cached=true at write time (enables verbatim serve).
        assert read_envelope(stored_tbl)["cached"] is True

        # Arrow hit: served via byte-passthrough of that stored blob. httpx auto-
        # gunzips, so compare the decoded table + the in-band cached flag. The
        # hit must be TRUE zero-copy: the gzip/Arrow decode is skipped entirely,
        # so cache_decode is never called during the request.
        from unittest.mock import patch as _patch

        import orionbelt.api.query_cache as qc

        with _patch.object(qc, "cache_decode", wraps=qc.cache_decode) as spy_decode:
            hit = await client.post(
                f"/v1/sessions/{sid}/query/execute?format=arrow", json=body, headers=hdrs
            )
        assert hit.status_code == 200
        assert hit.headers.get("content-encoding") == "gzip"
        assert spy_decode.call_count == 0  # zero-copy: no decode on the raw-arrow hit
        hit_tbl = pa.ipc.open_stream(pa.BufferReader(hit.content)).read_all()
        assert read_envelope(hit_tbl)["cached"] is True
        assert hit_tbl.to_pylist() == miss_tbl.to_pylist()
        assert hit_tbl.equals(stored_tbl)

    async def test_json_hit_still_decodes(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        """A JSON (non-passthrough) hit still decodes the blob to rebuild rows."""
        from unittest.mock import patch as _patch

        import orionbelt.api.query_cache as qc

        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        mid = (
            await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        ).json()["model_id"]
        body = {"model_id": mid, "query": _QUERY_BODY, "dialect": "duckdb"}

        assert (await client.post(f"/v1/sessions/{sid}/query/execute", json=body)).json()[
            "cached"
        ] is False
        with _patch.object(qc, "cache_decode", wraps=qc.cache_decode) as spy_decode:
            hit = await client.post(f"/v1/sessions/{sid}/query/execute", json=body)
        assert hit.json()["cached"] is True
        assert spy_decode.call_count == 1  # JSON path needs the decoded rows


class TestShortcutExecuteCache:
    async def test_shortcut_execute_caches_then_hits(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        # Single-model-mode-style usage: one session, one model, then call shortcut.
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})

        first = await client.post(
            "/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"}
        )
        assert first.status_code == 200, first.text
        assert first.json()["cached"] is False

        second = await client.post(
            "/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"}
        )
        assert second.status_code == 200
        body = second.json()
        assert body["cached"] is True
        assert body["physical_tables"]


class TestOneshotBatchCache:
    async def test_oneshot_batch_caches_then_hits(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client

        first = await client.post(
            "/v1/oneshot/batch",
            json={
                "model_yaml": SAMPLE_MODEL_YAML,
                "execute": True,
                "queries": [{"id": "q1", "query": _QUERY_BODY}],
                "dialect": "duckdb",
                "persist_model": True,
            },
        )
        assert first.status_code == 200, first.text
        first_data = first.json()
        sid = first_data["session_id"]
        mid = first_data["model_id"]
        assert first_data["results"][0]["cached"] is False

        # The oneshot hit path decodes through the shared try_cache_get, which
        # offloads the gzip + Arrow decode to a worker thread (never blocks the
        # event loop). Assert the delegation so it can't silently regress to a
        # synchronous inline decode.
        from unittest.mock import AsyncMock
        from unittest.mock import patch as _patch

        import orionbelt.api.routers.oneshot as osm

        with _patch.object(
            osm, "try_cache_get", new=AsyncMock(wraps=osm.try_cache_get)
        ) as spy_get:
            second = await client.post(
                "/v1/oneshot/batch",
                json={
                    "session_id": sid,
                    "model_id": mid,
                    "execute": True,
                    "queries": [{"id": "q1", "query": _QUERY_BODY}],
                    "dialect": "duckdb",
                },
            )
        assert second.status_code == 200
        result = second.json()["results"][0]
        assert result["cached"] is True
        assert spy_get.await_count == 1  # hit went through the offloaded shared getter
        assert result["row_count"] == 2


class TestShortcutPlanQuery:
    async def test_shortcut_plan_returns_plan(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})

        resp = await client.post(
            "/v1/query/plan",
            json={
                "model_id": "ignored-by-shortcut",
                "query": _QUERY_BODY,
                "dialect": "postgres",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "ok"
        assert data["would_compile"] is True
        assert any("ORDERS" in t for t in data["physical_tables"])


_MODEL_WITH_EXAMPLES = (
    SAMPLE_MODEL_YAML
    + """
examples:
  - name: revenue_by_country
    description: Total completed-order revenue by customer country.
    intentTags: [revenue, geography]
    query:
      select:
        dimensions: [Customer Country]
        measures: [Total Revenue]
"""
)


class TestShortcutExamples:
    async def test_shortcut_list_examples(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples")
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()["examples"]]
        assert "revenue_by_country" in names

    async def test_shortcut_list_examples_intent_filter(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples", params={"intent": "revenue"})
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()["examples"]]
        assert names == ["revenue_by_country"]

    async def test_shortcut_get_example(self, cached_client: tuple[AsyncClient, FileCache]) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples/revenue_by_country")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "revenue_by_country"
        assert data["compiled_sql_preview"] is not None

    async def test_shortcut_get_example_404(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": _MODEL_WITH_EXAMPLES})

        resp = await client.get("/v1/examples/no_such_example")
        assert resp.status_code == 404


class TestHeartbeatInvalidatesShortcut:
    async def test_heartbeat_evicts_then_next_call_misses(
        self, cached_client: tuple[AsyncClient, FileCache]
    ) -> None:
        client, _ = cached_client
        sid = (await client.post("/v1/sessions")).json()["session_id"]
        await client.post(f"/v1/sessions/{sid}/models", json={"model_yaml": SAMPLE_MODEL_YAML})
        # Prime the cache via the shortcut
        await client.post("/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"})
        hit = await client.post("/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"})
        assert hit.json()["cached"] is True

        # Heartbeat the underlying physical table → invalidate
        hb = await client.post(
            "/v1/heartbeat",
            json={"database": "WAREHOUSE", "schema": "PUBLIC", "table": "ORDERS"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert hb.status_code == 200
        assert hb.json()["invalidated_cache_entries"] >= 1

        # Next call must be a fresh miss
        miss = await client.post(
            "/v1/query/execute", json=_QUERY_BODY, params={"dialect": "duckdb"}
        )
        assert miss.json()["cached"] is False
