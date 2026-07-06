"""Unit tests for SessionManager."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from orionbelt.service.model_store import ModelCapacityError, ModelValidationError
from orionbelt.service.session_manager import (
    SessionCapacityError,
    SessionExpiredError,
    SessionManager,
    SessionNotFoundError,
)


class TestSessionLifecycle:
    def test_create_session(self, session_manager: SessionManager) -> None:
        info = session_manager.create_session()
        assert len(info.session_id) == 32
        assert info.model_count == 0
        assert info.metadata == {}

    def test_create_with_metadata(self, session_manager: SessionManager) -> None:
        info = session_manager.create_session(metadata={"user": "alice"})
        assert info.metadata == {"user": "alice"}

    def test_get_store(self, session_manager: SessionManager) -> None:
        info = session_manager.create_session()
        store = session_manager.get_store(info.session_id)
        assert store is not None
        assert store.list_models() == []

    def test_get_store_missing_raises(self, session_manager: SessionManager) -> None:
        with pytest.raises(SessionNotFoundError, match="not found"):
            session_manager.get_store("nonexist123")

    def test_get_session(self, session_manager: SessionManager) -> None:
        info = session_manager.create_session()
        retrieved = session_manager.get_session(info.session_id)
        assert retrieved.session_id == info.session_id

    def test_close_session(self, session_manager: SessionManager) -> None:
        info = session_manager.create_session()
        session_manager.close_session(info.session_id)
        with pytest.raises(SessionNotFoundError):
            session_manager.get_store(info.session_id)

    def test_close_missing_raises(self, session_manager: SessionManager) -> None:
        with pytest.raises(SessionNotFoundError, match="not found"):
            session_manager.close_session("nonexist123")

    def test_list_sessions(self, session_manager: SessionManager) -> None:
        session_manager.create_session()
        session_manager.create_session()
        sessions = session_manager.list_sessions()
        assert len(sessions) == 2

    def test_active_count(self, session_manager: SessionManager) -> None:
        assert session_manager.active_count == 0
        session_manager.create_session()
        session_manager.create_session()
        assert session_manager.active_count == 2


class TestSessionExpiration:
    def test_idle_expired_session_raises_expired_error(self) -> None:
        mgr = SessionManager(ttl_seconds=0, max_age_seconds=86400, cleanup_interval=9999)
        info = mgr.create_session()
        time.sleep(0.05)
        with pytest.raises(SessionExpiredError, match="expired"):
            mgr.get_store(info.session_id)

    def test_get_session_idle_expired_raises_expired_error(self) -> None:
        mgr = SessionManager(ttl_seconds=0, max_age_seconds=86400, cleanup_interval=9999)
        info = mgr.create_session()
        time.sleep(0.05)
        with pytest.raises(SessionExpiredError, match="expired"):
            mgr.get_session(info.session_id)

    def test_absolute_max_age_expiry(self) -> None:
        """Session expires after max_age_seconds even if actively used."""
        mgr = SessionManager(ttl_seconds=3600, max_age_seconds=0, cleanup_interval=9999)
        info = mgr.create_session()
        time.sleep(0.05)
        with pytest.raises(SessionExpiredError, match="max-age"):
            mgr.get_store(info.session_id)

    def test_max_age_get_session_expired(self) -> None:
        mgr = SessionManager(ttl_seconds=3600, max_age_seconds=0, cleanup_interval=9999)
        info = mgr.create_session()
        time.sleep(0.05)
        with pytest.raises(SessionExpiredError, match="max-age"):
            mgr.get_session(info.session_id)

    def test_protected_session_not_expired_on_access(self) -> None:
        """Admin-loaded (protected) sessions must survive TTL/max-age on get_store/get_session.

        Regression: ``_is_expired`` previously ignored ``protected``, so
        ``get_store()`` would delete a protected session on the first
        access past TTL even though ``_purge_expired`` correctly skipped
        it.
        """
        mgr = SessionManager(ttl_seconds=0, max_age_seconds=0, cleanup_interval=9999)
        mgr.get_or_create_named("admin_model")
        time.sleep(0.05)
        # Neither idle TTL nor max-age should evict a protected session.
        store = mgr.get_store("admin_model")
        assert store is not None
        info = mgr.get_session("admin_model")
        assert info.session_id == "admin_model"

    def test_not_found_vs_expired_distinction(self) -> None:
        """SessionNotFoundError for unknown IDs, SessionExpiredError for expired ones."""
        mgr = SessionManager(ttl_seconds=0, max_age_seconds=86400, cleanup_interval=9999)
        info = mgr.create_session()
        time.sleep(0.05)

        # Expired session → SessionExpiredError
        with pytest.raises(SessionExpiredError):
            mgr.get_store(info.session_id)

        # After expiry removal, same ID → SessionNotFoundError
        with pytest.raises(SessionNotFoundError):
            mgr.get_store(info.session_id)

        # Never-existed ID → SessionNotFoundError
        with pytest.raises(SessionNotFoundError):
            mgr.get_store("never_existed_id")


class TestExpiryInfo:
    def test_session_info_has_expires_at(self, session_manager: SessionManager) -> None:
        info = session_manager.create_session()
        assert info.expires_at is not None
        assert info.max_expires_at is not None
        # expires_at should be in the future
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        assert info.expires_at > now
        assert info.max_expires_at > now

    def test_expires_at_refreshes_on_access(self, session_manager: SessionManager) -> None:
        info = session_manager.create_session()
        time.sleep(0.05)
        refreshed = session_manager.get_session(info.session_id)
        # expires_at should have been pushed forward
        assert refreshed.expires_at >= info.expires_at
        # max_expires_at should stay roughly the same (same creation time)
        delta = abs((refreshed.max_expires_at - info.max_expires_at).total_seconds())
        assert delta < 1.0  # within 1 second tolerance


class TestSessionCap:
    def test_global_session_cap(self) -> None:
        mgr = SessionManager(
            ttl_seconds=3600,
            max_age_seconds=86400,
            max_sessions=3,
            cleanup_interval=9999,
        )
        mgr.create_session()
        mgr.create_session()
        mgr.create_session()
        with pytest.raises(SessionCapacityError, match="Maximum"):
            mgr.create_session()

    def test_cap_does_not_count_default_session(self) -> None:
        mgr = SessionManager(
            ttl_seconds=3600,
            max_age_seconds=86400,
            max_sessions=2,
            cleanup_interval=9999,
        )
        mgr.get_or_create_default()  # should not count
        mgr.create_session()
        mgr.create_session()
        with pytest.raises(SessionCapacityError):
            mgr.create_session()

    def test_cap_frees_after_close(self) -> None:
        mgr = SessionManager(
            ttl_seconds=3600,
            max_age_seconds=86400,
            max_sessions=2,
            cleanup_interval=9999,
        )
        s1 = mgr.create_session()
        mgr.create_session()
        with pytest.raises(SessionCapacityError):
            mgr.create_session()
        mgr.close_session(s1.session_id)
        # Now should succeed
        mgr.create_session()

    def test_cap_frees_after_expiry(self) -> None:
        mgr = SessionManager(
            ttl_seconds=0,
            max_age_seconds=86400,
            max_sessions=2,
            cleanup_interval=9999,
        )
        mgr.create_session()
        mgr.create_session()
        time.sleep(0.05)
        # Expired sessions don't count toward cap
        mgr.create_session()


class TestModelCap:
    def test_max_models_per_session(self) -> None:
        mgr = SessionManager(
            ttl_seconds=3600,
            max_age_seconds=86400,
            max_models_per_session=2,
            cleanup_interval=9999,
        )
        info = mgr.create_session()
        store = mgr.get_store(info.session_id)

        from tests.conftest import SAMPLE_MODEL_YAML

        # dedup=False so each load creates a fresh model and counts toward
        # the per-session model cap.
        store.load_model(SAMPLE_MODEL_YAML, dedup=False)
        store.load_model(SAMPLE_MODEL_YAML, dedup=False)
        with pytest.raises(ModelCapacityError, match="Maximum"):
            store.load_model(SAMPLE_MODEL_YAML, dedup=False)

    def test_model_cap_frees_after_remove(self) -> None:
        mgr = SessionManager(
            ttl_seconds=3600,
            max_age_seconds=86400,
            max_models_per_session=1,
            cleanup_interval=9999,
        )
        info = mgr.create_session()
        store = mgr.get_store(info.session_id)

        from tests.conftest import SAMPLE_MODEL_YAML

        result = store.load_model(SAMPLE_MODEL_YAML, dedup=False)
        with pytest.raises(ModelCapacityError):
            store.load_model(SAMPLE_MODEL_YAML, dedup=False)
        store.remove_model(result.model_id)
        store.load_model(SAMPLE_MODEL_YAML, dedup=False)  # should succeed now

    def test_model_cap_enforced_under_concurrency(self) -> None:
        """Concurrent load_model() calls must not exceed the cap."""
        mgr = SessionManager(
            ttl_seconds=3600,
            max_age_seconds=86400,
            max_models_per_session=1,
            cleanup_interval=9999,
        )
        info = mgr.create_session()
        store = mgr.get_store(info.session_id)

        from tests.conftest import SAMPLE_MODEL_YAML

        results: list[object] = []

        def _load() -> object:
            try:
                # dedup=False so each request actually competes for a model slot.
                return store.load_model(SAMPLE_MODEL_YAML, dedup=False)
            except (ModelCapacityError, ModelValidationError) as exc:  # noqa: UP038
                return exc

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_load) for _ in range(4)]
            results = [f.result() for f in futures]

        successes = [
            r for r in results if not isinstance(r, ModelCapacityError | ModelValidationError)
        ]
        assert len(successes) <= 1, f"Expected at most 1 success, got {len(successes)}"


class TestDefaultSession:
    def test_get_or_create_default(self, session_manager: SessionManager) -> None:
        store1 = session_manager.get_or_create_default()
        store2 = session_manager.get_or_create_default()
        assert store1 is store2

    def test_default_not_in_list(self, session_manager: SessionManager) -> None:
        session_manager.get_or_create_default()
        assert session_manager.list_sessions() == []

    def test_default_session_purged_when_not_single_model_mode(self) -> None:
        """Default session is purged when not in single-model mode."""
        mgr = SessionManager(
            ttl_seconds=0,
            max_age_seconds=86400,
            cleanup_interval=9999,
            is_single_model_mode=False,
        )
        mgr.get_or_create_default()
        time.sleep(0.05)
        mgr._purge_expired()
        # Default session should have been purged
        # get_or_create_default creates a new one
        store = mgr.get_or_create_default()
        assert store.list_models() == []  # fresh store

    def test_default_session_kept_in_single_model_mode(self) -> None:
        """Default session is NOT purged in single-model mode."""
        mgr = SessionManager(
            ttl_seconds=0,
            max_age_seconds=0,
            cleanup_interval=9999,
            is_single_model_mode=True,
        )
        store = mgr.get_or_create_default()

        from tests.conftest import SAMPLE_MODEL_YAML

        store.load_model(SAMPLE_MODEL_YAML)
        time.sleep(0.05)
        mgr._purge_expired()
        # Default session should still be alive with its model
        store2 = mgr.get_or_create_default()
        assert store is store2
        assert len(store2.list_models()) == 1


class TestCleanup:
    def test_purge_expired(self) -> None:
        mgr = SessionManager(ttl_seconds=0, max_age_seconds=86400, cleanup_interval=9999)
        mgr.create_session()
        mgr.create_session()
        time.sleep(0.05)
        mgr._purge_expired()
        assert mgr.active_count == 0

    def test_purge_max_age_expired(self) -> None:
        mgr = SessionManager(ttl_seconds=3600, max_age_seconds=0, cleanup_interval=9999)
        mgr.create_session()
        time.sleep(0.05)
        mgr._purge_expired()
        assert mgr.active_count == 0

    def test_cleanup_thread(self) -> None:
        mgr = SessionManager(
            ttl_seconds=0,
            max_age_seconds=86400,
            cleanup_interval=0.05,  # type: ignore[arg-type]
        )
        mgr.start()
        try:
            mgr.create_session()
            time.sleep(0.2)  # wait for cleanup to run
            assert mgr.active_count == 0
        finally:
            mgr.stop()


class TestThreadSafety:
    def test_concurrent_creates(self, session_manager: SessionManager) -> None:
        def create() -> str:
            info = session_manager.create_session()
            return info.session_id

        with ThreadPoolExecutor(max_workers=10) as pool:
            ids = list(pool.map(lambda _: create(), range(50)))

        assert len(set(ids)) == 50
        assert session_manager.active_count == 50


class TestSessionIsolation:
    def test_stores_are_independent(self, session_manager: SessionManager) -> None:
        """Models loaded in one session are not visible in another."""
        from tests.conftest import SAMPLE_MODEL_YAML

        info_a = session_manager.create_session()
        info_b = session_manager.create_session()

        store_a = session_manager.get_store(info_a.session_id)
        store_b = session_manager.get_store(info_b.session_id)

        store_a.load_model(SAMPLE_MODEL_YAML)

        assert len(store_a.list_models()) == 1
        assert len(store_b.list_models()) == 0


class TestCrossSessionModelCache:
    def test_identical_model_shared_across_sessions(self, session_manager: SessionManager) -> None:
        """Two sessions loading identical OBML share one compiled model + id."""
        from tests.conftest import SAMPLE_MODEL_YAML

        info_a = session_manager.create_session()
        info_b = session_manager.create_session()
        store_a = session_manager.get_store(info_a.session_id)
        store_b = session_manager.get_store(info_b.session_id)

        ra = store_a.load_model(SAMPLE_MODEL_YAML)
        rb = store_b.load_model(SAMPLE_MODEL_YAML)

        assert ra.model_id == rb.model_id
        assert store_a.get_model(ra.model_id) is store_b.get_model(rb.model_id)

    def test_closing_one_session_keeps_shared_model_for_the_other(
        self, session_manager: SessionManager
    ) -> None:
        from tests.conftest import SAMPLE_MODEL_YAML

        info_a = session_manager.create_session()
        info_b = session_manager.create_session()
        store_a = session_manager.get_store(info_a.session_id)
        store_b = session_manager.get_store(info_b.session_id)
        ra = store_a.load_model(SAMPLE_MODEL_YAML)
        store_b.load_model(SAMPLE_MODEL_YAML)

        cache = session_manager._model_cache
        assert cache.stats()["refs"] == 2

        # Closing one session releases its reference; the model survives.
        session_manager.close_session(info_a.session_id)
        assert cache.stats()["entries"] == 1
        assert store_b.get_model(ra.model_id) is not None

        # Closing the last session evicts the shared model.
        session_manager.close_session(info_b.session_id)
        assert cache.stats()["entries"] == 0
