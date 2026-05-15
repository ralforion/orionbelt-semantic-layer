"""Detect non-deterministic SQL — same query, different answer each run.

The result cache (``cache/key.py``) hashes entries on the compiled SQL
string. That makes the cache content-addressable only when the SQL is
deterministic. Two classes of non-determinism violate the invariant:

1. **Random / sampling**: ``RAND()``, ``UUID()``, ``TABLESAMPLE`` —
   different rows every run.
2. **Clock reads**: ``NOW()``, ``CURRENT_DATE`` — same SQL produces a
   different answer over time. ``WHERE date >= CURRENT_DATE - 7`` is
   the canonical trap: cache the first run today, serve the same stale
   "last 7 days" tomorrow.

This module's :func:`is_nondeterministic_sql` lets the cache layer
return ``cache_meta=None`` for these queries, so they execute against
the warehouse every time. The detector is intentionally conservative
— only flags known patterns; unknown function names pass.
"""

from __future__ import annotations

import re

# Function names whose call form (NAME(...)) makes the query non-deterministic.
# Names are upper-cased for case-insensitive matching.
_NONDETERMINISTIC_FNS: frozenset[str] = frozenset(
    {
        # --- Random / UUID ---
        "RAND",
        "RANDOM",
        "RANDOMUUID",
        "UUID",
        "UUID_STRING",
        "NEWID",
        "GEN_RANDOM_UUID",
        "GENERATEUUIDV4",
        "RANDOMBYTES",
        "RANDOM_BYTES",
        # --- Clock reads (function form) ---
        "NOW",
        "CURRENT_TIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "LOCALTIME",
        "LOCALTIMESTAMP",
        "GETDATE",
        "GETUTCDATE",
        "SYSDATE",
        "SYSTIMESTAMP",
        "TODAY",
        "UNIX_TIMESTAMP",
    }
)

# Keywords that read the clock without parentheses (standard SQL allows both
# ``CURRENT_DATE`` and ``CURRENT_DATE()``). These are reserved in the SQL
# standard, so they can only appear as keywords — not as unquoted identifiers.
_BARE_CLOCK_KEYWORDS: frozenset[str] = frozenset(
    {
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "CURRENT_TIME",
        "LOCALTIME",
        "LOCALTIMESTAMP",
        "SYSDATE",
    }
)

# Function-call pattern: NAME followed by (. Matched after string/identifier
# literals are stripped, so legitimate column names don't false-positive.
_FN_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Bare-keyword pattern: standalone clock-reading keywords.
_BARE_KEYWORD_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_BARE_CLOCK_KEYWORDS)) + r")\b",
    re.IGNORECASE,
)

# Sampling clauses: SQL standard ``TABLESAMPLE`` only. ClickHouse's bare
# ``FROM t SAMPLE 0.1`` slips through — documented limitation.
_SAMPLE_RE = re.compile(r"\bTABLESAMPLE\b", re.IGNORECASE)


def _strip_literals(sql: str) -> str:
    """Remove single-quoted strings and double-quoted identifiers.

    Single quotes (``'...'``) are SQL string literals across all 8
    supported dialects. Double quotes (``"..."``) are identifier quoting
    in standard SQL / Postgres / Snowflake / DuckDB — and a column named
    ``"NOW"`` must not trigger the detector. Stripping both keeps the
    scan focused on actual SQL keywords and function calls.

    Handles SQL's doubled-quote escape convention (``''`` inside a
    string, ``""`` inside an identifier).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if sql[i] == quote:
                    # Doubled quote = escaped — skip both, stay inside literal.
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                # Backslash escape (MySQL, ClickHouse, BigQuery — and harmless
                # on dialects that don't honour it inside the stripped span).
                if sql[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def is_nondeterministic_sql(sql: str) -> tuple[bool, str | None]:
    """Return ``(True, name)`` if the SQL is non-deterministic.

    The returned ``name`` is the offending function or keyword (upper-
    case), suitable for logging. Returns ``(False, None)`` on
    deterministic SQL.

    Conservative by design: only flags entries in :data:`_NONDETERMINISTIC_FNS`,
    :data:`_BARE_CLOCK_KEYWORDS`, or matching :data:`_SAMPLE_RE`.
    """
    if not sql or not sql.strip():
        return False, None

    cleaned = _strip_literals(sql)

    # 1. Function-call form: NAME(...)
    for match in _FN_CALL_RE.finditer(cleaned):
        name = match.group(1).upper()
        if name in _NONDETERMINISTIC_FNS:
            return True, name

    # 2. Bare keyword form: standalone clock identifiers.
    bare = _BARE_KEYWORD_RE.search(cleaned)
    if bare:
        return True, bare.group(0).upper()

    # 3. Sampling clauses.
    sample = _SAMPLE_RE.search(cleaned)
    if sample:
        return True, sample.group(0).upper()

    return False, None


__all__ = ["is_nondeterministic_sql"]
