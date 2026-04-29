# Changelog

All notable changes to OrionBelt Semantic Layer are documented here.

## [2.1.0] - 2026-04-28

### Added

- **`settings.defaultDialect` on the OBML model.** Optional top-level `settings.defaultDialect` lets a model pin its preferred SQL dialect so callers can omit `dialect` on every `/v1/query/sql` and `/v1/query/execute` request. Resolution chain at request time: explicit `dialect` → `settings.defaultDialect` → `DB_VENDOR` env → `postgres`. Validated against the 8 registered dialects at parse time. The session and shortcut endpoints both honor it; `dialect` on the request body is now `Optional`.
- **`/v1/query/execute` formatted output.** Four new query parameters on both the session-scoped and shortcut execute endpoints:
  - `format=tsv` returns `text/tab-separated-values` with RFC 4180-style quoting for cells containing tab/newline/CR/double-quote. Implies `format_values=true`.
  - `format_values=true` renders numeric cells in the JSON response as locale-aware display strings using each column's `format` pattern (matches the Gradio UI exactly).
  - `locale` (BCP-47) overrides the default locale for thousand/decimal separators; falls back to the new `DEFAULT_LOCALE` env when omitted.
  - `timezone` (IANA TZ) overrides the model's `default_timezone` per-request.
- **Shared formatting module** `service/value_formatting.py`. The UI and the API now use the same `format_number` / `parse_number_format` / `locale_separators` / `format_row` / `to_tsv` helpers, so what you see in Gradio is exactly what `?format_values=true` returns.
- **`DEFAULT_LOCALE` env / `default_locale` setting** (default empty → en-style separators).
- **Raw query mode (`select.fields`).** Returns un-aggregated rows by projecting physical columns directly. Mutually exclusive with `dimensions`/`measures`/`having`/`dimensionsExclude`. New `select.distinct` flag emits `SELECT DISTINCT`. Field references must be qualified `DataObject.Column`. Single-fact queries compile to a flat star-schema-style SELECT; fanout protection still applies (reversed many-to-one joins are rejected).
- **Raw CFL — multi-fact `UNION ALL` with NULL padding.** When `select.fields` references columns from independent fact tables, the planner emits one leg per leg-root fact, with typed `CAST(NULL AS <type>)` for fields not reachable from a given leg. Outer wrapper applies `DISTINCT` (when set), `ORDER BY` (remapped to field aliases), and `LIMIT`. New error codes: `RAW_FIELD_INVALID_REF`, `RAW_FIELD_UNKNOWN_OBJECT`, `RAW_FIELD_UNKNOWN_COLUMN`.
- **`UNION ALL BY NAME` optimization for raw CFL on DuckDB and Snowflake.** On dialects that support it, per-leg NULL padding is skipped — each leg only emits the columns it has, and the database fills missing columns automatically. Output rows are identical; SQL is shorter and more readable.
- **Public-doc gating flags** (`EXPOSE_API_DOCS`, `EXPOSE_OPENAPI_SCHEMA`). Default `true` to preserve the public-demo behaviour. Set `EXPOSE_API_DOCS=false` to hide `/docs` and `/redoc`; `EXPOSE_OPENAPI_SCHEMA` toggles `/openapi.json` independently. The Dockerfile and `deploy-gcloud.sh` pin both to `true` explicitly so the demo stays exposed even if defaults flip later.
- **`/v1/settings` now returns `version` and `api_version`.** Clients can negotiate features from a single call instead of also hitting `/health`.
- **`/v1/settings` exposes the loaded model's `settings:` block plus the timezone and dialect resolution chains.** New optional sub-objects on the response:
  - `model_settings` — every key from the model's `settings:` block (`defaultTimezone`, `defaultDialect`, `overrideDatabaseTimezone`, `defaultNumericDataType`), in OBML camelCase to mirror the YAML.
  - `timezone` — `{model, host, database, effective, override_database_timezone, now, utc, database_detected, database_raw}`. Always present so clients can show the wall clock even without a loaded model. The chain matches what `db_executor.resolve_timezone()` does at execute time: when `overrideDatabaseTimezone` is true the model wins; otherwise the cached DB session timezone (if any) takes priority. **The endpoint now warms the DB session-TZ cache on first hit when a model is bound and `query_execute` is enabled** — so the report runner / UI sees the correct `effective` immediately, instead of falling through to the model TZ until the first query happens to populate the cache. `database_detected` reports whether the probe has run, `database_raw` exposes the cached value for diagnostics. `now` is the current wall-clock time in the effective TZ (ISO 8601 with offset); `utc` is the same instant in UTC for reference.
  - `dialect` — `{model, env, effective}`. `effective` is what the planner uses when a request omits `dialect`: model.defaultDialect → DB_VENDOR → `postgres`. Always present.
