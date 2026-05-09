"""Snapshot save/load harness for the Tier 2 drift suite.

Tier 2 captures the compiled SQL and (for execution snapshots) the result
rows of each test query. Subsequent runs diff against the captured value
and fail loudly on mismatch. The snapshot is only as trustworthy as the
most recent Tier 1 correctness check that ratified it — see
``design/PLAN_correctness_and_drift_tests.md`` §2.2 and §7.2.

Snapshot YAML files live in:
    tests/integration/drift/duckdb/<query_id>.yaml          # exec snapshot
    tests/integration/drift/compile_only/<dialect>/<id>.sql # SQL string

To re-snap after an intentional change, run:
    UPDATE_SNAPSHOTS=1 uv run pytest tests/integration/drift/

CI must refuse to merge a re-snap PR unless the matching Tier 1 test for
the query has run green in the same workflow (§2.2). That gate is wired
in CI, not here — this conftest only manages the on-disk format.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

DRIFT_DIR = Path(__file__).resolve().parent
DUCKDB_SNAP_DIR = DRIFT_DIR / "duckdb"
COMPILE_ONLY_DIR = DRIFT_DIR / "compile_only"


def _update_mode() -> bool:
    return os.environ.get("UPDATE_SNAPSHOTS", "").lower() in ("1", "true", "yes")


def _normalize_value(v: Any) -> Any:
    """Make a result value YAML-friendly and sort-stable.

    Decimals become strings (preserves precision; YAML round-trips faithfully).
    Dates / datetimes become ISO strings. Everything else is left as-is.
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _normalize_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Canonical-sort rows for stable diffs.

    Convert each row dict to a list keyed by the sorted column order of the
    first row, then sort the row list lexicographically. This produces a
    deterministic, dialect-agnostic byte sequence.
    """
    if not rows:
        return []
    cols = list(rows[0].keys())
    ordered = [[_normalize_value(r[c]) for c in cols] for r in rows]
    ordered.sort(key=lambda row: [("" if x is None else str(x)) for x in row])
    return ordered


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(
            data,
            fh,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=120,
        )


def assert_exec_snapshot(
    query_id: str,
    *,
    sql: str,
    rows: list[dict[str, Any]],
    last_verified_by: str,
) -> None:
    """Diff a DuckDB execution snapshot against ``drift/duckdb/<id>.yaml``.

    ``last_verified_by`` is the dotted path of the Tier 1 test that
    ratified this snapshot (§4.1 ``Last verified by`` pointer). Stored
    in-file so a future drift failure shows the trust signal directly.

    Raises ``AssertionError`` on mismatch when ``UPDATE_SNAPSHOTS`` is unset;
    rewrites the file otherwise.
    """
    path = DUCKDB_SNAP_DIR / f"{query_id}.yaml"
    actual = {
        "last_verified_by": last_verified_by,
        "sql": sql.rstrip() + "\n",
        "rows": _normalize_rows(rows),
    }

    if _update_mode() or not path.exists():
        _dump_yaml(path, actual)
        if not _update_mode():
            pytest.skip(
                f"Created new snapshot {path.relative_to(DRIFT_DIR.parent.parent)}; "
                "rerun without UPDATE_SNAPSHOTS to verify."
            )
        return

    expected = _load_yaml(path)
    mismatches = []
    expected_sql = str(expected.get("sql") or "")
    actual_sql = str(actual["sql"])
    if expected_sql.strip() != actual_sql.strip():
        mismatches.append("sql")
    if expected.get("rows") != actual["rows"]:
        mismatches.append("rows")

    if mismatches:
        rel = path.relative_to(DRIFT_DIR.parent.parent)
        verified = expected.get("last_verified_by", "(unknown)")
        raise AssertionError(
            f"Snapshot drift detected ({rel}). "
            f"Last verified by tier-1: {verified}. "
            f"Diverged fields: {', '.join(mismatches)}. "
            f"If this change is intentional, re-snap with:\n"
            f"  UPDATE_SNAPSHOTS=1 uv run pytest {rel}"
        )


def assert_compile_only_snapshot(
    query_id: str,
    *,
    dialect: str,
    sql: str,
) -> None:
    """Diff a per-dialect compile-only SQL snapshot.

    Path: ``drift/compile_only/<dialect>/<query_id>.sql`` — literally the
    rendered SQL string. Catches dialect-specific emit drift without
    needing a live database.
    """
    path = COMPILE_ONLY_DIR / dialect / f"{query_id}.sql"
    actual = sql.rstrip() + "\n"

    if _update_mode() or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        if not _update_mode():
            pytest.skip(
                f"Created new compile-only snapshot for {dialect}/{query_id}; "
                "rerun without UPDATE_SNAPSHOTS to verify."
            )
        return

    expected = path.read_text(encoding="utf-8")
    if expected.strip() != actual.strip():
        rel = path.relative_to(DRIFT_DIR.parent.parent)
        raise AssertionError(
            f"Compile-only SQL drift for dialect={dialect}, query={query_id} ({rel}). "
            f"If intentional, re-snap with:\n  UPDATE_SNAPSHOTS=1 uv run pytest {rel}"
        )
