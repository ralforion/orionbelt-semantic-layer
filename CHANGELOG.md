# Changelog

All notable changes to OrionBelt Semantic Layer are documented here.

## [2.8.0] - 2026-06-02

### Added

- **Session-scoped OSI model endpoints.** Two new endpoints bridge the model store with Open Semantic Interchange (OSI), distinct from the existing stateless `/v1/convert/*` transforms:
  - `POST /v1/sessions/{id}/models/from-osi` accepts OSI YAML (`osi_yaml`), converts it to OBML, and loads the result into the session's model store. Returns the standard model summary plus `conversion_warnings` and the advisory OSI `input_validation`.
  - `GET /v1/sessions/{id}/models/{mid}/osi` exports a loaded model from the store as OSI YAML, with optional `?model_name=`, `?model_description=`, and `?ai_instructions=` overrides.
- **`ModelStore.get_raw()`**: public accessor for a model's raw OBML dict, preferring the faithful copy captured at load time and returning a deep copy so callers cannot mutate internal state.

### Fixed

- **OSI converter not packaged for non-editable wheel installs.** The converter (repo-root `osi-obml/`) was only discoverable via repo-root or `/app` paths, so a PyPI wheel or Docker install raised `ModuleNotFoundError` from the `/convert` and new OSI endpoints. The converter module and its OSI schema are now bundled into the wheel as package data under `orionbelt/_osi_obml/` (hatch `force-include`), and the lookup searches that location first. Verified on a clean wheel install.
- **Mermaid ER diagram clipped every label by one character** (`string` to `strin`, `Supplier` to `Supplie`). Mermaid pre-measures ER column and edge-label widths with the theme `fontFamily`, but the browser painted text with a wider font the host CSS cascaded in. Pinned a local-only font stack (`Helvetica, Arial, sans-serif`) in the diagram's `%%{init}%%` `themeVariables.fontFamily` and forced the same family on the rendered ER text, so measure-time and paint-time use the identical font.

## [2.7.10] - 2026-06-01

### Fixed

- **`GET /v1/reference/schemas/obml` and `/v1/reference/schemas/query` returned `HTTP 500: Schema file '...' is missing from this deployment`** on every non-editable install (PyPI wheel and Docker / Cloud Run). The loader resolved the JSON Schema files via `Path(__file__).resolve().parents[4] / "schema"`, which only equals the repo root in a source / editable layout; in an installed wheel that path points into `site-packages` and the files were never shipped there (`packages = ["src/orionbelt"]` excluded the repo-root `schema/` directory). The test suite missed it because it runs editable, where the buggy assumption holds. The schema files are now shipped inside the wheel as package data under `orionbelt/schema/` (via hatch `force-include`) and loaded through `importlib.resources`, with a source-tree fallback for editable checkouts. Added a regression test that exercises the loader directly.

## [2.7.9] - 2026-05-27

### Fixed

- **Colab notebook still broken on v2.7.8: `/v1/query/execute` returned `HTTP 503: ob-flight-extension package is not installed`** on every query cell. v2.7.8 dropped ob-flight-extension from the notebook's `_REQUIRED` map on the assumption the quickstart only queries via REST and never opens a Flight SQL connection. That assumption was wrong: `src/orionbelt/service/db_executor.py` imports `ob_flight.db_router.get_credentials` unconditionally for credential lookup on every dialect, including DuckDB. v2.7.8 unbroke API startup but moved the failure two cells later. Restored `ob-flight-extension` to the `_REQUIRED` map; PyPI now has 2.6.1 (published during the v2.7.8 cycle) with the `cache=` kwarg the API expects, so the install resolves cleanly. v2.7.8's `except TypeError` guard in the lifespan stays as forward-compat insurance.

### Tooling

- **Notebook smoke workflow had been masking Colab regressions.** The existing job ran inside `uv sync --all-extras` which always installs every `drivers/*` package as a workspace member; the test environment had `ob-flight-extension==2.6.1` from local source even when PyPI was at 2.1.0. My v2.7.8 verification missed cell-level errors because the wrapper script crashed before printing PASS/FAIL and I read the bg task's exit code as success. Added a second workflow job `notebook-pypi-equivalent` that builds the OBSL wheel from PR source, installs it + side packages **strictly from PyPI** into a plain `python -m venv` (no uv, no workspace), executes the notebook end-to-end, and asserts every code cell ran cleanly (mermaid.ink transient 503s are filtered as the only allowed exception). The workflow now also triggers on `src/orionbelt/**` and `pyproject.toml` changes - both v2.7.7 and v2.7.8 shipped Colab regressions through `src/` changes that the path-pinned trigger missed.

### Background (this is the 5th release touching notebook bugs)

The drift was real and the root cause was incomplete test coverage:

| Release | Notebook fix | What it missed |
|---|---|---|
| v2.7.5 | Added notebook smoke workflow with xfail | Workflow ran in uv workspace, mirrored Colab poorly |
| v2.7.6 | #87 (install cell idempotent), #88 (show_yaml typo), #89 (UI fallback) | Workflow still in workspace; Colab still broken upstream |
| v2.7.7 | #94 (uv-venv-no-pip), #91 / #92 / #94 bundle | Workflow finally green; Colab `pip install ob-flight-extension` resolves stale PyPI 2.1.0, TypeError on lifespan |
| v2.7.8 | #96 (drop ob-flight from notebook + catch TypeError) | Lifespan no longer crashes, but db_executor still requires ob_flight -> HTTP 503 on every execute |
| **v2.7.9** | Restore ob-flight (now that PyPI has 2.6.1) + add clean-venv workflow job | Real gate against the regressions above |

## [2.7.8] - 2026-05-27

### Fixed