- **`/v1/settings` accepts `?session_id=...&model_id=...` to scope the model-specific blocks in multi-model mode.** Resolution: single-model mode → preloaded model; both params → explicit lookup (404 on miss); `session_id` only → auto-pick when that session has exactly one model; no params in multi-model mode → auto-pick if a single model is loaded across all sessions, else the model blocks are omitted (no error). `model_id` without `session_id` → 400.

### Changed

- `Select` AST node gains a `distinct: bool` field; codegen emits `SELECT DISTINCT` when set. `QueryBuilder.distinct()` and a widened `with_cte()` signature support raw CFL composite construction.

## [2.0.1] - 2026-04-27

### Added

- **`/v1/settings` now returns `version` and `api_version`.** Clients can negotiate features from a single call instead of also hitting `/health`. `version` matches the `__version__` constant; `api_version` is the REST URL prefix (`"v1"`).

### Docs

- Reordered `query-language.md`: the **Coalesce (Merging Role-Playing Dimensions)** section now sits between **Time Grain Override** and **Measures** so it reads next to the other dimension subsections.

## [2.0.0] - 2026-04-27

### Breaking

- **Many-to-one joins are now strictly forward-only.** The query planner refuses to walk a `many-to-one` join in reverse (which would silently inflate fact-table row counts). Queries that previously compiled by traversing such a reverse hop now raise `UNREACHABLE_REQUIRED_OBJECT`. **Migration:** declare bridge tables as `many-to-many` (already supported by OBML); see `examples/movies.obml.yml` for the canonical pattern.
- **CFL leg projection now honors per-dimension `via:` waypoints.** Role-playing dimensions (e.g., `Sales Employee` and `Purchase Employee`) no longer leak across UNION ALL legs — each leg projects only its own role and NULL-pads the others. Query results CHANGE for any model that had role-playing dimensions where the previous (incorrect) behavior was being relied on. **Migration:** the new output is the correct one; verify and update downstream code accordingly.
- **PostgreSQL renderer now emits `DECIMAL(p, s)`** instead of `NUMERIC(p, s)`. The two are SQL-standard synonyms in Postgres and every other supported dialect (canonical name in sqlglot is `DECIMAL`). **Migration:** consumers comparing exact SQL strings need an update; query semantics are unchanged.
- **`sqlparse` removed from dependencies.** The UI and API now use sqlglot's pretty-printer for all SQL formatting. Anyone transitively importing `sqlparse` from this project must add it to their own dependencies.

### Added

