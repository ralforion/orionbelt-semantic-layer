# Correctness & Drift Tests

OBSL ships a two-tier integration suite for the compiler. **Tier 1 —
correctness** answers *"is this query producing the right answer?"*
**Tier 2 — drift** answers *"is this query producing the same answer it
did yesterday?"* Each tier catches the bugs the other can't, and they
are designed to run together so the cheap regression check (drift) only
locks in values that have been independently ratified (correctness).

The full design rationale lives in `design/PLAN_correctness_and_drift_tests.md`
(an internal-only working note — `design/` is gitignored). This page is
the operator's manual.

## TL;DR

| Goal | Command |
|------|---------|
| Fast PR check | `uv run pytest tests/integration/drift/` |
| Full correctness ratification | `uv run pytest tests/integration/correctness/` |
| Both at once | `uv run pytest tests/integration/correctness/ tests/integration/drift/` |
| Re-snap after intentional SQL change | `UPDATE_SNAPSHOTS=1 uv run pytest tests/integration/drift/` |

All commands run in well under a second against the bundled DuckDB seed
(`examples/orionbelt_1_commerce.duckdb`).

## Why two tiers

A snapshot test (golden test, recorded test, approval test) only ever
asserts *"same as last time"*. If the snapshot was captured while the
query was already wrong, it locks the wrong answer in. Every future run
reports green and the bug is invisible until production catches it.

OBSL avoids this by capturing snapshots *only after* a separate,
independent verification that the value is correct. A snapshot is the
*cache* of a correctness proof, never the proof itself.

```
Tier 1 — correctness (independent verification)
  │
  │   for each query, compute the answer two independent ways
  │   and assert numerical equality
  │
  ▼
Tier 2 — drift (snapshot regression)
  │
  │   capture the SQL + sorted rows of every ratified query
  │   diff future runs against the captured value
```

## Tier 1 — correctness checks

Every correctness test computes a query result two ways: once via the
OBSL compiler and once via an *independent* method that does not
exercise the same compiler code path. The methods used in the v0 corpus:

| Method | What it catches | Test file |
|--------|-----------------|-----------|
| **Aggregation invariance** — ungrouped == sum of grouped | grain-leak, dim joining wrong fact | `test_aggregation_invariance.py` |
| **Hierarchical rollup** — coarse grain == sum of fine grain | time-grain composition bugs | `test_hierarchical_rollup.py` |
| **Hand-authored reference SQL** — SQL written by reading the schema directly | join-path, role-playing dim, fan-trap | `test_hand_sql_reference.py` |
| **Pandas baseline** — same algorithm rebuilt in pandas | window-frame, cumulative, rolling, period-over-period | `test_pandas_baseline.py` |
| **Metric algebra** — recompute the formula from base measures | expression-evaluation drift, NULLIF, decimal cast | `test_metric_algebra.py` |
| **CFL split** — combined cross-fact == per-fact queries combined | CFL leg corruption, NULL-padding mistakes | `test_cfl_split.py` |
| **Filter additivity** — `IN (a,b)` == `=a` + `=b` | WHERE-clause routing in CFL legs | `test_filter_additivity.py` |

All tests live under `tests/integration/correctness/`. The bundled
DuckDB seed and the matching OBML model
(`examples/orionbelt_1_commerce.yaml`) are the source of truth for every
correctness ratification.

### Adding a new correctness test

1. Pick at least two methods from the table above. Document the choice
   in the test docstring so future readers can audit the cross-check.
2. Use the `run_query` fixture (`tests/integration/correctness/conftest.py`)
   — it compiles a `QueryObject` through the full pipeline, executes
   against the seed, and returns rows as `list[dict]` with `Decimal`
   values preserved.
3. Use `assert_decimal_equal` (or `pytest.approx` for ratios/averages
   only) to compare. Sums must use exact equality per plan §10.1.

## Tier 2 — drift snapshots

Once a query passes its Tier 1 check, the harness captures:

* the compiled SQL string, plus
* the canonical-sorted result rows,

into a YAML file under `tests/integration/drift/duckdb/<query_id>.yaml`.
A pointer to the ratifying Tier 1 test is recorded as
`last_verified_by:`; the metadata gate
(`tests/integration/drift/test_snapshot_metadata.py`) verifies every
pointer resolves to a real, collectable pytest test.

