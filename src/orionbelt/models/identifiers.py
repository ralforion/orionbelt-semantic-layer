"""Identifier normalization + validation for multi-model addressing.

Used by both the OBML ``name:`` field validator and the multi-model
startup loop (which derives names from filenames when ``name:`` is unset).
Same rules apply to both paths so the resolved model addressing key is
predictable regardless of source.

Public API:

* :func:`normalize_model_name` — applies the normalization pipeline and
  returns the cleaned identifier. Raises :class:`ModelNameError` if the
  result is empty, too long, starts with a non-letter, contains chars
  outside ``[a-z0-9_]``, or matches a reserved name.

* :class:`ModelNameError` — typed error for the API / startup paths so
  callers can surface a precise message naming both the source and the
  intermediate normalization state.
"""

from __future__ import annotations

import re

_SEP_RUN = re.compile(r"[\s\.\-]+")
"""Runs of whitespace, dots, or dashes — all collapse to a single underscore."""

_UNDERSCORE_RUN = re.compile(r"_+")
"""Runs of underscores (after the first pass) — collapsed to one underscore."""

_VALID_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
"""Final validation: must start with a letter, then letters/digits/underscores,
1-63 chars total. Matches PostgreSQL/Snowflake/BigQuery identifier conventions
so the model name works in URLs, gRPC metadata, and SQL identifiers without
quoting."""

RESERVED_NAMES: frozenset[str] = frozenset(
    {
        # Internal session ID
        "__default__",
        # OBSL brand acronyms — would confuse routing and docs
        "obsl",
        "obml",
        "obsql",
        "model",
        # PostgreSQL default databases
        "default",
        "public",
        # System schemas across vendors
        "information_schema",
        "pg_catalog",
        "sqlite_master",
        "mysql",
        "sys",
        "performance_schema",
        # Common DBA / utility schemas
        "admin",
        "root",
    }
)
"""Names that would collide with internal OBSL surfaces or with vendor system
schemas. Rejected at startup with a clear error pointing at this list."""


class ModelNameError(ValueError):
    """Raised when an OBML ``name:`` value or filename stem cannot be
    normalized into a valid model identifier.

    The message describes what was tried, what the intermediate state
    looked like, and (when possible) suggests a fix.
    """


def normalize_model_name(raw: str, *, source: str | None = None) -> str:
    """Normalize a free-form name into the canonical addressing identifier.

    Pipeline (per ``design/PLAN_flight_natural_sql.md`` v2.4.0 multi-model
    spec):

    1. lowercase
    2. replace runs of ``[whitespace | . | -]`` with a single underscore
    3. collapse runs of underscores to one underscore
    4. strip leading/trailing underscores
    5. strip a single trailing ``_obml`` suffix (courtesy for the
       ``commerce.obml.yaml`` filename convention)
    6. validate against ``^[a-z][a-z0-9_]{0,62}$``
    7. check against the reserved-name list

    ``source`` is an optional human-readable description of where the raw
    name came from (e.g. ``"filename 'My-Model.yaml'"`` or ``"OBML name:
    field in sales.yaml"``) — included in error messages.
    """
    if raw is None:
        raise ModelNameError(_msg(source, "(no source)", "name is None"))

    original = raw
    src = source or f"'{original}'"

    # 1. lowercase
    name = original.lower()
    # 2. replace separator runs with a single underscore
    name = _SEP_RUN.sub("_", name)
    # 3. collapse underscore runs
    name = _UNDERSCORE_RUN.sub("_", name)
    # 4. strip leading/trailing underscores
    name = name.strip("_")
    # 5. strip a single trailing _obml
    if name.endswith("_obml"):
        name = name[: -len("_obml")]
        name = name.strip("_")  # in case stripping created a new boundary

    # 6. validate
    if not name:
        raise ModelNameError(
            _msg(
                src,
                original,
                "normalization produced an empty name. The input must contain "
                "at least one letter or digit (after stripping spaces, dots, "
                "dashes, underscores, and a trailing `_obml` suffix).",
            )
        )
    if not _VALID_NAME.match(name):
        reason = _identifier_reason(name)
        raise ModelNameError(
            _msg(
                src,
                original,
                f"normalized to '{name}' which is not a valid identifier "
                f"({reason}). Required pattern: must start with a letter, "
                "then letters / digits / underscores only, max 63 chars.",
            )
        )
    if name in RESERVED_NAMES:
        raise ModelNameError(
            _msg(
                src,
                original,
                f"normalized to '{name}', which is reserved. "
                f"Reserved names: {', '.join(sorted(RESERVED_NAMES))}. "
                "Choose a different name.",
            )
        )

    return name


def _identifier_reason(name: str) -> str:
    """Human-readable reason a normalized name still fails the regex."""
    if len(name) > 63:
        return f"length {len(name)} exceeds 63 chars"
    if not name[0].isalpha() or not name[0].isascii():
        return f"first character must be a-z, got '{name[0]}'"
    bad = sorted({c for c in name if not (c.isalnum() and c.isascii()) and c != "_"})
    if bad:
        return f"contains disallowed characters: {', '.join(repr(c) for c in bad)}"
    return "fails validation pattern"


def _msg(source: str, original: str, problem: str) -> str:
    """Format a consistent ModelNameError message."""
    return f"Model name '{original}' (source: {source}) is invalid: {problem}"