- **Colab quickstart crashed on PyPI: `start_flight_background() got an unexpected keyword argument 'cache'`** ([#96](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/96)). Contract drift between OBSL and ob-flight-extension: OBSL has called `start_flight_background(cache=..., cache_config=...)` since v2.4.0, but the published PyPI release of ob-flight-extension is stuck at 2.1.0 (the local source in `drivers/ob-flight-extension/` bumped to 2.6.1 with the kwargs but was never republished). CI passed because the notebook workflow built the local 2.6.1 path; Colab failed because `pip install ob-flight-extension` resolves PyPI 2.1.0. The bug had shipped since v2.4.0 but was masked by separate notebook install-cell bugs until v2.7.7 fixed both (#87 / #94) and surfaced the underlying kwarg mismatch. Two-part fix: (1) drop `ob-flight-extension` from the Colab notebook's `_REQUIRED` map - the quickstart only queries via REST, never Flight SQL - so Colab never installs it and `find_spec("ob_flight")` returns False, correctly skipping Flight startup; (2) catch `TypeError` (in addition to `ImportError`) around `start_flight_background` in `src/orionbelt/api/app.py` so any future kwarg drift logs a clear warning naming both versions and continues serving REST + pgwire instead of crashing the whole lifespan. Verified end-to-end by executing the published Colab notebook in a fresh venv that mimics the Colab environment (OBSL local wheel, no ob-flight-extension, PyPI ob-driver-core / ob-duckdb / duckdb / pyarrow): API starts cleanly, all dataframe-returning cells run. 3 new tests: 2 static contract guards in `test_notebook_contracts.py` (no `ob_flight` in `_REQUIRED`, `except TypeError` present in lifespan); 1 runtime regression test in `test_lifespan_flight_signature_drift.py` that monkey-patches the old PyPI signature and asserts the lifespan logs a warning instead of crashing.

### Out of scope

- Publish ob-flight-extension 2.6.1 to PyPI so downstream Flight SQL installs work again. Separate release process; tracked as follow-up.

## [2.7.7] - 2026-05-27

### Added

- **`GROUP BY ALL` on supporting dialects** ([#91](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/91)). New `DialectCapabilities.supports_group_by_all` flag, advertised via `GET /v1/dialects`. Snowflake (2022+), Databricks/Spark (3.4+), DuckDB (0.7+), BigQuery, and ClickHouse (22.6+) now emit `GROUP BY ALL` instead of the explicit column list when no `ROLLUP` / `CUBE` modifier is requested. Functionally equivalent, much shorter on queries with computed dimensions where the explicit form repeats the full expression (e.g. `GROUP BY date_trunc('year', "Sales"."salesdate"), date_trunc('month', "Sales"."salesdate")` collapses to `GROUP BY ALL`). Postgres, MySQL, and Dremio unchanged. ClickHouse retains its trailing `WITH ROLLUP` / `WITH CUBE` form for modifier paths; delegates plain GROUP BY to the base implementation so the capability flag applies uniformly. 27 new tests in `tests/unit/test_group_by_all.py` (per-dialect emission, ROLLUP / CUBE fallback, measure-only no-GROUP-BY guard); 35 drift snapshots regenerated; `docs/guide/dialects.md` capability matrix updated; live-verified against Snowflake, BigQuery, Databricks, ClickHouse.
- **`aggregation: measure` for engine-delegated resolution** ([#92](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/92)). New `AggregationType.MEASURE` enum value (with `agg` / `aggregate` accepted as aliases via the normalizing validator). When set, the compiler emits `MEASURE("<measure_label>")` literally and skips column-reference resolution; the engine resolves the aggregation by name via its metric-view machinery. Only Databricks Metric Views accept this. The other 7 dialects (including Snowflake, which uses the separate `SEMANTIC_VIEW(view DIMENSIONS d METRICS m)` table function instead of bare `MEASURE()`) raise `UnsupportedAggregationError` → HTTP 422 with the `aggregation` and `dialect` echoed in the response. Model validator forbids `columns:`, `expression:`, `filters:`, `total: true` on a delegated measure (no source column to read). 22 codegen + validation tests in `tests/unit/test_aggregation_measure.py`; 5 OSI roundtrip tests. OBML signal propagated to `obml_reference.py` aggregation list + dialect matrix, `schema/obml-schema.json` enum, `ontology/obsl.ttl` + `obsl.shacl.ttl`, and the `osi_obml_converter.py` roundtrip (via `obml_aggregation: measure` under the COMMON custom_extension).

### Fixed

- **Colab notebook smoke workflow failed inside uv-managed venv** ([#94](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/94)). Two compounding bugs surfaced by the v2.7.6 notebook workflow run. (1) The install cell's `_REQUIRED` map mapped `ob_flight_extension` as the import name, but the actual module shipped by the distribution is `ob_flight`; `find_spec` always returned `None` on CI, forcing the pip fallback path. (2) uv-managed venvs do not include pip by default, so the fallback died with `No module named pip` and cascaded into `NameError` on every subsequent cell. Fixed the `_REQUIRED` map (`"ob_flight": "ob-flight-extension"`) and added `uv pip install pip` to `.github/workflows/notebook.yml` as a backstop. 2 new contract tests in `tests/unit/test_notebook_contracts.py` lock both fixes in place.

### Docs

- **SEO `description:` frontmatter** added to top-level MkDocs pages (`docs/index.md`, `docs/api/overview.md`, `docs/comparison/index.md`, `docs/getting-started/installation.md`, `docs/getting-started/quickstart.md`, `docs/guide/model-format.md`, `docs/guide/query-language.md`, `docs/guide/semantic-ql.md`, `docs/examples/sales-model.md`, `docs/examples/tpcds.md`). Improves Google snippet quality on the published site.

### Tooling

- **Live-DB sweep on PR #95**: Snowflake (12/12, 17s), BigQuery (12/12, 64s), Databricks (12/12, 103s), ClickHouse via testcontainer (12/12, 8s). `GROUP BY ALL` accepted natively by every supporting dialect — no SQL rewriting needed.

## [2.7.6] - 2026-05-27

### Fixed

- **`notebook_setup.show_yaml()` raised `TypeError` on every call** ([#88](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/88)). The `_IndentedDumper.increase_indent` override misnamed its parameter `_indentless`; PyYAML calls with `indentless=`. One-character fix. Bug had been in `examples/notebook_setup.py` since April; v2.7.5's contract tests only verified the module imported cleanly. Now `tests/unit/test_notebook_setup_helpers.py` calls every public display helper against synthetic inputs.
- **UI silently fell back to bundled YAML on transient `/v1/settings` failure** ([#89](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/89)). A single Cloud Run cold-start timeout cached an empty settings dict (`_cached_settings[url] = {}`), then silently loaded `examples/sem-layer.obml.yml` instead of the deployed model. Users saw a different YAML than what `/v1/settings` returned. Fix: kill the cache, retry with backoff, distinguish *"API unreachable"* from *"API in self-service mode"* via a `_unreachable` flag so the startup branch shows a placeholder instead of swapping models. 5 new tests.
- **Colab notebook smoke workflow failed on every PR** ([#87](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/87)). Notebook's `pip install -q` masked a conflict between CI's `uv sync` pre-installed working tree and the PyPI install. Fix: install cell is now idempotent (`importlib.util.find_spec` check), never `-q`; CI workflow pre-installs working tree so the check sees `orionbelt` already importable and skips pip.
- **JSON Schemas drifted from the Pydantic models** ([#85](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/85)). Four bugs: root `additionalProperties: false` nested inside `properties` (no-op); `timeGrain` enum advertised `year-end`/`month-end` that Python rejects; removed `dimension.group` and `measure.reduceToRelationDimensionality` still listed; `query-schema.json` missing the `grouping` property. All fixed. 8 contract tests round-trip valid / invalid payloads through both validators.
- **RDF exporter silently dropped v2.7.5 fields** ([#84](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/84)). `CustomExtension` / `ModelExample` / `WithinGroup` classes plus `numClass` / `delimiter` / `hasWithinGroup` / `hasCustomExtension` / `hasExample` and related properties shipped in the ontology in v2.7.5 but the exporter never emitted them. Authors who put `customExtensions:` or `examples:` in their model saw them vanish on export. Fix: exporter emits every new field; new bidirectional drift guard (`tests/unit/test_exporter_drift.py`) asserts every authored field shows up in the exported graph.

### Added

- **4 new test files** covering the bugs above, written in the spirit of the v2.7.5 review: `test_notebook_setup_helpers.py` (runtime smoke for display helpers), `test_ui_settings_fetch.py` (retry + no-cache + unreachable-marker), `test_json_schema_contract.py` (bidirectional Pydantic ↔ JSON Schema), `test_exporter_drift.py` (model → exported RDF).
- **`ontology/spec.md` refresh** — spec now lists window metrics, `partitionBy`, refresh policies, role-playing `via`, LISTAGG `delimiter`/`withinGroup`, `ModelExample`, `CustomExtension`, `numClass`, `primaryKey`. Brought back in sync with the actual ontology surface.

## [2.7.5] - 2026-05-26

### Fixed

- **`test_ob_clickhouse_driver::test_obml_derived_metric` raised `TypeError: unsupported operand type(s) for -: 'float' and 'decimal.Decimal'`** when run against the live ClickHouse testcontainer. The ob-clickhouse driver correctly returns `Decimal` for decimal-typed metric columns (preserving precision is the driver contract), but the test asserted via `pytest.approx(<float>)`, which can't subtract a `Decimal` from a `float` operand. Stay in the Decimal domain end-to-end: `pytest.approx(Decimal(200) / Decimal(240), rel=Decimal("1e-3"))`. No production-code change.
- **Ontology drift — six OBML fields silently missing from `ontology/obsl.ttl`.** Reported in [#82](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/82). Audit against `models/semantic.py` (and follow-up via the new drift-guard test) found that `numClass`, `primaryKey`, `customExtensions`, `withinGroup`, `delimiter`, the `examples` block + `intentTags`, and `Dimension.via` were all present in OBML for releases but absent from the RDF surface — the `OBML feature → also OSI + ontology` rule was relying on author memory and had drifted for v2.2–v2.6 modeling additions.
- **Inheritance silently dropped most parent OBML fields (`ModelStore._model_to_raw` was lossy).** Child models inheriting from a loaded parent saw a stripped version of the parent: columns kept only `code` + `abstractType`, measures lost `dataType` / `filters` / `grain` / `delimiter` / `withinGroup`, metrics lost most subtype config, and computed columns lost their `expression`. A child inheriting a parent's computed `Net Amount` column compiled to `SUM("Orders"."")` — wrong SQL, no error. Root cause: `ModelStore` only stored the resolved `SemanticModel`, so when it needed a raw dict for the merger it round-tripped through a hand-maintained serializer that covered only a small subset of fields. Now `ModelStore._raws` parallels `_models`, capturing the merged raw dict at load time so inheritance re-merges against the exact content the parent was built from. `_model_to_raw` retained as deprecated fallback for programmatically-constructed models. 4 new tests in `tests/unit/test_inherits_faithful.py` lock in round-trip coverage for computed columns, `numClass`/`primaryKey`, all the measure extras, and `examples`/`customExtensions`.
- **`Measure.aggregation` accepted arbitrary strings — `aggregation: ssum` compiled to `SSUM(...)` SQL.** Pre-fix `Measure.aggregation` was a plain `str`, so any typo or invented function name slipped through to the SQL emitter. Mild SQL-construction surface when model authoring is semi-trusted. Now `Measure.aggregation: AggregationType` (the existing enum) so Pydantic rejects unknown values at parse time. A normalizing field validator keeps accepting uppercase / mixed case (`"SUM"`, `"Sum"`, `"sum"` all resolve to `AggregationType.SUM`) since pre-fix BI tools and LLMs relied on that latitude. 12 new tests in `tests/unit/test_aggregation_validation.py`.
- **Resolver silently dropped `model.name`, `model.description`, `dataObject.description`** ([#83](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/83)). `ReferenceResolver` parsed these fields off the raw YAML but never passed them into `SemanticModel(...)` / `DataObject(...)` construction, so authored documentation vanished from every downstream surface — `GET /v1/models` discovery, the RDF graph exporter, the new ontology drift guard. Now forwarded through; 4 new round-trip tests in `tests/unit/test_resolver_metadata.py`.
- **Public ChatGPT Action OpenAPI on v2.5.0 with old `dimension`/`operator` query keys; OBML reference missing 3 of 8 dialects** ([#86](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/86)). The ChatGPT Custom GPT Action that users follow today (`integrations/chatgpt-custom-gpt/openapi-gpt-action.yaml`) had been broken since v2.5.0 — version label stale, query examples + schema using removed `dimension`/`operator` keys instead of `field`/`op`. Compiled queries from the GPT Action have been hitting `UNKNOWN_PROPERTY` since v2.7.2 strict parsing landed. Refreshed the OpenAPI; added EXISTS / NONEXISTS to the filter `op` enum. Also fixed `obml_reference.py` (consumed by `GET /v1/reference/obml`) which listed only 5 of the 8 supported dialects. The xfail in `tests/unit/test_notebook_contracts.py` flipped to passing — guard now locks in correct keys.

### Added

- **Ontology coverage for the missing OBML fields.** New `CustomExtension`, `ModelExample`, and `WithinGroup` classes in `ontology/obsl.ttl` with full property sets (`vendor` / `extensionData`; `exampleName` / `exampleDescription` / `exampleQuery` / `intentTag`; `withinGroupOrder`). New `numClass` / `primaryKey` / `delimiter` datatype properties. New `via` object property on `Dimension`. Matching SHACL shapes (`CustomExtensionShape` / `ModelExampleShape` / `WithinGroupShape`) added to `ontology/obsl.shacl.ttl` with the same cardinality and enum constraints the Pydantic models enforce.
- **`tests/unit/test_ontology_drift.py`** — automated drift guard that introspects every OBML modeling class in `models/semantic.py` and asserts each field maps to an `obsl:*` property in the ontology, with explicit exclusions for housekeeping fields and runtime-only config. New OBML fields now fail loudly until the author adds the corresponding RDF property — the rule no longer depends on memory.
- **OBSQL `EXISTS` / `NOT EXISTS` translation.** v2.7.0 added EXISTS / NONEXISTS to `QueryObject` but OBSQL (the BI-style SQL surface) rejected the syntax — `WHERE EXISTS (SELECT 1 FROM "OrderItems")` came back with `UNSUPPORTED_SQL_FEATURE` even though the QueryObject layer fully supported it. `compiler/sql_translator.py` now translates `[NOT] EXISTS (SELECT 1 FROM <DataObject> [WHERE <preds>])` into `QueryFilter(op=exists/nonexists, subquery=Subquery(...))`. The outer subject column for the planner's join walk is taken from the SELECT's first dimension (fallback: first measure). EXISTS subquery body is constrained: no `JOIN` / `GROUP BY` / `ORDER BY` / `LIMIT` / `HAVING` / nested EXISTS — these all error with `UNSUPPORTED_SQL_FEATURE`. Documented in `obsql_reference.py`. 12 new unit tests + 1 live pgwire round-trip test (Dremio compose stack — direct OBSL pgwire, not via Dremio's federation parser which can't see OBSL's nested data objects).

### Tooling

- **Colab notebook regression guard.** The published Colab quickstart was broken since v2.7.0 — `examples/notebook_setup.py` still referenced `MODEL_FILE` (removed in v2.7.0, silently ignored by pydantic-settings) so the API booted with no model loaded and every shortcut endpoint 404'd. Fixed the env var to `MODEL_FILES` and added two test layers so this can't recur:
  - `tests/unit/test_notebook_contracts.py` — static contract checks that scan the Colab notebook + `examples/notebook_setup.py` + every integration example (LangChain, CrewAI) for known-removed env vars / stale query keys. Runs in the default suite.
  - `tests/integration/test_notebook_execution.py` — heavier end-to-end smoke that executes every cell of the published Colab quickstart headlessly via `nbclient`. Marker-gated (`-m notebook`).
  - `.github/workflows/notebook.yml` — runs the execution-level test on every PR that touches the Colab notebook, `notebook_setup.py`, or the TPC-H YAML. Colab serves the notebook live from `main` (no publish step), so a breaking change to either file would otherwise ship to the next user who clicks the "Open in Colab" badge.

### Follow-ups filed

- [#84](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/84) Spec ↔ ontology ↔ RDF exporter are three drifting contracts (bidirectional drift guard needed) — needs design choice on flattening, deferred to follow-up release.
- [#85](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/85) JSON schemas in `schema/` have concrete drift vs the Pydantic models (root `additionalProperties` mis-nested, removed fields advertised, missing `grouping`) — deferred to follow-up release.

## [2.7.4] - 2026-05-26

### Fixed

- **Generated SQL was deeply over-parenthesized — every operator wrapped itself unconditionally.** Reported in [#79](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/79). The dialect emitter in `dialect/base.py` rendered every `BinaryOp` / `IsNull` / `Between` / `InList` / `UnaryOp` with an outer `(...)` wrap regardless of operator precedence or surrounding context. A simple `Year * 100 + Month` computed dimension joined into a multi-key anti-join produced nine layers of nested parens; the WHERE clause `WHERE ("Instrument"."instrmnt_id" IS NULL)` carried a redundant outer wrap. Correct SQL, but unreadable when copied into BI tools, the Gradio UI editor, or `/v1/query/sql` debugging.

### Added

- **Precedence-aware SQL emitter.** `compile_expr` now accepts an internal `_parent_prec` hint and each operator branch wraps its output in `(...)` only when its precedence is strictly less than its parent's required level. Atoms (literals, column refs, function calls, `CAST(...)`, `CASE ... END`) are at the top precedence level and never wrap. The clause root (SELECT projection, ON / WHERE / HAVING, GROUP BY / ORDER BY item, function argument) passes precedence `0` so the outermost expression never picks up a redundant outer wrap.
- **Non-associative operator handling.** Comparison operators (`=`, `<>`, `<`, `<=`, `>`, `>=`, `LIKE`, `NOT LIKE`) wrap any equal-precedence child on both sides — SQL forbids chained comparisons (`a >= b = c` is a syntax error in every supported dialect). Subtraction / division wrap the right operand at equal precedence so `a - (b - c)` keeps its required parens.
- 26 new tests in `tests/unit/test_emitter_precedence.py` covering root-clause unwrapping, mixed-precedence wrapping (OR inside AND, addition inside multiplication, etc.), subtraction associativity, comparison-chaining safety, and identical behaviour across all 8 dialects.

## [2.7.3] - 2026-05-26

### Fixed

- **Computed-column `CASE WHEN ... END` expressions silently compiled to the string literal `'CASE'`.** Reported in [#77](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/77). The recursive-descent parser in `compiler/expr_parser.py` tokenised `CASE` as a bare identifier; `_parse_factor` treated bare identifiers not followed by `(` as `Literal.string(...)`, leaving `WHEN ... THEN ... ELSE ... END` as dangling tokens that the parser silently dropped. A measure aggregating a computed column like `CASE WHEN {Default Status} NOT IN ('11', '14') THEN {Credit Exposure Amount} ELSE 0 END` ended up as `SUM('CASE')` in the generated SQL — a regulatory-quality bug for Anacredit-style risk metrics.

### Added

- **Full SQL expression syntax in computed-column `expression:` values**: `CASE WHEN ... THEN ... [WHEN ...]* [ELSE ...] END`, `[NOT] IN (...)`, `[NOT] BETWEEN ... AND ...`, `IS [NOT] NULL`, `[NOT] LIKE ...`. The existing AST nodes (`CaseExpr`, `InList`, `Between`, `IsNull`) already had codegen on every dialect — only the expression parser was missing the syntax. New `_SQL_KEYWORDS` set promotes the SQL keywords to `op` tokens so the parser can branch on them.
- **Strict parsing**: `parse_expression` now rejects unconsumed trailing tokens, missing closing parens, unterminated `CASE`, `CASE WHEN` without `THEN`, `IN` without `(`, `BETWEEN` without `AND`, `IS` without `NULL`. Pre-v2.7.3 these silently produced garbage AST.
- 28 new tests in `tests/unit/test_expr_parser_case.py` covering each new construct on all 8 dialects, plus the exact #77 repro and strictness regressions.

## [2.7.2] - 2026-05-26

### Fixed

- **Validator silently accepted unknown OBML / QueryObject properties — typos slipped through.** A measure with `filtter:` (typo) used to validate clean and compile to SQL with no filter applied — the exact class of bug a semantic layer is supposed to prevent. The Pydantic models defaulted to `extra="ignore"` and the OBML resolver picked fields manually with `raw.get(...)`, so unknown keys were dropped without comment. Reported in [#75](https://github.com/ralfbecher/orionbelt-semantic-layer/issues/75).

### Added

- **`UNKNOWN_PROPERTY` error code (no flag to bypass).** Strict parsing is now the implicit default for every OBML object (`dataObjects`, `columns`, `joins`, `dimensions`, `measures`, `metrics`, `filters`, `filterContext`, `grain`, `settings`, `examples`, etc.) and every QueryObject surface (`QueryObject`, `QueryFilter`, `QueryFilterGroup`, `Subquery`, `QuerySelect`, `QueryOrderBy`, `UsePathName`, …). Unknown keys are rejected with `UNKNOWN_PROPERTY` and a "did you mean?" suggestion list derived from the model's actual fields.
- **Pydantic `extra="forbid"`** on every model in `models/semantic.py` and `models/query.py` so anyone constructing a model via `Model.model_validate(some_dict)` gets the same strict behaviour as the resolver / API path. The FastAPI `RequestValidationError` handler translates Pydantic `extra_forbidden` entries into the OBSL error shape so REST clients see one consistent `UNKNOWN_PROPERTY` code instead of FastAPI's default "Extra inputs are not permitted" body.
- New test file `tests/unit/test_strict_property_parsing.py` (21 tests) covering every parse site on both surfaces.

## [2.7.1] - 2026-05-25

### Fixed

- **Gradio UI broken in admin-curated mode under v2.7.0.** The MODEL_FILE removal in v2.7.0 also dropped the legacy "auto-preload model into every new user session" behavior and stopped returning the model YAML in `/v1/settings`. The bundled UI hadn't migrated to the new `GET /v1/models` discovery path, so its compile / execute flow tried to upload the model into a fresh user session and hit `403 Single-model mode: model upload is disabled`. v2.7.1 restores both legs of the v2.6 contract — but **only when MODEL_FILES has exactly one entry** (single-model deployments). Multi-model deployments (N > 1) are unchanged and clients still use `GET /v1/models` for discovery.
  - `/v1/settings` re-exposes `model_yaml` (the single MODEL_FILES entry's YAML) so the UI can render the read-only editor.
  - `POST /v1/sessions` re-loads the protected model into each newly-created user session so the session-scoped compile / execute endpoints work without re-uploading.
- **Cloud Armor rule #106** (LLM-API recon block) inadvertently denied OBSL's legitimate `GET /v1/models` endpoint along with the OpenAI-style `/v1/chat/completions` probes it was meant to block. The regex is tightened in `orionbelt-infra` so `/v1/models` reaches the API while the OpenAI / Anthropic / Ollama recon paths remain blocked.

## [2.7.0] - 2026-05-25

### Removed

- **`MODEL_FILE` env var (deprecated since v2.4.0).** Removed in v2.7.0. Replace with `MODEL_FILES=<path>` — a single-entry comma-separated list is the direct equivalent and keeps the same admin-curated semantics (uploads/removals blocked, model preloaded at startup). The preloaded model now lives in its own *named protected session* (addressing name = OBML `name:` field or filename stem) rather than the legacy `__default__` session, so REST clients address it via `/v1/sessions/<model_name>/...`. The `single_model_mode` flag on `/v1/settings` is retained (it now means "admin-curated mode is active"), but the `model_yaml` field is always `null`; use `GET /v1/models` for discovery.

### Added

- **`exists` / `nonexists` filter operators.** First-class primitive for "row in this data object has (or doesn't have) a matching row in a related data object" — a correlated `EXISTS (SELECT 1 FROM …)` subquery that no longer requires modelling the question as a precomputed boolean column or a raw-SQL data-object expression. Drives regulatory data-quality rules, coverage / anti-join reports, and any "parent has at least one child of kind X" check. The new `subquery:` payload names the target data object (the planner walks the model's existing `joins:` to derive the correlation predicates — join columns are not restated), with an optional `pathName:` to pin a secondary join and an optional `filter:` list of predicates restricting which target rows count. Available in query-level `where:` only — `having:` is rejected (the row-level correlation subject is out of scope after `GROUP BY`); measure-level EXISTS is the deferred `MeasureFilter.subquery` follow-up. Portable across all 8 dialects. See [Existence Operators](docs/guide/query-language.md#existence-operators) and `design/PLAN_exists_operator.md`.

## [2.6.1] - 2026-05-24

### Fixed

- **Derived metric referencing a window metric now wraps correctly.** A derived metric like ``MoM Delta = {[Revenue]} - {[Revenue Prior Month]}`` selected without ``Revenue Prior Month`` also in the SELECT used to compile as ``SUM(amount) - "Revenue"`` — no ``LAG``, no window CTE, broken SQL. Even when both were selected together the substitution baked the window metric's inner aggregate into the outer expression, producing ``Revenue - Revenue`` (always zero) instead of the intended ``Revenue - LAG(Revenue)``. ``compiler/window_wrap.py`` now detects deferred derived metrics (DDMs), pulls every transitively-referenced window metric's base measure into the ``window_base`` CTE, and inlines ``_build_window_call(...)`` for window-component references at the outer SELECT — yielding ``"Revenue" - LAG("Revenue", 1) OVER (...)`` regardless of which combination of metrics appears in the SELECT.
- **Two-column statistical aggregates (`corr` / `covar_*` / `regr_*`) rejected cleanly in CFL.** The previous concat-count path emitted ``CORR(CAST(f0 AS VARCHAR) || '|' || CAST(f1 AS VARCHAR))`` — one argument, garbage types. New ``UnsupportedAggregationForCFLError`` (inherits ``UnsupportedAggregationError`` so existing router catches translate it transparently) is raised before the multi-fact branch. Single-fact queries using the same measures continue to compile through the star planner unchanged.
- **Expression-based two-column statistical aggregates rejected at model-load.** ``aggregation: corr`` combined with ``expression: "{a} + {b}"`` used to slip past arity validation and compile to invalid ``CORR((a + b))``. The validator now rejects ``expression:`` for two-column aggregates with guidance to use ``columns:`` (define computed columns on the data object for per-argument transformations). Single-column statistical aggregates (``stddev`` etc.) still accept ``expression:`` since ``STDDEV(<scalar>)`` is valid SQL.
- **Window metric inherits base measure's declared dataType.** ``window_wrap`` now applies ``_apply_measure_cast`` when projecting the base measure into the ``window_base`` CTE, mirroring ``cumulative_wrap``. A ``LAG`` over a ``decimal(18, 2)`` measure no longer operates on uncast ``SUM(...)``.
- **ORDER BY on window / cumulative / period-over-period metric resolves to the wrapped alias.** Previously ``_resolve_order_by_field`` returned ``meas.expression``, so ``ORDER BY "Revenue Prior Month" DESC`` silently rewrote to ``ORDER BY "Revenue" DESC`` (the lag-input). Now returns a table-less ``ColumnRef`` for wrapped metrics — same pattern as ``coalesce_aliases`` — so the outer SELECT binds the windowed output.
- **OSI converter emits all columns of multi-column measures.** ``_measure_to_sql`` only emitted ``columns[0]``, so a derived metric that referenced a two-column ``corr`` measure exported wrong OSI SQL even though the standalone measure path was already correct. Now iterates every column in declaration order.

### Added

- **Idempotent-wrap shim extended to metrics and non-matching measures.** OBSL's natural-SQL surface previously accepted ``SUM(<sum-declared measure>)`` as a syntactic shim (stripped, declared aggregation applied). Calcite-validating BI tools (Dremio, Spark, Flink) cannot mirror each measure's declared aggregation when ``GROUP BY`` is present, so the rule is generalised: any wrap from ``{SUM, MIN, MAX, AVG, MEDIAN}`` is accepted on metrics (whose values are evaluated per grain — wrap-over-singleton is provably a no-op) and on non-matching measures (same reasoning). ``COUNT`` / ``COUNT(DISTINCT)`` remain restricted to matching-aggregation acceptance because they change cardinality. Error messages now name every viable alternative.
- **``AGG()`` and ``AGGREGATE()`` accepted as ``MEASURE()`` aliases.** Three portable spellings for the explicit measure-marker syntax — Snowflake / Databricks ship ``MEASURE()``; Calcite-style proposals and some BI tools emit ``AGG()`` or ``AGGREGATE()``.

### Docs

- New section in ``docs/guide/postgres-wire-bi-tools.md``: **"6. Dremio SQL Runner — catalog flip & Calcite quirks"**. Documents three gotchas that Calcite-validating BI tools hit (address the model as ``<schema>."model"``, use Calcite-standard ``ROLLUP``/``CUBE`` syntax, wrap measures in idempotent aggregates because Calcite has no ``MEASURE()``), plus the AVG / COUNT_DISTINCT workaround playbook.
- Removed redundant ``(v2.6+)`` parenthetical version stamps across docs / comparison pages now that the v2.6 surface is mature. Earlier-version stamps are retained for readers on older OBSL builds.

## [2.6.0] - 2026-05-23

### Added

- **Trend Analysis primitives.** Four additive surface extensions for FP&A / finance workloads:
  - **`partitionBy` on `MetricType.CUMULATIVE`** — per-dimension rolling windows (e.g. 12-month MA per country). Threads through resolution into the cumulative window's `PARTITION BY` clause, composes with grain-to-date. Default `[]` preserves v2.5 SQL exactly.
  - **`MetricType.WINDOW`** — single-row window functions (`rank` / `dense_rank` / `row_number` / `ntile` / `lag` / `lead` / `first_value` / `last_value`). New `compiler/window_wrap.py` runs after `cumulative_wrap` so window metrics compose with cumulative outputs (e.g. ranking a moving average). New fields: `windowFunction`, `offset`, `buckets`, `orderDirection`, `defaultValue`.
  - **9 statistical aggregates** on `Measure.aggregation`: `stddev`, `stddev_pop`, `variance`, `var_pop`, `corr`, `covar_pop`, `covar_samp`, `regr_slope`, `regr_intercept`. Column arity validated at model-load time. ClickHouse maps to camelCase (`stddevPop`, `covarSamp`, etc.); MySQL rejects correlation/covariance/regression; BigQuery + ClickHouse reject linear regression — all with hard `UnsupportedAggregationError` at compile time, no silent fallback.
  - **Composition over `DERIVED`** — derived metrics already compose any metric by name, so MA-crossover signals, MoM deltas, and similar emerge with zero new compiler work.
  - New guide: [Trend Analysis](docs/guide/trend-analysis.md).
- **OSI v0.2 spec compatibility.** Converter emits `version: "0.2.0.dev0"` to match the upstream OSI spec evolution:
  - **`primary_key` first-class** — per-column `primaryKey: true` flags map to the OSI dataset's `primary_key` array (composite supported, declaration order preserved).
  - **`unique_keys` first-class** — round-trip via OBSL-vendor `customExtensions` (`obml_unique_keys`); OBML has no native unique-key concept yet.
  - **Field `label` first-class** — round-trip via OBSL-vendor `customExtensions` (`obml_field_label`).
  - **Top-level `dialects` / `vendors` informational arrays** emitted on every doc.
  - **`MAQL` dialect** and **`GOODDATA` vendor** added to the known-enum tuples; MAQL-only metric expressions pass through the existing fallback without crashing.
  - **Legacy v0.1.x reader** — `_normalize_legacy_v01()` promotes pre-v0.2 `obml_primary_key` / `obml_unique_keys` `custom_extensions` payloads into v0.2 first-class fields before parsing, so existing OSI v0.1.1 inputs continue to load.
  - **Vendored schema refreshed** to upstream `main` (`osi-obml/osi-schema.json`); `scripts/refresh-osi-schema.sh` keeps the vendored copy in sync.
- **OSI input validation on `POST /v1/convert/osi-to-obml`.** Response gains an optional `input_validation` field carrying Draft 2020-12 schema errors and semantic errors for the source OSI document. Advisory by default — the endpoint still returns 200 with the converted output even when input fails strict v0.2 validation, because the legacy v0.1 shim still produces correct OBML.

### Changed

- **Breaking (output format):** OBML → OSI conversion now emits OSI v0.2.0.dev0. Downstream consumers pinning to v0.1.1 will reject v2.6 output. Migration path: parse the output with any v0.2-aware reader, or use the OSI-side legacy shim on the reverse direction (the converter still reads v0.1 inputs).
- **`/v1/convert/osi-to-obml` response shape:** new optional `input_validation: ValidationDetail | None` field. Existing clients see no break (the field is optional and `null` on the obml-to-osi endpoint where input-side validation isn't wired yet).

### Propagated

- **OBML JSON Schema** (`schema/obml-schema.json`) — new metric fields, `WindowFunctionKind` enum, statistical aggregations.
- **OBML reference markdown** (`src/orionbelt/obml_reference.py`) — documents `MetricType.WINDOW`, `partitionBy`, and statistical aggregates.
- **OBSL ontology** (`ontology/obsl.ttl`) — new `obsl:WindowMetric` class plus `partitionBy`, `windowFunction`, `windowOffset`, `windowBuckets`, `orderDirection`, `windowDefaultValue` datatype properties. Disjointness with other metric subtypes asserted.
- **MkDocs** — new `docs/guide/trend-analysis.md` under the Guide nav. `docs/guide/model-format.md` lists the four metric types, the `partitionBy` + window-metric fields, and the statistical-aggregate arity rules. `docs/guide/osi.md` documents the v0.2 bump and the legacy reader shim. Comparison docs (dbt / Cube / LookML / Malloy / AtScale) refreshed to reflect the new differentiation surfaces.

### Tests

+72 new (43 trend-analysis unit + 9 OSI roundtrip for v2.6 fields + 16 OSI v0.2 compat + 4 convert-endpoint integration). Total suite: **2081 passed, 159 skipped**.

## [2.5.0] - 2026-05-22

### Added

- **Postgres wire protocol surface (pgwire)** — fourth surface alongside REST, Flight, and MCP. Native ``postgres://``-protocol endpoint on port 5432 (configurable via ``PGWIRE_PORT``) so any BI tool, ORM, or psql client speaking the Postgres v3 protocol talks to OBSL directly. Steps 1–4 land the hello-world handshake, semantic-SQL routing through ``SemanticRouter``, ``pg_catalog`` / ``information_schema`` emulation backed by an embedded DuckDB catalog, and the extended-query protocol (``Parse`` / ``Bind`` / ``Describe`` / ``Execute`` / ``Sync``). New env vars ``PGWIRE_ENABLED``, ``PGWIRE_HOST``, ``PGWIRE_PORT``, ``PGWIRE_AUTH_MODE``, ``PGWIRE_MAX_CONNECTIONS``, ``PGWIRE_QUERY_TIMEOUT_SECONDS``. PRs #58–#61.
- **v2.5.0 catalog layout: `orionbelt` database with one schema per model + a `model` table per schema.** BI tools see ``database=orionbelt``, ``schema=<model_name>``, ``table=model``. Six metadata views per schema — ``dimensions`` / ``measures`` / ``metrics`` and their ``_*_metadata`` siblings — surface the semantic surface for in-SQL introspection. The Arrow Flight ``model.<view>`` alias works identically on pgwire so the same probing SQL is portable across both wires.
- **Tableau Desktop end-to-end compatibility over pgwire / pgjdbc.** Captured and fixed every layer Tableau exercises during connect + dashboard query — wire format (``Bind.result_formats`` honoured per column, including 8-byte binary FLOAT8 for pgjdbc's binary-transfer set), result-column alias preservation (Tableau's ``SUM(x) AS "sum:x:ok"`` now survives translator + compiler intact via a sqlglot-based router-level rewrite), catalog OID translation (shadow ``_obsl_pg_attribute`` and ``_obsl_pg_type`` ``TEMP`` views map DuckDB internal type IDs to real Postgres OIDs so pgjdbc allocates the right-width column readers), JDBC connect-check temp-table dance (binary parameter decoding for INT2/4/8, BOOL, FLOAT4/8, TEXT/VARCHAR/NAME/BPCHAR, BYTEA; SQL rewrites for ``CREATE [LOCAL|GLOBAL] TEMP TABLE`` and ``SELECT … INTO TEMP TABLE``; stub ``_pg_expandarray`` macro for ``getPrimaryKeys``), canned locale + version probes (``SHOW lc_collate`` family, ``SELECT current_catalog`` / ``current_role`` / ``session_user``), and the Tableau ``HAVING (COUNT(1) > 0)`` tautology silently stripped by the translator.

### Fixed

- **CFL leg joins tables referenced by measure-filter expressions.** A measure with a filter on a sibling dim table (e.g. ``Electronics Sales`` filtering ``Products.Category = 'Electronics'``) expanded into the CFL UNION ALL leg as ``CAST(CASE WHEN Products.Category = 'Electronics' THEN Sales.Amount END AS …)``, but the leg's FROM only carried the measure's source table + dimension joins. Real-database runs failed with ``missing FROM-clause entry for table "Products"``. The planner now collects table refs from each own-measure expression (intersected with the leg's reachable set so unrelated facts don't get pulled in) and adds them to ``leg_required`` before computing the common-root + join path.
- **ob-postgres driver classifies ADBC PyArrow types.** ``adbc-driver-postgresql`` returns ``cursor.description[i].type_code`` as a PyArrow ``DataType`` (``int32`` / ``timestamp[us, tz=UTC]`` / …) and wraps NUMERIC / MONEY / INTERVAL in ``OpaqueType`` whose ``type_name`` attribute carries the Postgres type name. The legacy code did ``isinstance(type_code, int)`` and fell through to ``STRING`` for everything else, so every column from a live ADBC connection surfaced as TEXT — including NUMERIC measures that Tableau then read as strings. New ``_classify_type_code`` handles the PyArrow DataType paths, the OpaqueType repr-substring match, and keeps the OID-integer legacy psycopg2 path.
- **Executor recognises ADBC ``OpaqueType`` in ``_arrow_type_to_hint``.** Same root cause as the driver fix, surfacing in the executor's Arrow-fast-path type-hint detection. ``pa.types.is_decimal`` returns False for OpaqueType; we now inspect ``type_name`` and map ``numeric`` / ``money`` / ``decimal`` → ``number``, temporal names → ``datetime``, ``bytea`` → ``binary``. Wrapped in ``try/except (AttributeError, TypeError)`` so non-DataType objects fall through cleanly. Added a ``_PG_OID_TO_HINT`` table for ``_map_type_code`` covering the PEP-249 fallback when ``cursor.description`` populates type codes with raw Postgres OIDs (psycopg2/3).

#### DBeaver + Tableau end-to-end hardening (cdee746…91be4a2)

The catalog-layout flip surfaced a stack of BI-tool compat regressions. Each commit isolates one root cause; together they make the schema-tree refresh, column panel, SQL editor, and chart-query paths work end-to-end in DBeaver 26.x and Tableau Desktop 2024.x.

- **``pg_get_keywords()`` is a TABLE macro, not scalar.** DBeaver's SQL-editor probe calls it as ``FROM pg_get_keywords()``; DuckDB rejected the scalar form with ``Table Function with name pg_get_keywords does not exist``. Now declared as a one-row table macro returning a ``word`` column.
- **``current_schema()`` and ``SHOW search_path`` return the connected model.** Pre-fix the canned response was the literal ``"orionbelt"`` — but post-flip that's the database name, not a schema. DBeaver issued ``SET search_path = <model>`` then ``SELECT current_schema()``, got back ``orionbelt``, filtered ``pg_class`` by a non-existent schema, and NPE'd. The router now threads an ``_effective_schema(database)`` value into ``match_canned`` so both responses reflect the model schema (the connected database when it names a model, else the first loaded model for the ``orionbelt`` brand).
- **3-part qualifier stripping for Tableau pushdown.** ``_unwrap_model_qualifier`` previously only handled ``"<schema>"."model"``. Tableau's JDBC-source pushdown emits the full 3-part form ``"orionbelt"."<schema>"."model"`` and the un-stripped reference bounced off the OBSQL translator — empty charts in Tableau. The regex now accepts an optional leading database prefix; ``"a"."b"."model"`` reduces to ``"b"`` like the 2-part shape.
- **``search_path`` ParameterStatus at startup (DBeaver "all activities" NPE).** pgjdbc 42.x reads ``serverParameters.get("search_path").toString()`` from the post-AuthOk ParameterStatus frames; OBSL never emitted one for ``search_path``, so the lookup returned ``null`` and the unguarded ``.toString()`` matched the verbatim Java 17 NPE message users hit on every DBeaver action. Now emitted (along with ``is_superuser`` / ``session_authorization`` / ``IntervalStyle`` so other pgjdbc-cached lookups don't NPE the same way).
- **Canned ``SELECT current_schema(),session_user`` (DBeaver SQL-editor opener).** Only the single-column form was canned; the combined two-column form fell through to the semantic translator, errored on the missing FROM, and the downstream schema-tree refresh NPE'd in DBeaver's UI code. Both forms now return the two columns directly.
- **Eight empty pg_catalog stubs for relations DuckDB doesn't expose.** ``pg_event_trigger`` / ``pg_publication`` / ``pg_subscription`` / ``pg_foreign_data_wrapper`` / ``pg_foreign_server`` / ``pg_user_mapping`` / ``pg_policy`` / ``pg_extension``. DBeaver's schema browse probes each; without the stubs DuckDB returned ``Catalog Error: Table with name <X> does not exist`` and the browse aborted with a generic SQL-state 42601. Empty results are semantically correct for OBSL's read-only semantic surface (no event triggers, no logical replication, no foreign tables, no RLS policies).
- **Idempotent catalog refresh → stable ``pg_class.oid`` (DBeaver Columns panel).** Every ``CatalogEmulator.refresh()`` call ran ``DROP TABLE … model`` + ``CREATE TABLE``, assigning a fresh oid each time. DBeaver caches the table oid from one probe (``getTables`` walk) and reuses it on the next (``WHERE c.oid = $1`` in ``getColumns``). Shifted oids → DBeaver's column panel rendered empty. Refresh now hashes the table DDL + the model's dimension / measure / metric labels into a per-schema fingerprint; identical models skip the DROP / CREATE so the oid stays stable across catalog probes. A real model edit (rename / type swap / add / remove a column) still invalidates the fingerprint and triggers a clean re-CREATE.
- **``model.<view>`` user-friendly alias for metadata views.** The Arrow Flight surface accepts ``SELECT * FROM model.dimensions`` (``model`` reads as "the connected model"); pgwire now does the same — the router recognises the alias and rewrites it to the connected model's actual schema before catalog execution. Covers all six metadata views.
- **``<schema>.model`` routes to OBSQL data path, not catalog probe.** ``_references_model_schema`` matched the qualified ``test_sem_layer.model`` and routed to the catalog DuckDB — where ``model`` is a column-shape-only table with no rows. Now excluded from the catalog match so ``_unwrap_model_qualifier`` + the OBSQL translator handle the data query end-to-end. Tableau / Dremio / DBeaver direct pushdown all hit the right path.
- **Shadow ``pg_settings`` exposes Postgres GUCs DuckDB lacks (the Tableau ``81B3934F`` connection error).** Tableau's pgjdbc connect-check runs ``SELECT setting FROM pg_settings WHERE name = 'max_index_keys'`` and aborts the entire connection when the result is empty. DuckDB's native pg_settings only carries DuckDB-internal GUCs (``max_memory`` …). The shadow UNIONs ``duckdb_settings()`` with the standard Postgres defaults for ~27 GUCs BI tools probe at connect — ``max_index_keys`` / ``max_identifier_length`` / ``server_version_num`` / locale settings / search_path / transaction defaults / extra_float_digits / application_name / IntervalStyle / etc.

### Deferred

- **``MODEL_FILE`` removal pushed to v2.6.0.** The v2.4.0 changelog scheduled removal for v2.5.0 but the BI-tool compat work consumed the runway. The deprecation warning still fires at startup and the alias still works; only the removal target moves. ``MODEL_FILE`` and ``MODEL_FILES`` remain mutually exclusive — startup errors when both are set.

#### Arrow Flight catalog tree mirrors pgwire (5d8aeaa)

- **Flight ``CommandGetCatalogs`` / ``CommandGetDbSchemas`` / ``CommandGetTables`` / ``CommandGetColumns`` flip to match the pgwire v2.5.0 layout.** Pre-fix the Flight tree showed ``<model_name> > model > <model_name>`` — the model name as catalog, the literal "model" as schema, and the per-model virtual table at the table level. Confusing for users toggling between Flight and pgwire connections in DBeaver. Now both wires render ``orionbelt > <model_name> > model`` (plus the six ``dimensions`` / ``measures`` / ``metrics`` + ``_*_metadata`` views). Model selection moves from ``catalog_filter`` (protobuf field 1) to ``db_schema_filter`` (field 2); the old field 1 path is preserved as a fallback so the obsql CLI ``--model`` flag and our Dremio integration tests keep working unchanged.

## [2.4.0] - 2026-05-15

### Added

- **OrionBelt Semantic QL (OBSQL)** — the third surface in the OBSL / OBML / OBSQL trio. BI-style SQL against a per-model virtual table, translated to `QueryObject` and compiled through the existing pipeline. New `src/orionbelt/compiler/sql_translator.py` is a pure-function translator (`translate_sql_to_query(sql, model) -> QueryObject`) — sqlglot-based, with no Flight or FastAPI imports. New REST endpoints `POST /v1/sessions/{id}/query/semantic-ql[/compile]` and top-level shortcuts `POST /v1/query/semantic-ql[/compile]`; the `/compile` variant returns the translated QueryObject JSON so callers can see what their SQL became. Grammar covers bare-label form, `MEASURE("…")` marker (matches Snowflake `SEMANTIC_VIEW` / Databricks metric-view syntax), aggregate-wrap matching (`SUM(sum_measure)` accepted when the wrap matches the declared aggregation; mismatched wraps and any wrap on a metric reject with a clear error naming the declared form), `WHERE` on a measure auto-routes to `HAVING`, `GROUP BY` silently accepted, `ORDER BY` by alias or 1-based position, and explicit rejection of joins / CTEs / subqueries / UNION / window functions / `SELECT *` with `UNSUPPORTED_SQL_FEATURE`. New guide at `docs/guide/semantic-ql.md`.
- **OBSQL no-FROM mode.** On a single-model connection, `SELECT "Customer Country", "Total Revenue"` (no `FROM`) resolves to the implicit model. Classification routes no-FROM SELECTs to semantic mode when every column identifier matches a dim / measure / metric. Unknown identifiers reject with `RAW_SQL_REJECTED` so the user gets a clear "this isn't a query against the model" signal rather than `UNKNOWN_SELECT_ITEM`.
- **OBSQL raw mode via qualified columns.** OBML raw-mode (un-aggregated detail rows from data objects) is now a first-class OBSQL shape, triggered by qualified `"<DataObject>"."<column>"` references in `SELECT`. Translator inspects each SELECT item; if every item is a qualified data-object column, it emits `QuerySelect(fields=[...])` instead of dimensions/measures. WHERE accepts qualified-column predicates only (no measure routing, no HAVING — HAVING rejects with `UNSUPPORTED_SQL_FEATURE`). GROUP BY rejects (raw is per-row, not aggregated). DISTINCT honoured via `QuerySelect.distinct`. ORDER BY accepts qualified refs from the SELECT list or 1-based positions. Mixing qualified raw cols with bare dim/measure labels in the same SELECT rejects with new `MIXED_RAW_AND_AGGREGATE_MODE`.
- **`WITH ROLLUP` / `WITH CUBE` first-class.** `QueryObject.grouping: Grouping | None` enum. Star + CFL planners emit `GROUP BY ROLLUP(...) / CUBE(...)` and append `GROUPING(dim) AS _g_<dim>` flag columns. ClickHouse dialect override emits `GROUP BY ... WITH ROLLUP / CUBE` (function form not supported by ClickHouse). Trailing `WITH ROLLUP` / `WITH CUBE` in OBSQL is stripped before parse and maps to the Grouping enum; `GROUP BY ROLLUP(...)` function form also recognized. Auto-order even without `LIMIT` when grouping is set, defaulting to `NULLS FIRST` so subtotals and the grand-total row sort to the top of BI pivot tables. Backfill `NULLS FIRST` on explicit `ORDER BY` entries that omit the NULLs position; respect explicit `NULLS LAST`. `INCOMPATIBLE_COMBINATION` warning when rollup combines with total / period-over-period / cumulative wrappers.
- **OBSQL `OFFSET` support** (both semantic + raw mode) translates `OFFSET <n>` into `QueryObject.offset`. Pairs with `LIMIT` for deterministic pagination. Schema updated (`schema/query-schema.json` adds `nullsPosition` enum + `nulls` field on `queryOrderBy`).
- **Multi-model addressing — `MODEL_FILES` + Flight catalog routing.** New env var `MODEL_FILES=path1.yaml,path2.yaml,...` lets OBSL pre-load N models at startup; each model gets its own protected internal session whose session id IS the model's addressing name. BI tools and clients pick which to query via the standard Flight SQL "Database" field (`Connection.setCatalog()` / gRPC `database` metadata header). New `_SessionRoutingMiddleware` reads `database` / `x-obsl-model` / `catalog` from gRPC metadata; `_get_model(context)` resolves in priority order (explicit selector → legacy `__default__` → auto-resolve when exactly one model is loaded → rich error listing available models with per-client setup instructions). `CommandGetCatalogs` now returns one row per loaded model; per-model dialect: OBML `settings.defaultDialect` wins over the server's process-wide `DB_VENDOR`. New name-normalisation pipeline at `src/orionbelt/models/identifiers.py` (lowercase → replace whitespace/dot/dash runs with underscore → collapse runs → strip `_obml` suffix → validate against `^[a-z][a-z0-9_]{0,62}$` → reject reserved names). New optional `name:` field on `SemanticModel`. New `GET /v1/models` discovery endpoint lists every pre-loaded model with name, description, and counts.
- **OBSQL CLI** — `examples/obsql.py`, a tiny pyarrow-based smoke-test CLI for the Arrow Flight SQL surface. Supports model selection via the `database` header (`--model/-m`), catalog discovery (`--list` hits REST `/v1/models`), and demonstrates all three Flight modes (semantic / catalog / governance rejects). Mirrors the `psql` convention — the CLI shares its name with the language it runs.
- **Reference endpoints for LLM / MCP / BI tool discovery.** New `GET /v1/reference` index lists all available references; `GET /v1/reference/obml` and `GET /v1/reference/obsql` return agent-friendly markdown grammar references; `GET /v1/reference/schemas/{name}` (`obml | query`) returns JSON Schema with content-type `application/schema+json`. New `obsql_reference.py` module mirrors `obml_reference.py` with virtual-table shape, three SELECT forms, MEASURE() + aggregate-wrap matching, raw mode, catalog mode, hard-rejection error code table, and worked examples.
- **Flight extension result-cache wiring.** Flight semantic queries now participate in the same freshness-driven result cache as REST `/query/execute`, with matching keys and TTL semantics. `OBFlightServer.__init__` accepts `cache` + `cache_config`; `_prepare_sql` now returns a 6-tuple including a `cache_meta` dict; `_execute_sql` checks the cache pre-execute (lookup → decode parquet → wrap in RecordBatchStream) and writes back post-execute. All cache errors are best-effort — execution never fails because of cache I/O. OBML YAML queries also cache (free with SQL-hashed keys). A dashboard tile rendered via OBSQL in DBeaver and via OBML YAML in an MCP agent will hit the same entry.
- **Deterministic caching — auto-order on `LIMIT` + non-deterministic SQL bypass.** Two cache-correctness holes closed: `LIMIT N` without `ORDER BY` (engines return any N rows; cache freezes one slice forever) and SQL containing `RAND()` / `NOW()` / `CURRENT_DATE` / `TABLESAMPLE` (same SQL, different answer per call — `WHERE date >= CURRENT_DATE - 7` cached today serves stale "last week" forever). When `limit` is set with no explicit `order_by`, the planner appends `ORDER BY <all dims>` (aggregate-only queries skip — single-row results are already deterministic). New `cache/determinism.py::is_nondeterministic_sql(sql)` walks the compiled SQL for known random / clock / sample patterns after stripping string literals and quoted identifiers. New `NoCacheReason.NON_DETERMINISTIC_SQL` so responses surface `ttl_source = "no_cache:non_deterministic_sql"`.
- **Flight observability — full OBSQL request + compiled SQL logged on Flight + REST paths.** Every compile path now logs two `INFO` lines: the OBSQL request (preserved newlines, human-readable) and the compiled SQL. QueryObject-shaped REST endpoints (`/query/sql`, `/query/execute`) log the input as pretty-printed JSON. Same for OBML YAML on the Flight side. Replaces the previous 200-char truncation that hid auto-generated `JOIN` / `GROUP BY`.
- **Flight catalog metadata views.** Per-category label views for BI column pickers — `dimensions` / `measures` / `metrics` (no leading underscore, look like regular catalog objects) with one column per dim/measure/metric label typed by `result_type`. Queries `SELECT "X" FROM dimensions` route to semantic mode. Introspection schemas move to `_dimensions_metadata` / `_measures_metadata` / `_metrics_metadata` (underscore prefix follows Postgres / DBeaver "internal" convention). `_metrics_metadata` surfaces `time_dimension` / `window` / `grain_to_date` / `time_grain` so cumulative and PoP metrics no longer require cross-referencing `_dimensions_metadata` to decode "Rolling 3m Sales".
- **Commerce battery across 8 dialects + persistent cloud fixtures.** Shared commerce parquet fixtures (`tests/fixtures/commerce/`) and battery runner (`tests/integration/_commerce.py`), regenerated from the demo DuckDB. New live integration tests for BigQuery, Databricks, Snowflake with skip-if-exists data caching (rowcount-vs-parquet check); `BIGQUERY_RESEED` / `DATABRICKS_RESEED` / `SNOWFLAKE_RESEED` env vars force reload. ClickHouse / MySQL / Postgres tests migrated to the shared battery. New pytest markers `snowflake` / `bigquery` / `databricks`. Real-DB ROLLUP/CUBE tests across all 4 vendors (Postgres / ClickHouse / MySQL / DuckDB).

### Changed

- **BREAKING: hard-block raw SQL + DDL/DML across the Flight surface.** OBSL is a semantic layer, not a JDBC proxy — make that the design, not a configuration choice. Removed env flags `FLIGHT_ALLOW_RAW_SQL` and `FLIGHT_ALLOW_DATA_OBJECT_SQL`. There are no longer any flags that allow arbitrary SQL through to the warehouse. The only SQL that reaches the warehouse is produced by the OBSL compiler from a `QueryObject` (REST `/query/execute`) or from OBSQL. New mode `catalog` answers `SHOW TABLES` / `DESCRIBE` / `information_schema.*` / `pg_catalog.*` / `_dimensions` / `_measures` / `_metrics` / scalar probes (`SELECT 1`, `SELECT version()`, `current_database()`) from the model in-process via `_handle_catalog_sql()` returning a `pa.Table` — never touches the warehouse, so BI tool discovery (Tableau, Power BI, DBeaver) works without granting raw warehouse access. New error codes `RAW_SQL_REJECTED` (replaces `RAW_SQL_DISABLED`, no flag to bypass) and `WRITE_OPERATION_REJECTED` (top-of-prepare-sql guard rejects INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / TRUNCATE / MERGE / GRANT / REVOKE / COMMIT / ROLLBACK before any other classification, on every path).
- **BREAKING: `MODEL_FILE` is deprecated** — preserved as an alias for `MODEL_FILES` with one entry, deprecation warning logged at startup, **scheduled for removal in v2.5.0**. `MODEL_FILE` and `MODEL_FILES` are mutually exclusive — startup error when both are set. Existing `MODEL_FILE` deployments work unchanged via the legacy `__default__` session path.
- **BREAKING: result cache `KEY_VERSION` bumped to 2** — `build_cache_key` now hashes on `session_id + model_id + dialect + compiled SQL` instead of the QueryObject JSON. The compiler is deterministic so two callers reaching the same compiled SQL — via OBSQL, QueryObject, or OBML YAML — share a cache key. Trade-off: drops the QueryObject normalization layer (sort dim lists, normalize `IN` value order); the upgrade invalidates pre-v2.4.0 cache entries.
- **OBSQL-shaped error envelopes everywhere.** REST `/query/plan`, `/query/execute`, `/oneshot/batch` all catch `UnsupportedAggregationError` and the new `UnsupportedGroupingError` and return 422 with a structured warning instead of 500.
- **Drivers and UI documentation refreshed** across `docs/comparison/{dbt,lookml,malloy}.md`, `docs/guide/drivers.md` (DBeaver section now leads with OBSQL), and the in-tree `obsql_reference.py` for the no-FROM mode + governance + multi-model `database` selector.
- **Flight `CommandGetSqlInfo` populated.** DBeaver / Tableau Flight no longer show `Server: ?` — the response now carries the 4 standard SqlInfo entries (server name, server version, arrow version, read-only flag) over the spec-mandated dense-union schema.
- **MySQL `ORDER BY ... NULLS FIRST/LAST` only emits the `IS NULL` workaround when it disagrees with MySQL's default.** MySQL orders NULLs as the smallest value (`ASC` puts NULLs first; `DESC` puts them last). The two matching-default cases now compile to plain `ORDER BY <dim> ASC/DESC` (the common path on the new ROLLUP/CUBE auto-order); only the two non-matching cases emit the extra `<expr> IS NULL` sort key. Class-level docstring on `MySQLDialect` documents the two SQL-standard deviations.
- **Mypy strict cleanup across `orionbelt-semantic-layer` and `ob-flight-extension`.** Both projects now type-check clean (92 + 8 files). Added the PEP 561 `py.typed` marker to `ob-flight-extension` so callers see typed imports instead of `Skipping analyzing "ob_flight.*"` warnings.

### Fixed

- **Cache key `_normalize_sql` collapsed whitespace inside quoted regions** — `name = 'A  B'` and `name = 'A B'` (or `"Order  Id"` vs `"Order Id"`, or backtick variants) produced the same cache key and served each other's rows. Now skips quoted regions (single-quote literals, double-quote ANSI identifiers, backtick MySQL/BQ/Databricks identifiers) when normalizing.
- **`SessionManager._is_expired()` ignored `session.protected`** — `get_store()` / `get_session()` would delete an admin-loaded (`MODEL_FILES`) session on the first access past TTL even though `_purge_expired` correctly skipped it. Returns `False` early for protected sessions.
- **Flight `_cache_put_table` wrote bare parquet** — REST writes via `parquet_codec.encode` (sql/dialect/columns in Parquet key/value metadata). On a shared cache key, a REST reader of a Flight entry decoded `sql=""`, `dialect=""`, `columns=[]`. Flight now writes the same envelope so the cache namespace is bidirectionally consistent.
- **Flight cache column metadata key mismatch** — `_cache_put_table` wrote column metadata as `data_type` but REST's decoder reads `type`. Flight-written cache hits decoded sql/dialect/columns/rows correctly but numeric and datetime columns came back as `type="string"`. Encoded under `type` with a small Arrow→OBSL type-hint mapper.
- **OBSQL trailing `WITH ROLLUP/CUBE` was silently dropped in raw-mode queries.** The trailing modifier is stripped from the raw SQL text *before* parsing, so by the time `_build_raw_mode_query` checks `ast.args.get("group")`, the grouping was gone. Now threaded through as `forced_grouping` and rejected with `UNSUPPORTED_SQL_FEATURE`, symmetric to the existing `GROUP BY` rejection.
- **Flight `CommandGetTables/Columns` ignored `table_name_filter_pattern`** — DBeaver's per-node `CommandGetColumns` requests for `_measures` / `_dimensions` returned every row, so DBeaver attributed all rows to the expanded node and dimensions/measures/metrics appeared mixed under each metadata view. New `parse_table_filter` / `matches_filter` helpers (parse field 3 from the protobuf body; SQL LIKE-style `%` and `_` wildcards) thread the parsed filter through.
- **Flight `CommandGetTables/Columns` ignored protobuf field 1 (catalog)** — selecting a catalog through the command body still got the auto-resolved model's metadata in multi-model mode. New `parse_catalog_filter` routes the metadata build to the matching session/model; unknown catalog returns empty metadata (no silent fallback).
- **Flight `build_tables_table` / `build_columns_table` hard-coded `catalog_name="orionbelt"`** so every model's tables collapsed under the single catalog even after `build_catalogs_table` started reporting one catalog per loaded model. Now reads `_ob_model_id` (stamped by `_stamp_model`) and emits per-model catalog names.
- **MySQL dialect rejected `compile_group_by(grouping="cube")` with bare `NotImplementedError`** which surfaced as a 500 via REST. New domain `UnsupportedGroupingError(dialect, grouping)` mirrors the existing `UnsupportedAggregationError`; 422 catch handlers added at every REST/oneshot call site that already catches `UnsupportedAggregationError`.
- **OBSQL trailing line / block / `#` comments slipped past the trailing-modifier regex** — DBeaver's autogenerated SQL like `SELECT … FROM t WITH CUBE -- ORDER BY 1 NULLS FIRST` no longer trips through to RAW_SQL_REJECTED. Comments inside string literals and quoted identifiers preserved by a small state-machine pass; block comments collapse to a single space so `a/*x*/b` doesn't fuse into `ab`. Coverage for `--` (universal), `#` (MySQL / MariaDB / BigQuery), and `/* block */` (universal).
- **`GROUPING()` argument is the group-by expression, not the SELECT alias.** Star planner emitted `GROUPING("Client Name")` referencing the SELECT alias; Postgres / Snowflake / BigQuery rejected this. Now stashes the per-dimension group-by expression by alias so `GROUPING()` uses the same expression in `GROUP BY ROLLUP(...)`. Time-grained dimensions get their dialect-wrapped (`DATE_TRUNC(...)`) form propagated automatically.
- **CFL own-leg measure cast aligned with sibling NULL padding** — resolves ClickHouse `UNION ALL` Variant typing errors. Databricks dialect overrides `_ABSTRACT_TYPE_MAP` so CFL NULL-padding renders `STRING` (Databricks rejects bare `VARCHAR` without a length).
- **UI execute button + dialect refresh on page load, not process startup.** The startup-time `_fetch_settings()` call has a 5s timeout and silently returns `{}` on failure — common on Cloud Run cold starts. Decision moves from per-process to per-session by chaining `_refresh_query_exec_visibility` after `_restore` in `demo.load()`.
- **Flight catalog SQL precomputes the result `pa.Table` at `get_flight_info` and `do_action` (prepared-statement) time** so JDBC clients see the actual table schema instead of a placeholder `pa.schema([pa.field("result", pa.utf8())])`. DBeaver's SQL editor uses the prepared-statement path almost exclusively — that's why `SELECT * FROM _measures_metadata` showed one column called `result` with the measure names in it.
- **Ctrl-C left port 8815 bound** — `FlightServerBase.shutdown()` returns but the gRPC C++ thread can keep the socket. Added `server.wait()` so `serve()` actually drains before declaring the server stopped.
- **Flight `SHOW TABLES` over the text OBSQL path rendered the binary `table_schema` column as raw bytes in pandas output.** The binary column is needed for the protobuf `CommandGetTables` path (JDBC clients decode it) but is meaningless for text-mode display. `_catalog_tables_table` now strips that column. Also adds a raw-text fast-path so `SHOW` / `DESCRIBE` / `DESC` / `USE` / `SET` never reach sqlglot (silences the `"'SHOW tables' contains unsupported syntax"` warning).
- **Stale `allow_data_object_sql` kwarg from startup path.** The kwarg was removed from `OBFlightServer.__init__` in the hard-block-raw-SQL commit but `startup.py` still passed it, causing FastAPI lifespan to fail at boot with `TypeError: OBFlightServer.__init__() got an unexpected keyword argument 'allow_data_object_sql'`. Removed.
- **Vendor-execution drift snapshot refresh** for Databricks CFL after the `STRING` type-map fix (snapshot was missed in the 8-dialect commerce battery commit).

## [2.3.1] - 2026-05-11

### Fixed

- **MySQL `CAST(string-typed expr AS string)` no longer emits invalid / silently-truncating SQL.** `dialect/mysql.py::_compile_cast` previously rewrote any `VARCHAR[(N)]` cast target to `CHAR[(N)]` without inspecting `N`. For OBML's unbounded `string` type — which `_OBML_SIMPLE_TYPE_MAP` resolves to `VARCHAR(65535)` — that produced `CAST(x AS CHAR(65535))`, exceeding CHAR's 255-character column limit; the abstract `string`-via-`_ABSTRACT_TYPE_MAP` path produced `CAST(x AS CHAR(255))`, silently truncating any longer value. The cast rewriter is now length-aware: lengths above 255 collapse to plain `CHAR` so MySQL picks a safe internal width without truncation; lengths ≤ 255 are preserved.
- **Vendor-execution drift normaliser keeps distinct string keys distinct.** `tests/integration/drift/vendor_exec/test_vendor_exec.py::_normalize_value` ran every string through `Decimal()` → `float():.11g` to keep YAML-stored Decimal goldens (`"100.50"`) symmetric with live vendor Decimals. That coercion also collapsed zero-padded IDs (`"00123"` → `"123"`), exponent-form strings (`"1e3"` → `"1000"`), and `"0000"` → `"0"`, which would have let a cross-vendor key-handling regression silently pass row-set equality. The coercion is now restricted to canonical `Decimal.__str__` form (no leading zeros, no scientific notation) so legitimate decimal goldens still normalise but string IDs stay distinct.
- **`test_all_pointers_collect_in_pytest` no longer requires `uv` on PATH.** The Tier 2 metadata gate at `tests/integration/drift/test_snapshot_metadata.py` shelled out to `uv run pytest --collect-only ...`, failing for infrastructure reasons in any environment that runs the test suite without `uv` installed (CI containers, plain virtualenvs). It now uses `sys.executable -m pytest`.

## [2.3.0] - 2026-05-10

### Added

- **Two-tier integration test framework.** `tests/integration/correctness/` ratifies query results via independent computation paths (aggregation invariance, hierarchical rollup, hand-SQL reference, pandas baseline, metric algebra, CFL split, filter additivity); `tests/integration/drift/` snapshots compiled SQL + canonical-sorted result rows per query (DuckDB exec + 8-dialect compile-only + a metadata gate that validates every snapshot's `last_verified_by` pointer is a real, collectable pytest test). Pure-OBML query files under `tests/integration/correctness/queries/` plus a sidecar `corpus.yaml` manifest keep the schema-faithful query bodies separated from test-rig metadata. Operator manual at `docs/guide/correctness-and-drift-tests.md`; dev quick-reference at `tests/integration/README.md`.
- **Phase A vendor-execution sweep** — every corpus query × every available local engine (DuckDB, Postgres 16, MySQL 8, ClickHouse) via testcontainers, gated by `pytest -m docker`. 60 / 60 pass, no xfails. Cross-vendor row normalisation: tz-naive datetime, midnight-→-date collapse, numeric values rounded via `float()` to 11 significant digits to absorb cross-engine ULP drift while preserving money sums up to ~$100 B at 2-dp precision.

### Changed

- **Cumulative metrics now respect their declared `dataType`.** `compiler/cumulative_wrap.py` casts the inner `cumulative_base` projection and the outer windowed aggregate to the metric's declared decimal type. Pre-fix, `Cumulative Sales` declared `decimal(18, 2)` returned DOUBLE values with last-bit drift like `281222.37999999995`; now it returns exact 2-dp Decimals.
- **HAVING auto-includes referenced measures.** `compiler/resolution.py` pre-scans every HAVING filter (recursively across `QueryFilterGroup`) and ensures every referenced measure / metric ends up in `resolved.measures` *before* base-object selection. A HAVING-only measure from a different fact correctly flips the planner into CFL mode. The auto-included measure is dropped from the final SELECT so the user only sees columns they asked for.
- **Demo metric `Rolling 30 Day Sales` declares `decimal(18, 0)`** (was `decimal(18, 2)`). A 30-day rolling AVG is a smoothed metric where cent precision is noise — and the .5-cent boundary diverges across engines (DuckDB HALF_UP vs Postgres / MySQL HALF_EVEN vs ClickHouse engine-internal). Whole-dollar precision is engine-stable.

### Fixed

- **CFL NULL-pad type matches the source column for COUNT-style aggregates.** Inner-leg padding for `count` / `count_distinct` measures now uses the *raw column*'s abstract type (e.g. VARCHAR for `complid`) instead of the outer aggregate's declared bigint. Strict-typed engines (Postgres / MySQL / ClickHouse) accept the resulting `UNION ALL`; numeric SUM/AVG aggregates keep the existing outer-CAST-target alignment so the ClickHouse `Decimal` + `Float64` Variant trap stays closed.
- **MySQL `CAST` accepts text values.** `dialect/mysql.py::_compile_cast` translates `VARCHAR[(N)]` → `CHAR[(N)]` at cast time; MySQL's CAST type vocabulary excludes VARCHAR but DDL paths still use the wider type.
- **MySQL ratio precision.** `dialect/mysql.py::render_decimal_division_sql` widens both operands to `DECIMAL(38, 14)` so `div_precision_increment`'s default 4 extra fractional digits doesn't truncate ratios to 6 dp.
- **ClickHouse Decimal division precision.** `_compile_binary_op` and `render_decimal_division_sql` widen `/` operands to `Nullable(Decimal(38, 14))`. Without this, `Decimal(18, 2) / Decimal(18, 2) = Decimal(18, 2)` truncated `Return Rate` to `0.03` instead of `0.0365`.
- **ClickHouse Decimal CAST rounds, not truncates.** `_compile_cast` now wraps the inner expression in `round(x, S)` before `CAST(... AS Decimal(P, S))`. ClickHouse's CAST silently truncated (`CAST(4323.99 AS Decimal(18, 0)) → 4323`), diverging from DuckDB / Postgres / MySQL whose decimal CAST rounds.
- **CI lint job runs cleanly.** `pyproject.toml` now suppresses `pyarrow` `import-untyped` errors via a per-module mypy override (mirroring the existing pandas one). Removed two pre-existing ruff-format drifts (`parser/validator.py`, `tests/unit/test_parser.py`) that were failing CI on `main`.

## [2.2.1] - 2026-05-09

### Changed

- **Bundled demo model rewritten with business-friendly names.** `examples/orionbelt_1_commerce.yaml` now uses spaced YAML keys for every dataObject, column, dimension, measure, and metric. Common base measures use short business names (`Total Sales`, `Total Returns`, `Total Purchases`, `Total Shipments`) — the `Amount` suffix is dropped for amount-typed measures since amounts are the default; quantity/count variants keep their explicit suffixes. Derived metrics follow suit (`Return Rate`, `Average Sale`, `Cumulative Sales`, `MTD Sales`). Physical column names live unchanged in the `code:` fields, so the bundled DuckDB seed and the demo SQL are unaffected. Generic dimensions (`Client Name`, `Country Name`, …) coexist with explicit role-playing variants (`Sales Client Name` via Sales, `Complaint Client Name` via Client Complaints, etc.) so casual queries get business-named dropdowns and cross-fact queries can pin the join path with `via:` to silence `MISSING_VIA` warnings.
- **Demo model pins `settings.defaultDialect: duckdb`.** The bundled image runs against a baked-in DuckDB seed; the dialect is now declared in the model so the UI dropdown auto-selects DuckDB on load instead of falling back to its alphabetical default.
- **Demo model declares `refresh: { mode: static }` on every dataObject.** The bundled DuckDB seed is built into the image and never changes between deploys, so the freshness-driven result cache now uses `CACHE_MAX_TTL_SECONDS` (default 86400 = 1 day) as the effective TTL instead of falling back to the unknown-freshness 300s default. Also sidesteps the `tracked_physical_tables` / `entry_count: 0` divergence when entries land on Cloud Run's per-instance tmpfs and don't survive cold starts.
- **Demo model carries proper data types and display formats.** Adds `settings.defaultNumericDataType: "decimal(18, 2)"` and `settings.defaultTimezone: "Europe/Zagreb"`. Amount-typed measures use `dataType: "decimal(18, 2)"` and `format: "#,##0.00"`; counts use `bigint` + `"#,##0"`; ratio metrics use `decimal(18, 4)` + the auto-percent format `"#,##0.0%"` (the formatter multiplies by 100 at display time, so the stored value stays a raw ratio — `Return Rate = Total Returns / Total Sales` renders as `"12.3%"`).
- **UI: ``Execute Query`` snaps the SQL Dialect dropdown to the API's effective execution dialect** (from `/v1/settings.dialect.effective`) before executing. Lets users explore Postgres/Snowflake/etc. SQL via the Compile preview without accidentally sending a non-DuckDB statement to the underlying database.

### Fixed

- **Result cache silently no-op.** Every `cache.get` / `cache.set` against the file backend's DuckDB meta DB raised `Invalid Input Error: Required module 'pytz' failed to import` — DuckDB's Python binding lazily imports pytz when binding tz-aware datetimes (TIMESTAMPTZ columns), and the WARNING-level failure path returns a miss without surfacing the error. Cache stats showed `entry_count: 0` despite `tracked_physical_tables` accumulating on every query. Pin `pytz>=2024.1` as a runtime dep so DuckDB can persist cache entries.
- **ER diagram label clipping.** Per-attribute right-edge clipping in dense entities is gone: previously a CSS rule injected a 14px font on top of Mermaid's default-12px column-width measurement, so every row's text overflowed its measured rectangle. The override is removed; we now trust Mermaid's measure-equals-render contract.
- **ER diagram attribute names no longer mangle spaces into underscores.** Identifiers are camelCased from the column label (`Sales ID` → `SalesID`) and the spaced business label is emitted as the attribute's quoted comment column, so the diagram shows the human-readable name alongside the structural identifier. Entity names containing spaces (`Client Complaints`, `Account Balances`) are double-quoted so Mermaid renders them verbatim.
- **ER diagram join labels keep their business names** (e.g. `"Sales Client"`) instead of being lower-cased and underscored.

## [2.2.0] - 2026-05-05

### Added

- **`POST /v1/oneshot/batch` — one-shot batch endpoint.** Loads (or references) an OBML model and runs N independent queries against it in a single round trip. Designed for agent workflows where one model + multiple sub-questions is the dominant pattern. Supports `model_yaml` (transient or persisted via `persist_model: true`) or `model_id` (reference an already-loaded model). Queries run concurrently under an `asyncio.Semaphore` capped by `ONESHOT_BATCH_MAX_PARALLELISM` (default 8). Per-query overrides for `dialect` and `execute`. Stable result ordering keyed by caller-provided `id`. Partial failure is the default — each result carries its own `status` (`ok` / `error` / `cancelled`) and `error` envelope. `fail_fast: true` cancels remaining queries on first failure. Whole-batch and per-query timeouts are honored. Server limits surface via `GET /v1/settings.oneshot_batch`. See `design/PLAN_oneshot_batch.md`.
- **Model load deduplication.** `POST /v1/sessions/{sid}/models` and `POST /v1/oneshot/batch` now reuse an existing `model_id` when the same OBML bytes (whitespace-normalized) are already loaded in the session. The response includes a new `model_load` field (`"fresh"` | `"reused"` | `"referenced"` for batch). Skips parsing, validation, and OBSL graph generation on the dedup path. Disable with `dedup: false`. Index is per-session (session isolation preserved) and is cleared automatically on model removal. See `design/PLAN_model_load_dedup.md`.
- **Freshness-driven result cache (file backend, off by default).** New `CACHE_BACKEND=file` mode persists `query/execute` results in `{CACHE_DIR}/meta.duckdb` + Parquet sidecars. TTL is derived from the `freshness:` contract on each touched physical `dataObject` — the *minimum* contribution wins, and an ETL `POST /v1/heartbeat` invalidates every cached query that depends on the refreshed table. Cached `query/execute` responses gain `cached`, `cached_at`, `ttl_seconds`, `ttl_source`, `ttl_limiting_table`, and `physical_tables` fields. Per-query TTL caps via `caller_capped`; unknown-freshness queries skip the cache by default (`CACHE_UNKNOWN_FRESHNESS_POLICY=no_cache`) or fall back to a default TTL when explicitly enabled. Fails closed: any cache error degrades to a normal warehouse execution. See `docs/guide/result-cache.md` and `design/PLAN_freshness_driven_cache.md`.
- **`refresh:` block on dataObjects (OBML + OSI).** New per-dataObject freshness contract with `mode: static | scheduled | heartbeat | unknown`, plus mode-specific fields (`schedule:` cron, `nextRefreshAt:`, `maxStaleness:`, `tolerance:`). Roundtrips through OSI via `osi-obml/osi_obml_converter.py` (custom_extensions) and is declared in the OBSL ontology (`ontology/obsl.ttl`). Surfaced in `GET /v1/settings` and the Settings UI tab.
- **`GET /v1/cache/stats`, `POST /v1/cache/sweep`, `POST /v1/cache/clear`.** Stats exposes backend, entry count, total bytes, hit/miss counters, hit rate, oldest entry, next sweep, tracked physical tables, and heartbeat invalidations. Sweep runs one TTL + LRU capacity eviction pass on demand (returns `ttl_evicted` / `capacity_evicted`). Clear drops every entry regardless of TTL or dependencies (counters preserved as historical telemetry).
- **`POST /v1/heartbeat`.** ETL endpoint invalidating every cached query whose dependency set includes the refreshed `database.schema.table`. Bearer-auth via `HEARTBEAT_AUTH_TOKEN` (route returns 404 when unset). Response lists `invalidated_cache_entries` and `affected_data_objects`.
- **UI Cache panel.** Settings tab now shows a side-by-side **API Settings** + **Cache Stats** view with **Refresh Cache Stats**, **Sweep Cache now**, and **Clear Cache** buttons. Auto-loads on tab open. Query Results tab annotates each execution with `(cache)` or `(database)` next to `execution_time_ms`.

### Changed

- **`execution_time_ms` on cache hits** now reports the actual cache fetch + decode wall time, not the original DB run time persisted in the Parquet sidecar. Combined with the `cached: true` flag, this gives realistic "from cache" durations on the wire. The original DB timing remains on disk for forensic inspection.
- **`CACHE_SWEEP_INTERVAL_SECONDS` default raised from 900s (15 min) to 86400s (1 day).** Lazy TTL on read keeps user-facing freshness correct; the periodic sweeper only matters for reclaiming disk from entries that expire without being read again, so every 15 min was unnecessarily aggressive.
- **Persisted cache state (`meta.duckdb` + `results/`) is wiped on every server startup.** Structural reason: `model_id` is regenerated as a fresh UUID on every model load, so any entries from a previous process run reference model_ids that no longer exist — orphans by construction. Starting empty avoids accumulating dead state between restarts.
- **Cache stats output now uses UTC for both `oldest_entry` and `next_sweep_at`.** Previously `oldest_entry` rendered in the DuckDB session timezone (host local), creating a TZ mismatch with `next_sweep_at` (always UTC). Both are now ISO 8601 with `+00:00`.
- **Startup logging splits the cache config block into one line per setting** so each field is easy to grep without parsing one long line.

### Fixed

- **Gradio UI mounted in `create_app()` always captured `query_execute=False`** because `is_query_execute_enabled()` reads a deps.py global that's only populated inside the `lifespan` hook (which runs *after* the UI is mounted). UI settings now resolve directly from `Settings`, mirroring the same logic the lifespan uses, so the **Execute Query** button appears as soon as `QUERY_EXECUTE=true` is set.
- **Query Results dataframe no longer renders Gradio's default `1, 2, 3` placeholder columns** before the first execution. The dataframe is hidden until the post-execute visibility chain flips it on.

### Settings

- New env vars: `ONESHOT_BATCH_MAX_QUERIES` (50), `ONESHOT_BATCH_MAX_PARALLELISM` (8), `ONESHOT_BATCH_DEFAULT_TIMEOUT_MS` (30000), `ONESHOT_BATCH_BATCH_TIMEOUT_MS` (120000). All exposed in `GET /v1/settings.oneshot_batch`.
- New cache env vars: `CACHE_BACKEND` (`noop` default), `CACHE_DIR`, `CACHE_MIN_TTL_SECONDS` (5), `CACHE_MAX_TTL_SECONDS` (86400), `CACHE_MAX_VALUE_BYTES` (10 MB), `CACHE_MAX_DISK_BYTES` (5 GB), `CACHE_SWEEP_INTERVAL_SECONDS` (86400), `CACHE_UNKNOWN_FRESHNESS_POLICY` (`no_cache`), `CACHE_UNKNOWN_FRESHNESS_DEFAULT_TTL` (300), `HEARTBEAT_AUTH_TOKEN`.

### Behavior change

- Identical OBML loads in the same session now return the same `model_id` instead of minting a new one each time. Disable per-call with `dedup: false`.
- When the cache is enabled (`CACHE_BACKEND=file`), `query/execute` calls may serve cached results. Distinguish via the new `cached` field on the response. The cache fails closed on any error.

## [2.1.4] - 2026-05-03

### Fixed

- **`timeGrain` on a non-temporal column is now rejected at validation time** instead of producing a runtime SQL error. Previously a dimension like `{ column: YearMonth, resultType: string, timeGrain: month }` passed validation but compiled to `date_trunc('month', "Calendar"."ym")`, which Postgres (and other strict dialects) reject with `function date_trunc(unknown, text) does not exist`. The new check inspects the underlying `DataObject.columns[col].abstractType` and emits `TIME_GRAIN_ON_NON_TEMPORAL` unless the type is `date`, `timestamp`, or `timestamp_tz`. Error message includes a remediation hint (drop `timeGrain`, fix the column's `abstractType`, or define a computed column with `to_date()`). Particularly relevant for LLM-generated models, which were the most common source of this mistake.

### Documentation

- `docs/guide/model-format.md` and the `OBML_REFERENCE` resource now document the `timeGrain` constraint inline so LLMs generating OBML have the rule in their prompt context.

## [2.1.3] - 2026-04-30

### Fixed

- **Settings tab `timezone.now` showed UTC even after the v2.1.2 overlay set `timezone.effective: Europe/Berlin`.** The API had computed `now` server-side against its own (no-model) `effective` (UTC), and the v2.1.2 overlay only updated `effective` — leaving `now` stale. The overlay now also recomputes `now` as the current wall clock in the overlaid TZ when the API had no loaded model, so `now` and `effective` agree.

## [2.1.2] - 2026-04-30

### Fixed

- **Settings tab in the Gradio UI showed stale `effective` values** for `timezone` and `dialect` when the user had a model YAML pasted/loaded but had not yet compiled. The UI's `_fetch_settings_yaml` overlay only populated `tz["effective"]` and `dl["effective"]` when the API response was *missing* those keys — but the API always returns them (with no-model fallbacks: `UTC` / `DB_VENDOR`). The result: the overlay correctly added `model_settings` and `timezone.model` from the local YAML, but `effective` stayed at the API's no-model fallback. Now the overlay detects "API has no loaded model" by checking whether `model_settings` was returned by the API, and when absent, fully overlays the local YAML's TZ and dialect — including `effective` — so the Settings tab mirrors what compiling will produce.

## [2.1.1] - 2026-04-30

### Fixed

- **`/v1/settings.timezone.effective` falls back to UTC instead of the model's `defaultTimezone` on slim Linux Docker images.** Root cause: `python:3.12-slim` has no `/usr/share/zoneinfo`, so `ZoneInfo("Europe/Berlin")` (or any IANA name) raises `ZoneInfoNotFoundError`. The resolver caught the exception, walked the fallback chain (host TZ on Cloud Run is UTC and skipped), and ended at `UTC`. Now `tzdata>=2025.1` is a runtime dependency of `orionbelt-semantic-layer`, and both `Dockerfile` / `Dockerfile.ui` also `apt-get install tzdata` for defense in depth.

### Changed

- `examples/sem-layer.obml.yml` (the UI's default preloaded model) now declares `settings.defaultDialect: postgres`. Aligns the UI dialect dropdown and `/v1/settings.dialect.effective` for users loading the bundled demo — previously they could diverge when `DB_VENDOR` differed from the UI's hardcoded `postgres` fallback.

## [2.1.0] - 2026-04-30

### Added

- **Column-level computed columns on `DataObjectColumn`.** A column with `expression:` instead of `code:` defines a computed column inlined wherever the column is referenced. Single-brace `{Column}` placeholders refer to *sibling columns of the same data object*. Distinct from measure-level expressions (which use `{[DataObject].[Column]}` and can cross data objects). Example: `Year-Month: expression: "({Year} * 100 + {Month of Year})"`. Constraints: mutually exclusive with `code`, no recursive resolution. ORDER BY on a computed column is correctly emitted as the inlined expression.
- **Regex, blank, and string-length filter operators** in `QueryFilter`. Per-dialect regex SQL: Postgres `~`/`!~`, DuckDB `regexp_matches`, ClickHouse `match`, BigQuery `REGEXP_CONTAINS`, MySQL `REGEXP`, Databricks `RLIKE`, Snowflake/Dremio `REGEXP_LIKE`. New operators:
  - `regex` / `notregex` — match against a regular-expression pattern (string value)
  - `blank` / `notblank` — `(col IS NULL OR TRIM(col) = '')` and its inverse, for whitespace-aware empty checks on free-text columns
  - `length_eq` / `length_gt` / `length_lt` — `LENGTH(col) {= | > | <} N` (integer value), for fixed-width / padded-code filtering
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

### Fixed

- **CFL outer-aggregate ColumnRef alias shadowing on ClickHouse** (`ILLEGAL_AGGREGATION`). When a multi-fact CFL query mixed a measure with a metric referencing the same measure (e.g. `SUM(net_profit)` plus `SUM(net_profit)/SUM(sales) AS margin`), the metric's bare `SUM("Net Profit")` resolved to the sibling SELECT alias — itself an aggregate — and ClickHouse rejected the resulting nested aggregate. The planner now qualifies every ColumnRef inside outer-query aggregate functions with the composite CTE alias (`composite_01`), forcing resolution to the raw CTE column. Universally safe across all 7 dialects.
- **CFL NULL-padding dialect threading on ClickHouse** (`ILLEGAL_TYPE_OF_ARGUMENT — Variant(Decimal, Float64)`). The single-/zero-column path in `_resolve_null_type_for_field` was missing the `dialect` argument, so it bypassed `resolve_measure_data_type` and fell back to `result_type.value` (`'float'`) — emitting `CAST(NULL AS Nullable(Float64))` while the actual ClickHouse columns are `Decimal(7, 2)`. The `UNION ALL` widened to a `Variant` that `SUM` couldn't consume. Threading `dialect` through aligns NULL padding with the outer CAST target.
- **Raw CFL filter drop & ORDER BY on computed columns.** WHERE filters on data objects unreachable from a leg are now silently skipped per leg (instead of failing the entire query), matching the agg-mode semantics. ORDER BY on a column whose source AST is an inlined expression (computed column) now correctly remaps to the CTE alias in the outer query via structural-equality matching.
- **`/v1/settings` warms the DB session-TZ cache on first hit** when a model is bound and `query_execute` is enabled — and now also without a model bound when the cache is empty. Eliminates the previous one-request lag where the report runner / UI saw `effective: <model TZ>` until a real query happened to populate the probe.

### Docs

- New "Computed Columns" subsection in `guide/model-format.md` covering the column-level `expression:` field, single-brace `{Column}` syntax, the inlining semantics, and a reference-syntax cheat-sheet distinguishing column- vs measure- vs metric-level expressions.
- New "Regex Operators" and "String Length Operators" tables in `guide/query-language.md`, plus `blank` / `notblank` rows added to "Null Operators". Each includes per-dialect generated SQL for portability planning.
- Pinned an explicit `{ #filter-groups }` anchor on the Filter Groups heading so the in-page link `[filter groups](#filter-groups)` survives future heading-text edits.
- Expanded the bundled `examples/tpcds.obml.yml` model with `catalog_sales`, `catalog_returns`, `web_returns`, `inventory`, `warehouse`, `ship_mode`, and `reason` data objects, plus `Manager ID`, `Day Name`, and matching dimensions/measures. Demonstrates multi-fact CFL across three sales channels and exercises the new computed-column dim (`Year-Month`).

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
