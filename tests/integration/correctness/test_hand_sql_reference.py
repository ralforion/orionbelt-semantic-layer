"""Tier 1 §3.3 — hand-authored reference SQL.

Each entry in ``corpus.yaml`` that declares a ``handSql:`` block is run
through the OBSL compiler against the bundled DuckDB seed, and the
result rows are compared to the rows produced by executing the
corresponding hand-written SQL file under ``reference_sql/`` against the
same seed. A planner bug (wrong join path, dropped row, fan-trap,
time-grain misuse) shows up as a row-set diff.

Comparison rules (per plan §10.1):
  * Sums / counts compared as ``Decimal`` exact equality, no float tolerance.
  * Ratios may use ``pytest.approx`` — none of the corpus rows in this
    file produce ratios.
  * Row order is normalized by sorting on ``handSql.sortKeys`` before
    comparison; OBSL does not guarantee output order without ``ORDER BY``.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb", reason="duckdb required for correctness tests")

from orionbelt.models.query import QueryObject  # noqa: E402

from ._corpus import CORPUS, CorpusEntry  # noqa: E402

# Only entries with a hand-SQL ratifier participate in this test.
_HAND_SQL_ENTRIES: list[CorpusEntry] = [e for e in CORPUS if e.hand_sql is not None]


def _to_decimal(v: Any) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    raise TypeError(f"Cannot coerce {type(v).__name__} to Decimal: {v!r}")


def _normalize_value(v: Any) -> Any:
    """Make values comparable across OBSL output and hand-SQL output.

    DuckDB returns ``decimal(18, 2)`` columns as ``Decimal`` regardless of
    whether the SUM was emitted by OBSL or by the reference SQL, so no
    coercion is needed there. Date columns may be returned as ``date`` or
    ``datetime`` depending on the upstream cast — normalize to ``date``.
    """
    if v is None:
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    return v


def _normalize_rows(rows: list[dict[str, Any]], sort_keys: list[str]) -> list[dict[str, Any]]:
    norm = [{k: _normalize_value(v) for k, v in r.items()} for r in rows]
    if sort_keys:
        norm.sort(key=lambda r: tuple(("" if r[k] is None else str(r[k])) for k in sort_keys))
    return norm


def _execute_ref_sql(conn: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


@pytest.mark.parametrize("entry", _HAND_SQL_ENTRIES, ids=lambda e: e.id)
def test_obsl_matches_hand_sql(
    entry: CorpusEntry,
    run_query: Callable[[QueryObject], list[dict[str, Any]]],
    commerce_db: duckdb.DuckDBPyConnection,
) -> None:
    """OBSL-compiled rows must equal the hand-written reference SQL output."""
    spec = entry.hand_sql
    assert spec is not None  # filtered above; satisfies type checker
    ref_path = spec.ref_path
    assert ref_path.exists(), f"Missing reference SQL: {ref_path}"
    ref_sql = ref_path.read_text(encoding="utf-8")

    obsl_rows = run_query(entry.query)
    ref_rows = _execute_ref_sql(commerce_db, ref_sql)

    obsl_norm = _normalize_rows(obsl_rows, spec.sort_keys)
    ref_norm = _normalize_rows(ref_rows, spec.sort_keys)

    # Same row count first — the cheaper, more diagnosable failure.
    assert len(obsl_norm) == len(ref_norm), (
        f"Row count differs: OBSL produced {len(obsl_norm)}, "
        f"hand-SQL produced {len(ref_norm)}. "
        f"This usually means the planner picked a different join cardinality "
        f"(e.g. INNER vs LEFT) — check the OBSL SQL for INNER JOINs."
    )

    # Same column set.
    if obsl_norm:
        obsl_cols = set(obsl_norm[0].keys())
        ref_cols = set(ref_norm[0].keys())
        assert obsl_cols == ref_cols, (
            f"Column sets differ: only-in-OBSL={obsl_cols - ref_cols}, "
            f"only-in-ref={ref_cols - obsl_cols}"
        )

    # Strict per-row, per-column equality. Decimal comparison is exact;
    # date/string/int comparisons use Python ``==``.
    for i, (a, b) in enumerate(zip(obsl_norm, ref_norm, strict=True)):
        for col in a:
            av = a[col]
            bv = b[col]
            if isinstance(av, Decimal) or isinstance(bv, Decimal):
                assert _to_decimal(av) == _to_decimal(bv), (
                    f"Row {i} col {col!r}: OBSL={av} != ref={bv}. "
                    f"Suggests a measure aggregation or join cardinality bug."
                )
            else:
                assert av == bv, f"Row {i} col {col!r}: OBSL={av!r} != ref={bv!r}."