- **Query-level `coalesce` dimensions.** `select.dimensions` now accepts a `{coalesce: [...], as: <alias>}` group that merges role-playing dimensions into a single output column via `COALESCE(d1, d2, ...)`. ORDER BY may reference the alias directly. Validation: 5 new error codes (`COALESCE_MISSING_ALIAS`, `DUPLICATE_COALESCE_ALIAS`, `COALESCE_ALIAS_COLLISION`, `COALESCE_TOO_FEW_MEMBERS`, `COALESCE_TYPE_MISMATCH`).
- **`primaryKey` column property.** Optional informational marker on data object columns. Renders as `PK` in the Mermaid ER diagram (precedence over `FK`) and emits `obsl:primaryKey true` triples in the OBSL graph. Composite keys: set `primaryKey: true` on multiple columns.
- **`UNREACHABLE_REQUIRED_OBJECT` error.** Resolution-time error raised when a required dimension's source object cannot be reached from the query base via directed joins. Replaces silently-wrong SQL with a clear migration hint.
- **`examples/movies.obml.yml`** — bundled junction-table example (Movies / Directors / Producers with `many-to-many` bridges) demonstrating the recommended OBML pattern for many-to-many relationships.
- **Vertically responsive Gradio UI layout.** SQL Compiler, ER Diagram, and Ontology Graph tabs scale with viewport height via `dvh`-based CSS. Editor and output rows resize fluidly without overflow.
- **Ontology Graph tab.** Interactive vis-network visualization (data objects, dimensions, measures, metrics, joins) with toggleable layers and adjustable node spacing. vis-network v9.1.2 ships as a static asset (no CDN dependency), loaded via base64-encoded iframe srcdoc.
- **API responses now return sqlglot-pretty SQL.** Every `/v1/.../query/sql` and `/v1/.../query/execute` endpoint formats SQL with one expression per line. Consumers (gradio_client, MCP, AI agents, dashboards) get readable SQL by default with no flag required.

### Fixed

- **CFL planner via-aware leg construction** (see Breaking).
- **Join graph reverse-traversal silent fanout** (see Breaking).
- **MISSING_VIA validator** — only warns when a dimension table has direct joins from multiple fact tables, not transitive reachability. Fact-table dimensions (columns on the fact table itself) no longer trigger false warnings.
- **Example model `via` cleanup** — removed unnecessary `via` from dimensions on tables that are only direct children of one fact table (Clients, Countries, Regions) and from fact-table-local dimensions (Sales Date, Payment Type).

### Removed

- `sqlparse` runtime dependency.
- Unused `Graph Height` slider on the Ontology Graph tab (the iframe is now viewport-height driven).

### Security

- New Cloud Armor rules for the public demo block `/ui/gradio_api/info`, `/ui/monitoring/*`, and `/ui/openapi.json` (admin/discovery endpoints not used by the browser UI).
- `main` branch protection enabled on the public repo and the four sibling repos: PR required, force-push and deletion blocked, linear history enforced.

## [1.8.2] - 2026-04-25

_Release notes pending._

## [1.8.1] - 2026-04-24

### Fixed

- **CFL NULL padding type mismatch** — UNION ALL legs now use the source column's `abstractType` for NULL padding instead of the measure's `resultType`. Fixes PostgreSQL `UNION types cannot be matched` errors when COUNT_DISTINCT measures reference string columns.
- **Dropdown pre-selection** — UI picker dropdowns (Dimensions, Measures/Metrics, Columns) no longer auto-select the first value on load, which prevented that value from being selected by the user.

## [1.8.0] - 2026-04-22

### Added

- **Grain override** — per-measure `grain:` property controls aggregation grain independently from query dimensions. Supports `FIXED` (start empty) and `RELATIVE` (inherit query dims) modes with `exclude`, `include`, and `keepOnly` operators. Compiled as `AGG(x) OVER (PARTITION BY ...)` window functions. 40 new tests.
- **Filter context** — per-measure `filterContext:` property controls which query WHERE filters apply. Supports `FIXED` (ignore all) and `RELATIVE` (inherit and modify) modes with `exclude`, `keepOnly`, and structured `include` filters. Compiled as isolated CTEs with LEFT/CROSS JOIN. 59 new tests.
- **Grain & filter context guide** — dedicated MkDocs guide page with OBML syntax, properties, examples (percent of total, percent of parent, unfiltered grand total, selective filter exclusion), and compilation strategy.
- **OBSL ontology update** — 12 new datatype properties: `grainMode`, `grainExclude`, `grainInclude`, `grainKeepOnly`, `filterContextMode`, `filterContextExclude`, `filterContextKeepOnly`, `filterContextInclude`, `owner`, `dataType`, `format`. Exporter emits triples for all new properties across data objects, columns, dimensions, measures, and metrics.
- **OSI converter roundtrip** — `grain` and `filterContext` preserved through OBML → OSI → OBML conversion via `custom_extensions`. 13 new roundtrip tests.

