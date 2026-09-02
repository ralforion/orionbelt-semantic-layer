"""OBML YAML query detection.

Determines whether a query string is an OBML semantic query or plain SQL.
"""

from __future__ import annotations

from typing import Any

import yaml


def is_obml(query: str) -> bool:
    """Return True if *query* is an OBML YAML query (starts with ``select:`` and
    contains ``dimensions`` or ``measures``).
    """
    stripped = query.strip()
    if not stripped.lower().startswith("select:"):
        return False
    try:
        parsed = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return False
    if not isinstance(parsed, dict):
        return False
    # YAML preserves key case — normalise to lowercase for lookup
    lower_keys = {k.lower() if isinstance(k, str) else k: v for k, v in parsed.items()}
    select = lower_keys.get("select", {})
    if not isinstance(select, dict):
        return False
    return "dimensions" in select or "measures" in select


def parse_obml(query: str) -> dict[str, Any]:
    """Parse an OBML YAML string into a dict. Call only after :func:`is_obml`."""
    parsed: Any = yaml.safe_load(query.strip())
    if not isinstance(parsed, dict):
        raise ValueError("OBML must parse to a mapping")
    return parsed
