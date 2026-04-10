# Changelog

All notable changes to OrionBelt Semantic Layer are documented here.

## [1.3.0] - 2026-04-10

### Added

- **OBSL-Core 0.1 RDF graph export** — every loaded model is exported as an RDF graph (Turtle) using the OBSL vocabulary at `https://ralforion.com/ns/obsl#`. Graph is built eagerly at model load time and cached alongside the `SemanticModel`.
- **SPARQL query API** — read-only `SELECT` and `ASK` queries against the OBSL graph via `POST /v1/sessions/{id}/models/{mid}/sparql` and the `/v1/sparql` shortcut. Update operations (`INSERT`, `DELETE`, `LOAD`, `DROP`) are rejected with HTTP 400.
- **Graph endpoint** — `GET /v1/sessions/{id}/models/{mid}/graph` and `/v1/graph` shortcut return the OBSL graph as `text/turtle`.
- **OWL axioms in OBSL-Core** — disjointness, functional properties, and inverse properties added to `ontology/obsl.ttl`.
- **Extended metric profile** — OBSL vocabulary split into core and extended metric classes (`CumulativeMetric`, `PeriodOverPeriodMetric`) with dedicated properties.
- **`obsl:synonym` property** — replaces SKOS alignment; synonyms are now first-class in the OBSL vocabulary.
- **OBSL Turtle download button** — Gradio UI ER diagram tab now exposes a button to download the loaded model's OBSL graph as `.ttl`.
- **OBML reference endpoint** — `GET /v1/reference/obml` returns the OBML reference documentation as structured JSON.
- **OBSL guide page** — new `docs/guide/obsl.md` walks through graph retrieval, SPARQL queries (SELECT/ASK), and the OBSL vocabulary.

### Fixed