Per-dialect SQL strings (no execution, just compile-and-store) live
under `tests/integration/drift/compile_only/<dialect>/<query_id>.sql`.
These catch dialect-specific emit drift — `DECIMAL` → `NUMBER` for
Snowflake, `BOOLEAN` representation differences, `LISTAGG` vs
`STRING_AGG` — without needing a live database for any vendor other
than DuckDB.

### When drift fails

The failure message looks like:

```
FAILED tests/integration/drift/test_drift_duckdb.py::test_drift_duckdb_exec[02_total_sales_by_country]

Snapshot drift detected (tests/integration/drift/duckdb/02_total_sales_by_country.yaml).
Last verified by tier-1: tests/integration/correctness/test_hand_sql_reference.py::test_obsl_matches_hand_sql[total_sales_by_country].
Diverged fields: sql.
If this change is intentional, re-snap with:
  UPDATE_SNAPSHOTS=1 uv run pytest tests/integration/drift/duckdb/02_total_sales_by_country.yaml
```

The `last verified by` line is the trust signal: as long as that Tier 1
test still passes in the same workflow, the underlying correctness still
holds and you are looking at a downstream change in SQL emission. If the
Tier 1 test *also* fails, do not re-snap — fix the compiler.

### Re-snapping safely

```bash
# 1. confirm Tier 1 still passes for the affected query
uv run pytest tests/integration/correctness/

# 2. re-snap drift artefacts (only the failing ones, ideally)
UPDATE_SNAPSHOTS=1 uv run pytest \
    tests/integration/drift/test_drift_duckdb.py::test_drift_duckdb_exec[02_total_sales_by_country]

# 3. verify the new snapshot is green
uv run pytest tests/integration/drift/

# 4. inspect the diff before committing
git diff tests/integration/drift/
```

CI runs Tier 1 + Tier 2 in a single pytest invocation, so a green
workflow implies every snapshot is anchored to a green Tier 1 check by
construction.

## Corpus structure

The v0 corpus is split across two locations under
`tests/integration/correctness/` to keep query files faithful to the
OBML schema:

```
correctness/
├── corpus.yaml              ← test-rig manifest (id, description,
│                              lastVerifiedBy, optional handSql block)
├── queries/                 ← pure-OBML query files
│   ├── 01_total_sales.yaml
│   ├── 02_total_sales_by_country.yaml
│   └── ...                  ← each file is exactly the body you would
│                              POST to /v1/query/sql
└── reference_sql/           ← hand-written SQL ratifiers (§3.3)
    └── *.sql
```

Each entry in `corpus.yaml` references a query file under `queries/<id>.yaml`
by ID convention. The manifest carries everything that doesn't belong in
the OBML schema (the ratification pointer, sort keys for hand-SQL row
comparison, etc.).

## Adding a new corpus query

1. Drop a pure-OBML YAML file under `tests/integration/correctness/queries/<id>.yaml`.
   It is exactly the payload a user would POST to `/v1/query/sql` — you can
   copy/paste between the two.
2. Add a Tier 1 test in `tests/integration/correctness/` using one or
   two of the methods listed above (or rely on an existing one if the
   query is already covered by, e.g., aggregation invariance).
3. Append a manifest entry to `corpus.yaml`:
   ```yaml
   - id: <id>                            # matches the queries/ filename
     description: <one-line summary>
     lastVerifiedBy: tests/integration/correctness/...::<test_node_id>
     handSql:                            # optional, only for §3.3 cases
       refFile: <name>.sql               # under reference_sql/
       sortKeys: [<dim cols>]
   ```
4. Generate the snapshot:
   ```bash
   UPDATE_SNAPSHOTS=1 uv run pytest \
       tests/integration/drift/test_drift_duckdb.py \
       tests/integration/drift/test_drift_compile_only.py \
       -k <id>
   ```
5. Re-run without `UPDATE_SNAPSHOTS` to confirm the new snapshot is
   green, and commit the query YAML, the manifest entry, and the
   generated drift artefacts in the same change.

## Multi-vendor execution (opt-in)

Compile-only snapshots cover all 8 registered dialects out of the box.
Vendor-execution snapshots — running each query against a real
warehouse and asserting on rows — are not yet wired in v0; see
plan §5.2 for the design.

## Limitations

* The cumulative-metric snapshot captures OBSL's current emit, which
  does **not** cast the windowed output back to the metric's declared
  `dataType` (e.g. `decimal(18, 2)`). Float drift in the snapshot is
  expected; a precision-hardening fix is a separate work item.
* Vendor-side row execution (Postgres / Snowflake / BigQuery) is not in
  v0. Compile-only drift is the only multi-vendor coverage today.
