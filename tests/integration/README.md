# `tests/integration/` — quick reference

This directory holds OBSL's integration suites. Each subdirectory is a
distinct flavour of test; all of them run via `uv run pytest`.

For the full operator manual see
[`docs/guide/correctness-and-drift-tests.md`](../../docs/guide/correctness-and-drift-tests.md).

## What lives here

| Path | What it does | When it runs |
|---|---|---|
| `correctness/` | **Tier 1** — ratifies query results via independent computation paths (aggregation invariance, hand-SQL reference, pandas baseline, metric algebra, CFL split, filter additivity). | Every CI run. ~1 s. |
| `drift/duckdb/` | **Tier 2** — DuckDB execution snapshots: compiled SQL + canonical-sorted rows per corpus query. | Every CI run. ~0.5 s. |
| `drift/compile_only/<dialect>/` | **Tier 2** — per-dialect SQL strings. Catches dialect-specific emit drift (`DECIMAL` → `NUMBER` for Snowflake, etc.) without needing a live DB. | Every CI run. ~0.2 s. |
| `drift/test_snapshot_metadata.py` | Validates every snapshot's `last_verified_by:` pointer is a real, collectable pytest test. | Every CI run. ~0.5 s. |
| `drift/vendor_exec/` | **Phase A** — runs each corpus query against DuckDB / Postgres / MySQL / ClickHouse via testcontainers and diffs each result row set against the DuckDB golden. | Opt-in (`pytest -m docker`). ~20 s including container startup. |
| `test_*_execution.py` (legacy, sample model) | Earlier per-vendor smoke tests against the small `sales_model` fixture. Pre-dates the corpus framework. | Opt-in (`pytest -m docker`). |
| `test_api*.py`, `test_cache*.py`, `test_oneshot_batch.py`, etc. | API / service-layer integration tests (FastAPI via `httpx.ASGITransport`). | Every CI run. |

## Common workflows

```bash
# Fast feedback loop on a PR (Tier 2 only, no DB needed for compile-only):
uv run pytest tests/integration/drift/

# Full Tier 1 + Tier 2 sweep — what CI runs:
uv run pytest tests/integration/correctness/ tests/integration/drift/

# Just one corpus query across every dialect (compile-only + DuckDB exec):
uv run pytest tests/integration/drift/ -k "07_total_sales_by_client_with_complaints"

# Re-snap drift artefacts after an intentional SQL change:
UPDATE_SNAPSHOTS=1 uv run pytest tests/integration/drift/

# Vendor-execution sweep — full, all four engines:
uv run pytest -m docker tests/integration/drift/vendor_exec/

# Vendor-execution sweep — one vendor only:
uv run pytest -m docker tests/integration/drift/vendor_exec/test_vendor_exec.py::test_postgres_vendor_exec

# Single corpus query, one vendor (handy when iterating on a planner fix):
uv run pytest -m docker tests/integration/drift/vendor_exec/ -k "test_postgres_vendor_exec[07_"
```

## Adding a new corpus query

1. Drop a pure-OBML query YAML at `correctness/queries/<id>.yaml`. It
   is exactly the body you would `POST /v1/query/sql`.
2. Add a Tier 1 ratifier in `correctness/test_*.py` using one or two
   methods from the matrix in the operator guide (or rely on an
   existing one if the query is already covered by, e.g.,
   aggregation invariance).
3. Append a manifest entry to `correctness/corpus.yaml`:
   ```yaml
   - id: <id>                            # matches queries/<id>.yaml
     description: <one-line summary>
     lastVerifiedBy: tests/integration/correctness/...::<test_node_id>
     handSql:                            # optional — only for hand-SQL ratifiers
       refFile: <name>.sql               # under reference_sql/
       sortKeys: [<dim cols>]
   ```
4. Generate the snapshots:
   ```bash
   UPDATE_SNAPSHOTS=1 uv run pytest \
       tests/integration/drift/test_drift_duckdb.py \
       tests/integration/drift/test_drift_compile_only.py \
       -k <id>
   ```
5. Re-run without `UPDATE_SNAPSHOTS` to confirm green, then commit
   the query YAML, manifest entry, and generated drift artefacts in
   the same change.

## Markers

| Marker | Default behaviour | Run with |
|---|---|---|
| (none) | runs every test | `uv run pytest` |
| `docker` | skipped — needs Docker / testcontainers | `pytest -m docker` |
| `adbc` | skipped — needs `OB_PG_URI` pointing at a real Postgres | `pytest -m adbc` |

The skip is implemented in the top-level
[`tests/conftest.py`](../conftest.py).

## When tests fail

* **Tier 1 fails** → real correctness bug in OBSL's compiler or in
  the query model. Do *not* re-snap drift artefacts; fix the
  compiler.
* **Tier 2 (drift) fails, Tier 1 still passes** → SQL emission
  changed but the underlying answer is still correct. Inspect the
  diff and re-snap if intentional.
* **Vendor-execution fails on one engine** → either a real
  dialect-specific emit bug or an acceptable engine-rounding variance.
  See `_KNOWN_ISSUES` at the top of `test_vendor_exec.py` for the
  triage rules and add an entry there if the divergence is
  expected-but-tracked.

For the full design rationale and triage flowchart see the operator
guide.
