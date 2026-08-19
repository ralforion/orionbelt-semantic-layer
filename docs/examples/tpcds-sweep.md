---
description: "40 TPC-DS queries written as OBSL and checked row-by-row against the benchmark's own reference answers on DuckDB and ClickHouse."
---

# TPC-DS: checked against the reference answers

A semantic layer is easy to believe when it returns *a* number. This page is
about returning the *right* one.

40 queries from [TPC-DS](https://www.tpc.org/tpcds/) are written as OBSL query
files against one OBML model, compiled to SQL, executed, and compared
**row-by-row** with the benchmark's own reference SQL. Not totals, not row
counts: every cell of every row.

The queries are in `examples/tpcds_queries/`, the model is
`examples/tpcds.obml.yml`, and nothing in either names an engine.

## Results

| Engine | Scale | Exact | Known reference-variant differences | Unexplained |
|---|---|---|---|---|
| DuckDB | sf=1 | **39 / 40** | Q99 | none |
| ClickHouse | sf=10 | **37 / 40** | Q20, Q40, Q98 | none |

Every one of the 80 comparisons is accounted for.

A "known reference-variant difference" is a case where the *reference query*
differs between engines, chased to ground and recorded rather than waved at:

- **Q99** — DuckDB's variant wraps `cc_name` in `LOWER()` and ClickHouse's does
  not. Every numeric column matches; only the case of one string differs, and
  Q99 matches exactly on ClickHouse.
- **Q20, Q98** — ClickHouse's decimal division truncates to the operand scale,
  so the *reference* yields `0.42` where the true value is `0.4254`. All rows
  and all other columns match, and OBSL's value is the more accurate one.
- **Q40** — a `COALESCE(..., 0)` in the model, added so an empty filtered
  measure reads as 0 on DuckDB, is wrong on ClickHouse, where the filtered
  measure already yields 0 rather than NULL.

## How the comparison works

This is the part worth stating plainly, because "we ran TPC-DS" can mean almost
anything.

**Same database, same data, both queries.** The OBSL-compiled SQL and the
reference SQL execute against the *same* tables in the *same* engine in the same
run. There is no cross-engine or cross-scale comparison anywhere: a match means
two result sets are identical, not that two totals happen to agree.

**Where the reference comes from** differs per engine, and that is deliberate —
each engine is checked against the reference its own ecosystem publishes:

| Engine | Data | Reference SQL |
|---|---|---|
| DuckDB | `CALL dsdgen(sf=1)` from DuckDB's `tpcds` extension | The same extension's `tpcds_queries()` table function — all 99 official queries |
| ClickHouse | a local `tpcds` database at sf=10 | The ClickHouse-adapted query set, read from a directory given by `TPCDS_CLICKHOUSE_REF_DIR` |

**What is normalised, and what is not.** Rows are sorted before comparison,
because neither side is required to return them in the same order unless the
query says so. Numbers are rounded to a per-query number of decimals — 2 for
money, 4 where a ratio is compared. Where the reference projects the same
count three times under different names, or orders its columns differently from
the way the planner groups them, the comparison maps the columns; each such
mapping is a line in `sweep.py` with a comment saying why. Nothing else is
adjusted: no tolerance on values, no dropping of rows.

## Running it

```bash
# DuckDB. Needs examples/tpcds_queries/tpcds_sf1.duckdb, built once:
#   python -c "import duckdb; c=duckdb.connect('tpcds_sf1.duckdb'); \
#              c.execute('INSTALL tpcds; LOAD tpcds; CALL dsdgen(sf=1)')"
uv run python examples/tpcds_queries/sweep.py --dialect duckdb

# One query, or a few
uv run python examples/tpcds_queries/sweep.py --dialect duckdb Q53 Q63

# The compiled SQL for every query, no database needed
uv run python examples/tpcds_queries/sweep.py --dialect duckdb --dump
```

Every query runs on a connection of its own and writes its verdict to
`results/<dialect>/<label>.json` — match, row counts, the first differing row,
and how long it took. The whole DuckDB sweep is a few seconds of wall time; the
ClickHouse one at sf=10 is a long coffee, and the per-query files are what let
you see where it went.

`--jobs` sets the concurrency, and the default differs per engine because the
two are limited by different things. DuckDB takes 8: the sweep is a few seconds
of work against a local file. ClickHouse takes **1**, and that is not caution.
At sf=10 its heavy tail is memory-bound rather than CPU-bound - three of those
queries in flight together asked for 25 GB on one server, spilled, and turned
Q69 from a few minutes into 52. Running them concurrently makes the sweep
slower, so it runs them one at a time.

The compiled SQL for both engines is committed under
`examples/tpcds_queries/sql/`, so a change's effect on all 40 queries is visible
in a diff without a database.

## What it takes to express them

TPC-DS is not a star-schema benchmark. The queries that took real work are the
ones whose shape a semantic layer usually cannot reach:

| Shape | Queries | How OBML expresses it |
|---|---|---|
| A window function over an aggregate | Q20, Q53, Q63, Q65, Q98 | A `grain` override on a measure, which compiles to `AGG(x) OVER (PARTITION BY ...)` in a wrapper over the grouped CTE |
| Filtering on that windowed value | Q53, Q63, Q65 | The predicate is hoisted past the window rather than applied inside the CTE |
| Hierarchical subtotals | Q22, Q27 | `GROUP BY ROLLUP` with `GROUPING()` flag columns |
| A seven-day pivot | Q43 | Seven filtered measures |
| Eight correlated subqueries in one scan | Q88 | Eight filtered measures over one query |
| Two facts at different grains | Q40, Q83 | The CFL planner's `UNION ALL` legs |
| Nested boolean groups in `WHERE` | Q15, Q34, Q73, Q79 | Query-level `OR` groups mixing dimension lists and raw column predicates |

## What is not expressible yet

Four queries sit in `examples/tpcds_queries/drafts/` and are **not** counted
above. They compile, but as approximations of the reference rather than
equivalents of it:

| Q | What it needs |
|---|---|
| Q31 | The same aggregate at three quarter offsets side by side in one row. Period-over-period metrics give the comparison row-wise, not pivoted into columns |
| Q49, Q70 | Rank-then-top-N. Q70 additionally wants `rank()` partitioned by `GROUPING()` output, which ROLLUP does not expose as a partition key |
| Q66 | Around 36 filtered measures over a web + catalog union, needing a date join on both facts. Expressible in principle; not built |

Counting them as passes would be the easy thing to do and would make this page
worth less.

## History

An earlier sweep in April 2026 verified 3 of the original 29 queries. The sweep
after it, in August, reached 29 of 30 on DuckDB and recorded five compiler gaps
that were blocking the rest — chief among them that a `HAVING` on a value
produced by a window wrapper was compiled *inside* the CTE, against the
pre-window aggregate, silently returning the wrong rows.

All five are now fixed, which is most of the distance between 29 and 39: Q19,
Q53, Q63, Q65, Q68, Q72 and Q83 were blocked or partially blocked then and match
exactly now.
