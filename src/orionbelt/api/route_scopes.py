"""The scope each ``/v1`` endpoint requires.

One explicit entry per endpoint, keyed ``<router module>.<function name>``,
so the access level of every endpoint is a reviewed decision in one place.
``tests/unit/test_route_scopes.py`` fails when an endpoint is missing from
the table or the table names one that no longer exists. An endpoint that
is somehow not listed requires ``obsl:admin`` (fail closed).

- ``obsl:read``: read models, compile and run queries, and create your own
  session (in ``MODEL_FILES`` mode a new session starts with the curated
  model, which is how the UI begins).
- ``obsl:write``: put a model into a session, remove one, close a session,
  and ``/v1/oneshot/batch``, which runs a model sent with the request.
- ``obsl:admin``: the result and compilation caches.

``AUTH_MODE=none`` and ``api_key`` callers hold all three scopes, so the
table only restricts OIDC users. See design/PLAN_authentication.md §5.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orionbelt.auth import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE

_ROUTERS_PACKAGE = "orionbelt.api.routers."

ROUTE_SCOPES: dict[str, str] = {
    # cache_stats
    "cache_stats.clear_cache": SCOPE_ADMIN,
    "cache_stats.clear_compilation_cache": SCOPE_ADMIN,
    "cache_stats.get_cache_stats": SCOPE_ADMIN,
    "cache_stats.get_compilation_cache_stats": SCOPE_ADMIN,
    "cache_stats.trigger_cache_sweep": SCOPE_ADMIN,
    # composables
    "composables.composables_for_anchors": SCOPE_READ,
    "composables.composables_for_query": SCOPE_READ,
    # convert (stateless: nothing is loaded or run)
    "convert.obml_to_osi": SCOPE_READ,
    "convert.osi_to_obml": SCOPE_READ,
    # datasources
    "datasources.get_datasource_schemas": SCOPE_READ,
    "datasources.get_schema_tables": SCOPE_READ,
    "datasources.list_datasources": SCOPE_READ,
    # dialects
    "dialects.list_dialects": SCOPE_READ,
    # graph
    "graph.get_graph": SCOPE_READ,
    "graph.sparql_query": SCOPE_READ,
    # lineage (``_lineage`` serves the dimension, measure, metric and rule routes)
    "lineage._lineage": SCOPE_READ,
    "lineage.post_query_lineage": SCOPE_READ,
    # model_api
    "model_api.explain_artefact": SCOPE_READ,
    "model_api.find_artefacts": SCOPE_READ,
    "model_api.get_dimension": SCOPE_READ,
    "model_api.get_example": SCOPE_READ,
    "model_api.get_gaps": SCOPE_READ,
    "model_api.get_join_graph": SCOPE_READ,
    "model_api.get_measure": SCOPE_READ,
    "model_api.get_metric": SCOPE_READ,
    "model_api.get_schema": SCOPE_READ,
    "model_api.list_concept_mappings": SCOPE_READ,
    "model_api.list_concept_namespaces": SCOPE_READ,
    "model_api.list_dimensions": SCOPE_READ,
    "model_api.list_examples": SCOPE_READ,
    "model_api.list_measures": SCOPE_READ,
    "model_api.list_metrics": SCOPE_READ,
    "model_api.list_unmapped_objects": SCOPE_READ,
    "model_api.search_dimension_values": SCOPE_READ,
    # models
    "models.get_model": SCOPE_READ,
    "models.list_models": SCOPE_READ,
    # oneshot (loads and runs the model sent with the request)
    "oneshot.oneshot_batch": SCOPE_WRITE,
    # reference
    "reference.get_function_reference": SCOPE_READ,
    "reference.get_obml_reference": SCOPE_READ,
    "reference.get_obsql_reference": SCOPE_READ,
    "reference.get_schema": SCOPE_READ,
    "reference.list_references": SCOPE_READ,
    # rules
    "rules.compile_all_rules": SCOPE_READ,
    "rules.compile_rule": SCOPE_READ,
    "rules.evaluate_all_rules": SCOPE_READ,
    "rules.evaluate_rule": SCOPE_READ,
    "rules.get_rule": SCOPE_READ,
    "rules.list_rules": SCOPE_READ,
    # sessions
    "sessions.close_session": SCOPE_WRITE,
    "sessions.compile_query": SCOPE_READ,
    "sessions.compile_semantic_ql": SCOPE_READ,
    "sessions.create_session": SCOPE_READ,
    "sessions.describe_model": SCOPE_READ,
    "sessions.execute_query": SCOPE_READ,
    "sessions.execute_semantic_ql": SCOPE_READ,
    "sessions.export_model_to_osi": SCOPE_READ,
    "sessions.get_session": SCOPE_READ,
    "sessions.list_models": SCOPE_READ,
    "sessions.list_sessions": SCOPE_READ,
    "sessions.load_model": SCOPE_WRITE,
    "sessions.load_model_from_osi": SCOPE_WRITE,
    "sessions.model_diagram_er": SCOPE_READ,
    "sessions.plan_query": SCOPE_READ,
    "sessions.remove_model": SCOPE_WRITE,
    "sessions.validate_model": SCOPE_READ,
    # settings
    "settings.get_settings": SCOPE_READ,
    # shortcuts (each resolves to the single model the caller can see)
    "shortcuts._lineage": SCOPE_READ,
    "shortcuts.shortcut_compile_all_rules": SCOPE_READ,
    "shortcuts.shortcut_compile_query": SCOPE_READ,
    "shortcuts.shortcut_compile_rule": SCOPE_READ,
    "shortcuts.shortcut_compile_semantic_ql": SCOPE_READ,
    "shortcuts.shortcut_composables_for_anchors": SCOPE_READ,
    "shortcuts.shortcut_composables_for_query": SCOPE_READ,
    "shortcuts.shortcut_concept_mappings": SCOPE_READ,
    "shortcuts.shortcut_concept_namespaces": SCOPE_READ,
    "shortcuts.shortcut_diagram_er": SCOPE_READ,
    "shortcuts.shortcut_dimension": SCOPE_READ,
    "shortcuts.shortcut_dimensions": SCOPE_READ,
    "shortcuts.shortcut_evaluate_all_rules": SCOPE_READ,
    "shortcuts.shortcut_evaluate_rule": SCOPE_READ,
    "shortcuts.shortcut_execute_query": SCOPE_READ,
    "shortcuts.shortcut_execute_semantic_ql": SCOPE_READ,
    "shortcuts.shortcut_explain": SCOPE_READ,
    "shortcuts.shortcut_find": SCOPE_READ,
    "shortcuts.shortcut_gaps": SCOPE_READ,
    "shortcuts.shortcut_get_example": SCOPE_READ,
    "shortcuts.shortcut_graph": SCOPE_READ,
    "shortcuts.shortcut_join_graph": SCOPE_READ,
    "shortcuts.shortcut_list_examples": SCOPE_READ,
    "shortcuts.shortcut_measure": SCOPE_READ,
    "shortcuts.shortcut_measures": SCOPE_READ,
    "shortcuts.shortcut_metric": SCOPE_READ,
    "shortcuts.shortcut_metrics": SCOPE_READ,
    "shortcuts.shortcut_plan_query": SCOPE_READ,
    "shortcuts.shortcut_query_lineage": SCOPE_READ,
    "shortcuts.shortcut_rule": SCOPE_READ,
    "shortcuts.shortcut_rules": SCOPE_READ,
    "shortcuts.shortcut_schema": SCOPE_READ,
    "shortcuts.shortcut_search_values": SCOPE_READ,
    "shortcuts.shortcut_sparql": SCOPE_READ,
    "shortcuts.shortcut_unmapped_objects": SCOPE_READ,
    "shortcuts.shortcut_validate": SCOPE_READ,
    # how authentication is set up
    "auth_status.get_auth_status": SCOPE_ADMIN,
    # tokens (BI tokens; admin actions checked again in the handler)
    "tokens.create_token": SCOPE_READ,
    "tokens.consent_status": SCOPE_READ,
    "tokens.start_consent": SCOPE_READ,
    "tokens.list_tokens": SCOPE_READ,
    "tokens.revoke_owner_tokens": SCOPE_ADMIN,
    "tokens.revoke_token": SCOPE_READ,
}


def endpoint_key(endpoint: Callable[..., Any]) -> str:
    """Return the table key of a route endpoint: ``<router module>.<function>``."""
    return f"{endpoint.__module__.removeprefix(_ROUTERS_PACKAGE)}.{endpoint.__name__}"


def required_scope(endpoint: Callable[..., Any] | None) -> str:
    """Return the scope ``endpoint`` requires; ``obsl:admin`` when it is not listed."""
    if endpoint is None:
        return SCOPE_ADMIN
    return ROUTE_SCOPES.get(endpoint_key(endpoint), SCOPE_ADMIN)
