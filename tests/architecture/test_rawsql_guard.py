"""RawSQL containment gate (Phase 7.5).

``RawSQL`` is the dialect escape hatch: a SQL string spliced into the AST
where the typed nodes can't express a fragment (dialect-specific date-spine
generators, period-over-period self-joins, MySQL quarter arithmetic). The
goal is not to ban it but to keep it **narrow and visible** — every site is
enumerated here, carries an inline reason comment at its call site, and is
covered by drift snapshots. A new ``RawSQL`` construction anywhere fails
this test until it is added to the approved inventory.

Counting by module (not line) keeps the gate robust to unrelated edits.
"""

from __future__ import annotations

from collections import Counter

from tests.architecture.inventory import build_inventory

# Approved production ``RawSQL`` construction sites: path -> max count.
# Each corresponding call site carries a ``# RawSQL: <reason>`` comment.
# Lower these as fragments become expressible with typed AST nodes; raising a
# count (or adding a path) is a deliberate, reviewed escape-hatch decision.
APPROVED_RAWSQL: dict[str, int] = {
    # Period-over-period CTEs: date_range, date_spine, pop_base, pop_compare —
    # dialect-specific date-spine SQL the typed AST does not model.
    "src/orionbelt/compiler/pop_wrap.py": 4,
    # MySQL quarter truncation (nested DATE_ADD/MAKEDATE/QUARTER).
    "src/orionbelt/dialect/mysql.py": 1,
    # BigQuery DATE_TRUNC date-part keyword (MONTH/ISOWEEK, not a quoted string).
    "src/orionbelt/dialect/bigquery.py": 1,
}


def test_rawsql_sites_are_approved() -> None:
    inv = build_inventory()
    counts = Counter(site.path for site in inv.raw_sql_sites)

    unexpected_files = sorted(set(counts) - set(APPROVED_RAWSQL))
    assert not unexpected_files, (
        "New RawSQL construction in unapproved file(s): "
        f"{unexpected_files}. RawSQL is the dialect escape hatch — add a "
        "`# RawSQL: <reason>` comment at the call site and an entry in "
        "APPROVED_RAWSQL, or use a typed AST node instead."
    )

    grown = {
        path: (n, APPROVED_RAWSQL[path]) for path, n in counts.items() if n > APPROVED_RAWSQL[path]
    }
    assert not grown, (
        "RawSQL use grew beyond the approved baseline (path: actual vs approved): "
        f"{grown}. Justify and raise APPROVED_RAWSQL, or avoid the new RawSQL."
    )


def test_rawsql_total_does_not_grow() -> None:
    inv = build_inventory()
    total = len(inv.raw_sql_sites)
    baseline = sum(APPROVED_RAWSQL.values())
    assert total <= baseline, (
        f"Total RawSQL construction sites grew to {total} (baseline {baseline}). "
        "RawSQL count must stay stable or decrease."
    )
