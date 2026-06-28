"""Cache key construction.

See ``design/PLAN_freshness_driven_cache.md`` §14. Keys are server-internal —
callers do not see them. The construction is deterministic so the same query
under the same datasource/model/dialect always hashes to the same key.

v2 (2026-05): keys are computed from the **compiled SQL string**, not the
QueryObject. Single key shape covers every input path that converges on
compiled SQL (OBSQL, QueryObject, OBML YAML) and the hash trivially
matches what the warehouse executes. The compiler is deterministic, so
identical inputs still produce identical keys.

v3 (2026-06): keys are scoped to the **data source**, not the session. The
result of a compiled SQL string depends only on which physical connection it
runs against, not on which session issued it. Connections are global per
dialect today (``ob_flight.db_router`` resolves credentials from the
environment by dialect), so every session sharing a dialect shares cache
entries. When per-tenant / SSO connections land the datasource key gains the
principal so tenants stay isolated -- see
``design/PLAN_multi_tenant_connections.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

KEY_VERSION = 3

# Separator for composite datasource keys; an ASCII unit separator that cannot
# appear in a dialect name or a sanitized principal id.
_DS_SEP = "\x1f"


def build_datasource_key(dialect: str, principal: str | None = None) -> str:
    """Identity of the physical data source a query executes against.

    The result cache is shared across everything that resolves to the same
    data source. Today connections are global per dialect, so the identity is
    just the dialect. When per-tenant / SSO connections land (see
    ``design/PLAN_multi_tenant_connections.md``) ``principal`` carries the
    tenant / connection identity, so two principals on the same dialect get
    distinct keys and never read each other's cached rows.
    """
    return dialect if principal is None else f"{dialect}{_DS_SEP}{principal}"


_WHITESPACE_RE = re.compile(r"\s+")

# Match any quoted region — single-quoted string literal, double-quoted
# identifier, or backtick-quoted identifier (MySQL / BigQuery / Databricks).
# Each form supports doubled-quote escaping. Whitespace inside these regions
# is significant and must NOT be collapsed, or two different inputs (e.g.
# ``name = 'A  B'`` vs ``name = 'A B'`` or ``"Order  Id"`` vs ``"Order Id"``)
# would hash identically and serve each other's cached rows.
_QUOTED_RE = re.compile(
    r"'(?:''|[^'])*'"  # 'sql literal' with '' escape
    r"|\"(?:\"\"|[^\"])*\""  # "ansi identifier" with "" escape
    r"|`(?:``|[^`])*`"  # `mysql/bq/databricks identifier`
)


def _normalize_sql(sql: str) -> str:
    """Collapse insignificant SQL formatting variations.

    The compiler is deterministic, but pretty-printing or trailing
    semicolons could vary across paths (REST `format_sql` vs Flight raw
    SQL). Strip trailing whitespace/semicolon and collapse internal
    whitespace runs so equivalent SQL hashes identically — but preserve
    whitespace inside quoted regions verbatim, since it's significant to
    the engine and to the resulting cache key.
    """
    cleaned = sql.strip().rstrip(";").strip()
    parts: list[str] = []
    last = 0
    for m in _QUOTED_RE.finditer(cleaned):
        parts.append(_WHITESPACE_RE.sub(" ", cleaned[last : m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_WHITESPACE_RE.sub(" ", cleaned[last:]))
    return "".join(parts)


def build_cache_key(
    *,
    datasource: str,
    model_id: str,
    dialect: str,
    sql: str | None = None,
    query: Any = None,
) -> str:
    """Compute the deterministic 32-char cache key for a query.

    Scope is the ``datasource`` (see :func:`build_datasource_key`), not the
    session: any session resolving to the same data source, model, dialect and
    compiled SQL shares the entry.

    Pass ``sql`` (compiled SQL string) — that's the canonical input as
    of v2. The legacy ``query`` kwarg is accepted for back-compat with
    callers mid-migration; it gets JSON-canonicalized as a fallback.
    """
    if sql is None and query is None:
        raise ValueError("build_cache_key requires either sql or query")
    body: Any
    if sql is not None:
        body = _normalize_sql(sql)
    else:
        body = json.dumps(query, sort_keys=True, separators=(",", ":"), default=str)

    canonical = json.dumps(
        {
            "v": KEY_VERSION,
            "datasource": datasource,
            "model_id": model_id,
            "dialect": dialect,
            "sql": body,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:32]


def query_hash(sql: str | None = None, query: Any = None) -> str:
    """Short hash of the normalized SQL (for diagnostics / dedup).

    Same arg pattern as :func:`build_cache_key`: prefer ``sql``,
    fall back to ``query`` for legacy callers.
    """
    if sql is not None:
        payload = _normalize_sql(sql).encode("utf-8")
    elif query is not None:
        payload = json.dumps(
            query,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    else:
        raise ValueError("query_hash requires either sql or query")
    return hashlib.sha256(payload).hexdigest()[:16]
