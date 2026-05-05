"""Cache key construction.

See ``design/PLAN_freshness_driven_cache.md`` §14. Keys are server-internal —
callers do not see them. The construction is deterministic so the same query
under the same session/model/dialect always hashes to the same key.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

KEY_VERSION = 1


def _normalize(value: Any) -> Any:
    """Recursively canonicalize a query payload for hashing.

    - Sort dict keys.
    - Sort `select.dimensions` and `select.measures` lists.
    - Sort `where` filter list by ``(field, op, str(value))``.
    - For set-semantic ops (``in``, ``not in``), sort the value list.
    - Leave ``order_by`` untouched (order matters semantically).
    - Leave free-form expression strings opaque.
    """
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


_SET_OPS = frozenset({"in", "not in", "in_list", "not_in_list"})


def _normalize_query(query: Any) -> Any:
    """Apply the syntactic normalization rules from PLAN §14."""
    if not isinstance(query, dict):
        return _normalize(query)

    out = _normalize(query)
    select = out.get("select")
    if isinstance(select, dict):
        for k in ("dimensions", "measures"):
            v = select.get(k)
            if isinstance(v, list):
                select[k] = sorted(v, key=lambda x: json.dumps(x, sort_keys=True))

    where = out.get("where")
    if isinstance(where, list):
        normalized_where = []
        for f in where:
            if isinstance(f, dict):
                op = str(f.get("op", "")).lower()
                if op in _SET_OPS:
                    val = f.get("value") or f.get("values")
                    if isinstance(val, list):
                        sorted_val = sorted(val, key=lambda x: json.dumps(x, sort_keys=True))
                        if "value" in f:
                            f["value"] = sorted_val
                        elif "values" in f:
                            f["values"] = sorted_val
            normalized_where.append(f)
        normalized_where.sort(
            key=lambda f: (
                str(f.get("field", "")) if isinstance(f, dict) else "",
                str(f.get("op", "")) if isinstance(f, dict) else "",
                json.dumps(f.get("value") if isinstance(f, dict) else f, sort_keys=True),
            )
        )
        out["where"] = normalized_where

    return out


def build_cache_key(
    *,
    session_id: str,
    model_id: str,
    dialect: str,
    query: Any,
) -> str:
    """Compute the deterministic 32-char cache key for a query."""
    canonical = json.dumps(
        {
            "v": KEY_VERSION,
            "session_id": session_id,
            "model_id": model_id,
            "dialect": dialect,
            "query": _normalize_query(query),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:32]


def query_hash(query: Any) -> str:
    """Hash of the normalized query alone (for diagnostics / dedup)."""
    payload = json.dumps(
        _normalize_query(query),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