### Changed

- Version bumped to 1.8.0
- `total: true` is now documented as shorthand for `grain: { mode: FIXED }`
- README roadmap: grain & filter context moved from "Planned" to "Shipped"

---

## [1.7.1] - 2026-04-22

### Fixed

- **OSI converter roundtrip** — full preservation for all OBML properties through OSI-to-OBML and OBML-to-OSI conversion: `settings`, `owner`, `dataType`, column metadata (`sqlType`, `sqlPrecision`, `sqlScale`, `numClass`, `comment`), dimension properties (`resultType`, `description`), and metric `format`. 22 new property roundtrip tests.

### Added

- **Favicon** — docs site now has a favicon.

### Changed

- Version bumped to 1.7.1
- Fixed MkDocs Material pinned version

---

## [1.7.0] - 2026-04-20

### Added

- **Data types & numerical precision** — automatic CAST wrapping with dialect-specific type rendering (`NUMERIC`, `NUMBER`, `Decimal`, etc.). Type resolution order: explicit `dataType` → structural inference → model default → built-in default. Precision clamping per dialect.
- **Timezone settings** — `settings.defaultTimezone` (IANA timezone) and `settings.allowUtcFallback` for naive timestamp coercion in query execution results. Resolution chain: model setting → host process TZ → UTC fallback (opt-in).
- **ISO 8601 serialization** — temporal query results use proper offset notation, UTC "Z" suffix, and elide zero microseconds.
- **HAVING on metrics** — HAVING filters now accept metric names (not just measures). Alias expansion to full aggregate expressions ensures PostgreSQL compatibility.
- **Model settings in samples** — TPC-H example and sales model fixtures include `defaultNumericDataType`, `defaultTimezone`, and `allowUtcFallback`.

### Fixed

- **Pre-existing mypy errors** — resolved all type errors across `ui/app.py`, `model_store.py`, `sessions.py`, and `shortcuts.py`.

### Changed

- Version bumped to 1.7.0

---

## [1.6.2] - 2026-04-19

### Added

- **Query execution in Gradio UI** — new "Execute Query" button and "Query Results" tab with data table, visible when `QUERY_EXECUTE=true`. Calls `/query/execute` and auto-switches to results.
- **Docker UI instructions in README** — added examples for running API, UI, and Flight images together.
- **Gradio mount log message** — embedded mode now logs the UI URL on startup.

### Changed

- Version bumped to 1.6.2

---

## [1.6.1] - 2026-04-18

### Added

- **`model_json` input** — load and validate endpoints now accept `model_json` (JSON object) as an alternative to `model_yaml` (YAML string). Eliminates YAML escaping/indentation issues for LLM consumers.
- **Auto-parse stringified JSON** — if `model_json` is passed as a JSON string instead of an object (common with smaller LLMs), it is auto-parsed via `json.loads()`.

### Fixed

- **Verbose 422 error messages** — model validation errors now include all error codes and messages in the top-level `message` field, so MCP consumers see actionable details instead of generic "parsing or validation failed".

### Changed

- Version bumped to 1.6.1

---

## [1.6.0] - 2026-04-18

### Added

