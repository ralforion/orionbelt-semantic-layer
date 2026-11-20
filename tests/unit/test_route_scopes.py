"""Every /v1 endpoint has a reviewed scope (design/PLAN_authentication.md §5)."""

from __future__ import annotations

from fastapi.routing import iter_route_contexts

from orionbelt.api.app import create_app
from orionbelt.api.route_scopes import ROUTE_SCOPES, endpoint_key, required_scope
from orionbelt.auth import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE
from orionbelt.settings import Settings

# Under /v1 but outside the authenticated router: checks its own token.
_SELF_AUTHENTICATED = {"heartbeat.post_heartbeat"}


def _v1_endpoint_keys() -> set[str]:
    app = create_app(settings=Settings())
    return {
        endpoint_key(context.endpoint)
        for context in iter_route_contexts(app.routes)
        if context.path.startswith("/v1/")
    }


def test_every_v1_endpoint_has_a_scope() -> None:
    missing = _v1_endpoint_keys() - _SELF_AUTHENTICATED - ROUTE_SCOPES.keys()
    assert not missing, f"/v1 endpoints without a scope in api/route_scopes.py: {sorted(missing)}"


def test_no_scope_for_an_endpoint_that_does_not_exist() -> None:
    stale = ROUTE_SCOPES.keys() - _v1_endpoint_keys()
    assert not stale, f"api/route_scopes.py lists endpoints that no longer exist: {sorted(stale)}"


def test_scopes_are_from_the_vocabulary() -> None:
    assert set(ROUTE_SCOPES.values()) <= {SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN}


def test_unlisted_endpoint_requires_admin() -> None:
    def not_in_the_table() -> None: ...

    assert required_scope(not_in_the_table) == SCOPE_ADMIN
    assert required_scope(None) == SCOPE_ADMIN


def test_decisions_that_matter() -> None:
    # Loading a model, directly or through oneshot, is write.
    assert ROUTE_SCOPES["sessions.load_model"] == SCOPE_WRITE
    assert ROUTE_SCOPES["sessions.load_model_from_osi"] == SCOPE_WRITE
    assert ROUTE_SCOPES["oneshot.oneshot_batch"] == SCOPE_WRITE
    # A read-only user can start a session and query it.
    assert ROUTE_SCOPES["sessions.create_session"] == SCOPE_READ
    assert ROUTE_SCOPES["sessions.execute_query"] == SCOPE_READ
    # The caches are admin.
    assert ROUTE_SCOPES["cache_stats.clear_cache"] == SCOPE_ADMIN
