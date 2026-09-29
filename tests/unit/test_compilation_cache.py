"""The compilation cache returns what the compiler would, and only for the same inputs."""

from __future__ import annotations

import gc
import threading
from datetime import date
from typing import Any
from unittest import mock

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.compilation_cache import CompilationCache, query_key
from orionbelt.service.model_store import ModelStore
from orionbelt.service.session_manager import SessionManager
from tests.conftest import SAMPLE_MODEL_YAML


def _model() -> SemanticModel:
    store = ModelStore()
    loaded = store.load_model(SAMPLE_MODEL_YAML, dedup=False)
    return store.get_model(loaded.model_id)


@pytest.fixture
def model() -> SemanticModel:
    return _model()


def _query(**overrides: Any) -> QueryObject:
    data: dict[str, Any] = {
        "select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]},
        **overrides,
    }
    return QueryObject.model_validate(data)


class _Counting(CompilationPipeline):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def compile(self, query: QueryObject, model: SemanticModel, dialect_name: str) -> Any:
        self.calls += 1
        return super().compile(query, model, dialect_name)


def _cache(**kwargs: int) -> CompilationCache:
    return CompilationCache(**{"max_entries": 100, **kwargs})


class TestHits:
    def test_disabled_cache_always_compiles(self, model: SemanticModel) -> None:
        cache, pipeline = CompilationCache(), _Counting()
        cache.compile(pipeline, _query(), model, "duckdb")
        cache.compile(pipeline, _query(), model, "duckdb")
        assert pipeline.calls == 2
        assert not cache.stats().enabled

    def test_repeated_query_compiles_once(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        first = cache.compile(pipeline, _query(), model, "duckdb")
        second = cache.compile(pipeline, _query(), model, "duckdb")
        assert pipeline.calls == 1
        assert second == first == CompilationPipeline().compile(_query(), model, "duckdb")
        stats = cache.stats()
        assert (stats.hits, stats.misses, stats.entries) == (1, 1, 1)

    def test_hit_keeps_diagnostics_and_dependencies(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        fresh = cache.compile(pipeline, _query(), model, "duckdb")
        hit = cache.compile(pipeline, _query(), model, "duckdb")
        assert hit.physical_tables == fresh.physical_tables != []
        assert hit.warnings == fresh.warnings
        assert hit.sql_valid == fresh.sql_valid
        assert hit.explain == fresh.explain

    def test_camel_case_and_snake_case_share_an_entry(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        cache.compile(pipeline, _query(orderBy=[{"field": "Total Revenue"}]), model, "duckdb")
        cache.compile(pipeline, _query(order_by=[{"field": "Total Revenue"}]), model, "duckdb")
        assert pipeline.calls == 1


class TestIsolation:
    def test_mutating_a_hit_does_not_change_the_next(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        cache.compile(pipeline, _query(), model, "duckdb")
        hit = cache.compile(pipeline, _query(), model, "duckdb")
        hit.sql = "formatted"
        hit.warnings.append("x")  # type: ignore[arg-type]
        hit.physical_tables.clear()
        again = cache.compile(pipeline, _query(), model, "duckdb")
        assert again.sql != "formatted"
        assert "x" not in again.warnings
        assert again.physical_tables

    def test_mutating_the_first_result_does_not_change_the_entry(
        self, model: SemanticModel
    ) -> None:
        cache, pipeline = _cache(), _Counting()
        first = cache.compile(pipeline, _query(), model, "duckdb")
        expected = first.sql
        first.sql = "formatted"
        assert cache.compile(pipeline, _query(), model, "duckdb").sql == expected

    def test_equal_models_do_not_share_entries(self) -> None:
        cache, pipeline = _cache(), _Counting()
        cache.compile(pipeline, _query(), _model(), "duckdb")
        cache.compile(pipeline, _query(), _model(), "duckdb")
        assert pipeline.calls == 2

    def test_collected_model_takes_its_entries_along(self) -> None:
        cache, pipeline = _cache(), _Counting()
        model = _model()
        cache.compile(pipeline, _query(), model, "duckdb")
        assert cache.stats().entries == 1
        del model
        gc.collect()
        stats = cache.stats()
        assert stats.entries == 0
        assert stats.bytes == 0


class TestKeys:
    @pytest.mark.parametrize(
        "other",
        [
            pytest.param({"limit": 10}, id="limit"),
            pytest.param({"offset": 5, "limit": 10}, id="offset"),
            pytest.param({"allowFanOut": True}, id="allowFanOut"),
            pytest.param(
                {"select": {"dimensions": ["Customer Country"], "measures": ["Order Count"]}},
                id="measures",
            ),
            pytest.param(
                {"orderBy": [{"field": "Total Revenue", "direction": "desc"}]}, id="orderBy"
            ),
            pytest.param(
                {"where": [{"field": "Customer Country", "op": "=", "value": "DE"}]}, id="where"
            ),
        ],
    )
    def test_every_semantic_input_misses(self, model: SemanticModel, other: dict) -> None:
        cache, pipeline = _cache(), _Counting()
        cache.compile(pipeline, _query(), model, "duckdb")
        cache.compile(pipeline, _query(**other), model, "duckdb")
        assert pipeline.calls == 2

    def test_dialect_misses(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        cache.compile(pipeline, _query(), model, "duckdb")
        cache.compile(pipeline, _query(), model, "postgres")
        assert pipeline.calls == 2

    def test_list_order_is_kept(self) -> None:
        a = _query(orderBy=[{"field": "Total Revenue"}, {"field": "Customer Country"}])
        b = _query(orderBy=[{"field": "Customer Country"}, {"field": "Total Revenue"}])
        assert query_key(a) != query_key(b)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (1, True),
            (1, "1"),
            (1, 1.0),
            (None, "None"),
            ([1, 2], [2, 1]),
            ({"a": 1}, {"a": True}),
        ],
    )
    def test_filter_values_keep_their_type(self, left: object, right: object) -> None:
        def with_value(value: object) -> QueryObject:
            return _query(where=[{"field": "Customer Country", "op": "=", "value": value}])

        assert query_key(with_value(left)) != query_key(with_value(right))

    def test_dates_share_the_key_of_their_iso_string(self) -> None:
        """The query model coerces a date filter value to its ISO string first."""

        def with_value(value: object) -> QueryObject:
            return _query(where=[{"field": "Customer Country", "op": "=", "value": value}])

        assert query_key(with_value(date(2024, 1, 1))) == query_key(with_value("2024-01-01"))

    def test_unencodable_value_bypasses(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        query = _query(where=[{"field": "Customer Country", "op": "=", "value": {"k": {"DE"}}}])
        assert query_key(query) is None
        with mock.patch.object(CompilationPipeline, "compile", return_value="compiled"):
            assert cache.compile(pipeline, query, model, "duckdb") == "compiled"
        assert cache.stats().bypasses == 1
        assert cache.stats().entries == 0


class TestBounds:
    def test_entry_count_is_bounded_least_recently_used_first(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(max_entries=2), _Counting()
        for limit in (1, 2):
            cache.compile(pipeline, _query(limit=limit), model, "duckdb")
        cache.compile(pipeline, _query(limit=1), model, "duckdb")  # refresh 1
        cache.compile(pipeline, _query(limit=3), model, "duckdb")  # evicts 2
        assert cache.stats().entries == 2
        calls = pipeline.calls
        cache.compile(pipeline, _query(limit=1), model, "duckdb")
        assert pipeline.calls == calls
        cache.compile(pipeline, _query(limit=2), model, "duckdb")
        assert pipeline.calls == calls + 1

    def test_bytes_are_bounded(self, model: SemanticModel) -> None:
        probe = _cache()
        probe.compile(_Counting(), _query(limit=1), model, "duckdb")
        one = probe.stats().bytes
        cache = _cache(max_bytes=one * 2 + one // 2)
        for limit in range(1, 6):
            cache.compile(_Counting(), _query(limit=limit), model, "duckdb")
        stats = cache.stats()
        assert stats.bytes <= stats.max_bytes
        assert stats.entries == 2
        assert stats.evictions == 3

    def test_oversized_entry_is_not_stored(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(max_entry_bytes=10), _Counting()
        result = cache.compile(pipeline, _query(), model, "duckdb")
        assert result.sql
        assert cache.stats().entries == 0

    def test_failure_is_not_cached(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        bad = QueryObject.model_validate({"select": {"measures": ["No Such Measure"]}})
        for _ in range(2):
            with pytest.raises(Exception, match="No Such Measure"):
                cache.compile(pipeline, bad, model, "duckdb")
        assert pipeline.calls == 2
        assert cache.stats().entries == 0

    def test_clear_keeps_counters(self, model: SemanticModel) -> None:
        cache, pipeline = _cache(), _Counting()
        cache.compile(pipeline, _query(), model, "duckdb")
        assert cache.clear() == 1
        stats = cache.stats()
        assert (stats.entries, stats.bytes, stats.misses) == (0, 0, 1)


class TestConcurrency:
    def test_threads_get_equal_results(self, model: SemanticModel) -> None:
        cache = _cache()
        expected = CompilationPipeline().compile(_query(), model, "duckdb")
        results: list[object] = []

        def run() -> None:
            for _ in range(20):
                results.append(cache.compile(CompilationPipeline(), _query(), model, "duckdb"))

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 80
        assert all(r == expected for r in results)
        assert cache.stats().entries == 1


class TestWiring:
    def test_store_without_a_cache_compiles_every_time(self) -> None:
        store = ModelStore()
        model_id = store.load_model(SAMPLE_MODEL_YAML).model_id
        with mock.patch.object(
            CompilationPipeline, "compile", autospec=True, side_effect=CompilationPipeline.compile
        ) as compile_:
            store.compile_query(model_id, _query(), "duckdb")
            store.compile_query(model_id, _query(), "duckdb")
        assert compile_.call_count == 2

    def test_sessions_share_one_cache_for_a_shared_model(self) -> None:
        mgr = SessionManager(compilation_cache=_cache())
        stores = [mgr.get_store(mgr.create_session().session_id) for _ in range(2)]
        ids = [s.load_model(SAMPLE_MODEL_YAML).model_id for s in stores]
        with mock.patch.object(
            CompilationPipeline, "compile", autospec=True, side_effect=CompilationPipeline.compile
        ) as compile_:
            first = stores[0].compile_query(ids[0], _query(), "duckdb")
            second = stores[1].compile_query(ids[1], _query(), "duckdb")
        assert compile_.call_count == 1
        assert first == second