- **Extends/inherits model composition** — models can extend or inherit from other models via `extends_yaml` and `inherits_model_id` parameters on `POST /sessions/{id}/models`. `ExtendsMerger` deep-merges data objects, dimensions, measures, metrics, and filters with conflict detection.
- **Comprehensive malformed expression ref detection** — 16 bracket patterns detected across metric (`{[MeasureName]}`) and measure (`{[DataObject].[Column]}`) expressions, with specific error messages for each malformation (missing `[`, `]`, `{`, `}`, `.` separator, etc.).
- **UI query pickers** — dimension, measure/metric, and column dropdown pickers in the Gradio SQL Compiler tab with intelligent YAML insertion at correct sections and indentation.
- **UI editor toolbar buttons** — clear (✕), undo (↶), and redo (↷) buttons on both OBML and query CodeMirror editors.

### Fixed

- **UI editor layout** — fixed-height CodeMirror editors (45dvh) with bottom alignment, no content-dependent resizing.

### Changed

- Version bumped to 1.6.0

---

## [1.5.1] - 2026-04-16

### Added

- **OBSL measure filter expression** — measures with filters now export `obsl:filterExpression` in the RDF graph (e.g., `"Customers.Country equals 'US'"`). Updated ontology (`obsl.ttl`), SHACL shapes, spec, and example.

### Fixed

- **Unreachable filters silently skipped** — static and query-time filters on data objects not reachable from the query's join graph are now silently ignored instead of raising `UNREACHABLE_FILTER_FIELD`. Filters are irrelevant when the query doesn't touch that part of the schema.

### Changed

- Version bumped to 1.5.1

---

## [1.5.0] - 2026-04-16

### Added

- **Static model filters** — top-level `filters:` YAML key injects mandatory WHERE conditions into every query against the model. Supports all filter operators (OBML and SQL-style), auto-join extension, and AND combination with query-time filters.
- **ISO 8601 date/timestamp support** — bare YAML dates (`2026-01-01`) and timestamps (`2026-01-01T14:30:00Z`, `+02:00` offsets) are auto-coerced to ISO strings in both static and query-time filters.
- **Filter deduplication** — query-time WHERE filters identical to a static filter are silently skipped (no duplicate predicates in SQL).
- **OSI roundtrip for static filters** — `obml_filters` preserved in `custom_extensions` during OBML → OSI → OBML conversion.
- **JSON Schema validation** — `staticFilterOperator` enum (30 operators), typed `value`/`values` fields.
- **Schema API** — `filters` field in `GET /schema` response exposes static filters.

### Changed

- Version bumped to 1.5.0

---

## [1.4.0] - 2026-04-12

### Added

- **Absolute max-age** — `SESSION_MAX_AGE_SECONDS` (default 24 h) prevents immortal sessions from chatty clients that keep refreshing the idle TTL.
- **Global session cap** — `MAX_SESSIONS` (default 500) returns HTTP **429 Too Many Requests** with `Retry-After` header when at capacity.
- **Per-session model cap** — `MAX_MODELS_PER_SESSION` (default 10) limits how many models a single session may hold.
- **Rate limiting** — `SESSION_RATE_LIMIT` (default 10/min) per-IP sliding-window rate limit on `POST /sessions` via `SessionRateLimitMiddleware`.
- **Expiry visibility** — `expires_at` and `max_expires_at` fields in session responses let clients refresh proactively instead of getting surprise 404s.
- **410 Gone for expired sessions** — `SessionExpiredError` returns HTTP 410 (not 404) so clients can distinguish expired from never-existed.
- **Session lifecycle logging** — structured log events for session create, expire, close, and purge sweeps.
- **New settings in `GET /v1/settings`** — `session_max_age_seconds`, `max_sessions`, `max_models_per_session`.

### Changed

- Version bumped to 1.4.0
- Default session (`__default__`) is now purged when not in single-model mode (`MODEL_FILE` not set)
- `SessionManager` constructor accepts `max_age_seconds`, `max_sessions`, `max_models_per_session`, `is_single_model_mode` parameters
- `ModelStore` constructor accepts `max_models` parameter

---

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