- **Colab notebook Mermaid rendering** — switched from client-side mermaid.js CDN (blocked by Colab's sandboxed output iframe CSP) to server-rendered SVG via `mermaid.ink`.
- **Colab notebook zombie subprocesses** — added explicit port cleanup (`lsof -ti tcp:8099` + SIGKILL) before starting a fresh uvicorn subprocess; previous runs left stale listeners holding the port.
- **Colab notebook model loading** — replaced unreliable `MODEL_FILE` env var with explicit `POST /v1/sessions` + `POST /v1/sessions/{id}/models` from the notebook.

### Removed

- **Dead code** — removed unused `load_model_directory` method, `_cleanup_session` helper, and unreferenced `ErrorResponse` Pydantic model (47 lines total, identified via ruff + vulture).

### Changed

- Version bumped to 1.3.0
- Ontology directory renamed from `OBSL/` to `ontology/`
- SHACL shapes updated to match OBSL-Core 0.1 vocabulary
- Ruff format applied to `obsl/exporter.py`, `obsl/sparql.py`, `api/schemas.py`, `ui/app.py`, `tests/unit/test_obsl.py`

---

## [1.2.2] - 2026-03-28

### Fixed

- **Flight info stale after auto-detection** — refresh cached deps (flight_info, query_execute_enabled) after ob_flight auto-detection so /v1/settings and query gating reflect actual runtime state
- **Shortcut 409 in single-model mode** — return __default__ session immediately in _resolve_single_model() and _resolve_store_and_model() when single-model mode is active, avoiding false 409 Conflict after creating a second session
- **Test failures without optional packages** — add pytest skip guards to TestMapTypeCode (ob_driver_core) and TestExecuteSql (ob_flight) so default test suite passes on standard install
- **Validate shortcut not stateless** — remove session dependency from POST /v1/validate; create a fresh ModelStore for validation since it only needs YAML parsing

### Changed

- Version bumped to 1.2.2

---

## [1.2.1] - 2026-03-27

### Fixed

- **Reversed join ON clauses** — swap columns when traversing join edges in reverse direction (CR-01)
- **Empty join column crash** — reject empty `columnsFrom`/`columnsTo` in validator; guard `build_join_condition` (CR-02)
- **Default session purge** — skip `__default__` session in TTL cleanup so single-model mode survives idle periods (CR-03)
- **Assert in production** — replace `assert` with structured `ResolutionError`/`SemanticError` in PoP and cumulative metric resolution (CR-04)
- **SQL injection via table refs** — quote all `format_table_ref` components across 7 dialect implementations (CR-05)
- **Filter value injection** — validate `QueryFilter.value` rejects arbitrary nested objects (CR-06)
- **TOCTOU race in shortcuts** — handle session expiry between `list_sessions` and `get_store` (CR-07)
- **Duplicate YAML keys** — reject duplicate keys at parse time via `allow_duplicate_keys = False` (CR-08)
- **Recursion on large models** — convert DFS cycle detection to iterative with explicit stack (CR-09)
- **Silent PoP fallback** — raise error for unknown comparison types instead of defaulting to percent change (CR-10)
- **AVG total fallback** — use `Literal(1)` instead of invalid column reference in edge case (CR-11)

### Changed

- Version bumped to 1.2.1
- Published 11 packages to PyPI: `orionbelt-semantic-layer` + 10 driver packages

---

## [1.2.0] - 2026-03-25

### Added

- **MySQL dialect** — full SQL generation support for MySQL (8th dialect), plus `ob-mysql` PEP 249 driver
- **Cumulative metrics** — running total, rolling window, and grain-to-date aggregations via `cumulative` metric type
- **Period-over-period (PoP) metrics** — 4-CTE date spine architecture for comparing current vs prior periods
- **Filtered measures** — CASE WHEN wrapping for measures with inline filters, plus ratio metrics
- **Integration tests** — DuckDB, PostgreSQL, MySQL, and ClickHouse tests via testcontainers; `ob-*` PEP 249 driver tests against real databases
- **UnsupportedAggregationError** — dialect limitations exposed in API response when an aggregation is not supported
- **OSI converter** — cumulative and period-over-period metric support for OSI ↔ OBML roundtrip

### Changed

- Dialect count increased from 7 to 8 (added MySQL)
- Version bumped to 1.2.0

---

## [1.1.0] - 2026-03-17

### Added

- **DB-API 2.0 drivers** — PEP 249 drivers for all 7 databases: `ob-postgres`, `ob-clickhouse`, `ob-duckdb`, `ob-databricks`, `ob-snowflake`, `ob-dremio`, `ob-bigquery`
- **Arrow Flight SQL** — query execution endpoint via Arrow Flight SQL server, with execute support across all 7 database drivers
- **Query execution endpoint** — `POST /v1/sessions/{id}/query/execute` compiles and runs queries (requires database connection)
- **TPC-H quickstart notebook** — Jupyter notebook with TPC-H model, Docker Hub badges, and interactive examples
- **`description` property** — optional description metadata on all OBML model objects, mapped in OSI converter
- **Filter groups** — `AND`/`OR`/`NOT` compound filter expressions in query WHERE clauses
- **Qualified WHERE filters** — `DataObject.Column` references in WHERE filters with auto-join
- **CFL optimization** — skip NULL padding for dialects supporting `UNION ALL BY NAME` (Snowflake, DuckDB)
- **OSI roundtrip** — preserve OBML-only properties in `custom_extensions` for lossless OSI ↔ OBML conversion
- **Split SQL/Explain UI** — side-by-side SQL and explain panel with detailed CFL leg explanations

### Changed

- `QUERY_EXECUTE` decoupled from `FLIGHT_ENABLED` — REST query execution works without Arrow Flight
- `ob_flight` uses lazy imports to avoid `pyarrow.flight` dependency when using DB-API drivers only
- OBML validator relaxed: `database` and `schema` now optional on data objects
- Version bumped to 1.1.0

### Fixed

- Reversed join path swapping columns incorrectly in JoinGraph
- CFL not triggering for expression-based measures
- Execute endpoint hang with DuckDB dbgen data duplication
- Swagger UI blank page (missing `unsafe-inline` in docs CSP)

---

## [1.0.0] - 2026-03-16

### Added

- **BigQuery dialect** — full SQL generation support for Google BigQuery
- **DuckDB dialect** — full SQL generation support for DuckDB/MotherDuck (uses `UNION ALL BY NAME`)
- **Model discovery API** — 10 new endpoints for exploring models programmatically:
  - `GET /v1/sessions/{id}/models/{mid}/schema` — full model structure as JSON
  - `GET /v1/sessions/{id}/models/{mid}/dimensions` — list/get dimensions
  - `GET /v1/sessions/{id}/models/{mid}/measures` — list/get measures
  - `GET /v1/sessions/{id}/models/{mid}/metrics` — list/get metrics
  - `GET /v1/sessions/{id}/models/{mid}/explain/{name}` — lineage explain
  - `POST /v1/sessions/{id}/models/{mid}/find` — search artefacts by name/synonym
  - `GET /v1/sessions/{id}/models/{mid}/join-graph` — join graph adjacency list
- **Top-level shortcuts** — auto-resolving endpoints (`/v1/schema`, `/v1/dimensions`, etc.) when only one session/model exists
- **Query explain** — compilation response now includes `explain` with reasoning for planner choice, base object selection, and each join decision
- **`owner` field** — optional owner/responsible-party metadata on all OBML objects (model, data objects, columns, dimensions, measures, metrics)
- **API versioning** — all routes prefixed with `/v1/` (except `/health` and `/robots.txt`)
- **BSL 1.1 license** — Business Source License with Apache 2.0 conversion on 2030-03-16
- **GitHub Actions CI** — automated test, lint, and type-check on every push and PR

### Changed

- Dialect count increased from 5 to 7 (added BigQuery and DuckDB)
- MCP server moved to separate repository ([orionbelt-semantic-layer-mcp](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp))
- Version bumped to 1.0.0

### Migration from 0.8.x

**Breaking: API route prefix**

All API routes now require a `/v1/` prefix. Update your client URLs:

| Before (0.8.x)                  | After (1.0.0)                      |
| ------------------------------- | ---------------------------------- |
| `POST /sessions`                | `POST /v1/sessions`                |
| `POST /sessions/{id}/models`    | `POST /v1/sessions/{id}/models`    |
| `POST /sessions/{id}/query/sql` | `POST /v1/sessions/{id}/query/sql` |
| `GET /dialects`                 | `GET /v1/dialects`                 |
| `POST /convert/osi-to-obml`     | `POST /v1/convert/osi-to-obml`     |

The `/health` endpoint remains at the root (no prefix).

**New: `explain` in query response**

`POST /v1/sessions/{id}/query/sql` now returns an `explain` object alongside `sql`. Existing clients can safely ignore it.

**New: `owner` in OBML YAML**

The `owner` field is optional on all OBML objects. Existing models without `owner` continue to work unchanged.
