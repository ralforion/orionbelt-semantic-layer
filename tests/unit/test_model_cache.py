"""Unit tests for the cross-session content-addressed ModelCache.

Two ``ModelStore`` instances sharing one ``ModelCache`` model the real
topology: distinct sessions, one process-wide cache. Identical OBML must
compile once and be shared under a stable content-derived id, with
refcounted lifecycle so a model survives until its last referencing session
drops it.
"""

from __future__ import annotations

import pytest

from orionbelt.service import model_store as model_store_mod
from orionbelt.service.model_cache import ModelCache
from orionbelt.service.model_store import ModelStore
from tests.conftest import SAMPLE_MODEL_YAML


@pytest.fixture
def cache() -> ModelCache:
    return ModelCache()


def test_identical_yaml_shares_one_compiled_model(cache: ModelCache) -> None:
    a = ModelStore(shared_cache=cache)
    b = ModelStore(shared_cache=cache)

    ra = a.load_model(SAMPLE_MODEL_YAML)
    rb = b.load_model(SAMPLE_MODEL_YAML)

    # Same content -> same stable content-derived id across sessions.
    assert ra.model_id == rb.model_id
    # And the very same compiled SemanticModel object is shared, not copied.
    assert a.get_model(ra.model_id) is b.get_model(rb.model_id)

    stats = cache.stats()
    assert stats["entries"] == 1
    assert stats["refs"] == 2


def test_second_session_does_not_recompile(
    cache: ModelCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real_export = model_store_mod.export_obsl

    def counting_export(model: object, model_id: str) -> object:
        calls["n"] += 1
        return real_export(model, model_id)  # type: ignore[arg-type]

    monkeypatch.setattr(model_store_mod, "export_obsl", counting_export)

    a = ModelStore(shared_cache=cache)
    b = ModelStore(shared_cache=cache)
    a.load_model(SAMPLE_MODEL_YAML)
    b.load_model(SAMPLE_MODEL_YAML)

    # export_obsl runs at compile time only; the 2nd (cross-session) load
    # adopts the shared entry without recompiling.
    assert calls["n"] == 1


def test_dedup_false_stays_private_and_distinct(cache: ModelCache) -> None:
    a = ModelStore(shared_cache=cache)
    r1 = a.load_model(SAMPLE_MODEL_YAML, dedup=False)
    r2 = a.load_model(SAMPLE_MODEL_YAML, dedup=False)
    assert r1.model_id != r2.model_id
    # Private (non-dedup-eligible) loads never touch the shared cache.
    assert cache.stats()["entries"] == 0


def test_release_keeps_model_alive_until_last_reference(cache: ModelCache) -> None:
    a = ModelStore(shared_cache=cache)
    b = ModelStore(shared_cache=cache)
    ra = a.load_model(SAMPLE_MODEL_YAML)
    b.load_model(SAMPLE_MODEL_YAML)
    assert cache.stats()["refs"] == 2

    # One session drops it — the model survives for the other.
    a.close()
    assert cache.stats()["entries"] == 1
    assert cache.stats()["refs"] == 1
    assert len(b.get_model(ra.model_id).data_objects) == 2

    # Last reference gone -> evicted.
    b.close()
    assert cache.stats()["entries"] == 0


def test_remove_model_releases_shared_reference(cache: ModelCache) -> None:
    a = ModelStore(shared_cache=cache)
    r = a.load_model(SAMPLE_MODEL_YAML)
    assert cache.stats()["refs"] == 1
    a.remove_model(r.model_id)
    assert cache.stats()["entries"] == 0


def test_same_store_reload_reuses_without_extra_reference(cache: ModelCache) -> None:
    a = ModelStore(shared_cache=cache)
    r1 = a.load_model(SAMPLE_MODEL_YAML)
    r2 = a.load_model(SAMPLE_MODEL_YAML)  # local dedup hit
    assert r1.model_id == r2.model_id
    assert r2.model_load == "reused"
    # Reloading into the same store must not double-count the reference.
    assert cache.stats()["refs"] == 1


def test_no_shared_cache_keeps_models_private(cache: ModelCache) -> None:
    # A bare store (no shared cache) behaves as before: nothing is shared.
    a = ModelStore()
    r = a.load_model(SAMPLE_MODEL_YAML)
    assert len(r.model_id) == 16
    assert cache.stats()["entries"] == 0
