# AGENTS.md

This file provides shared guidance for coding agents working in this repository.

## Project Overview

**OrionBelt Semantic Layer** is a SaaS semantic layer engine that compiles and executes YAML semantic models (OBML format) as analytical SQL across 8 database dialects: BigQuery, ClickHouse, Databricks, Dremio, DuckDB, MySQL, Postgres, Snowflake. It exposes all capabilities through a REST API (FastAPI). An MCP server is available as a separate thin client in [orionbelt-semantic-layer-mcp](https://github.com/ralforion/orionbelt-semantic-layer-mcp).

## Commands

```bash
uv sync                           # all deps (dev, docs, ui, flight, drivers)

uv run orionbelt-api              # REST API on :8000
uv run orionbelt-ui               # Gradio UI
uv run obsl --help                # CLI: validate/compile/execute/convert/describe/diagram/graph (local-first)

uv run pytest                     # all tests
uv run pytest tests/unit/test_compiler.py::TestClass::test_method  # single test
uv run pytest -k "test_revenue"   # by name pattern

uv run ruff check src/            # lint
uv run ruff format src/ tests/    # format
uv run mypy src/                  # type check

uv sync --extra docs && uv run mkdocs serve  # docs on :8080

# Docker: two separate images (API + UI)
docker build -t orionbelt-semantic-layer-api .                  # API-only image
docker build -f Dockerfile.ui -t orionbelt-semantic-layer-ui .  # UI image (Gradio)

./scripts/release.sh                              # public release (PyPI + docs + tag-triggered Docker publish)
./scripts-infra/deploy-gcloud.sh                  # Cloud Run deploy (both services; infra repo, maintainer-only)
./tests/docker/test_docker.sh                     # 15 local Docker tests
./tests/cloudrun/test_cloudrun.sh <CLOUD_RUN_URL> # 30 live API tests
```

## Code Review

Code changes are reviewed with **OpenAI Codex**. Write clean, well-structured code that passes automated review. Avoid unnecessary complexity, dead code, or patterns that would trigger review warnings. Ensure all changes pass `ruff check`, `ruff format`, and `mypy` before submitting.

## Architecture — Compilation Pipeline

`QueryObject + SemanticModel` flow through these stages, orchestrated by `CompilationPipeline` in `compiler/pipeline.py`:

1. **Resolution** (`compiler/resolution.py`) — selects base object (fact table), resolves refs, determines join paths, classifies filters; sets `requires_cfl=True` only when measures span truly independent facts (directed reachability check via JoinGraph).
2. **Fanout detection** (`compiler/fanout.py`) — raises `FanoutError` if reversed many-to-one joins would multiply rows.
3. **Planner** — `compiler/star.py` (single-fact star schema, LEFT JOINs) or `compiler/cfl.py` (multi-fact CFL, UNION ALL + NULL padding; common root per leg via `JoinGraph.find_common_root()`).
4. **Total wrap** (optional, `compiler/total_wrap.py`) — `AGG(x) OVER ()` window CTEs for measures with `total=True`.
5. → **SQL AST** (frozen dataclasses in `ast/nodes.py`).
6. **Codegen** (`compiler/codegen.py` + `dialect/*.py`) — AST → SQL string.
7. **Validate** (`compiler/validator.py`) — sqlglot post-gen check (non-blocking).

## Key Subsystems

### Dialect Registry (`dialect/`)
Dialects self-register via `@DialectRegistry.register` decorator. `dialect/__init__.py` imports all 8 modules to trigger registration. `DialectRegistry.get(name)` returns a fresh instance. Each dialect implements `quote_identifier()`, `render_time_grain()`, `render_cast()`, `current_date_sql()`, `date_add_sql()`, and `compile_expr()` (uses `match` on AST nodes).

### SQL AST (`ast/nodes.py`)
All nodes are frozen dataclasses. Key types: `Select`, `From`, `Join`, `CTE`, `UnionAll`, `ColumnRef`, `AliasedExpr`, `FunctionCall`, `BinaryOp`, `WindowFunction`, `CaseExpr`, `Cast`, `Literal`, `RawSQL`. The union type `Expr` covers all expression nodes.

### Session Management (`service/`)
`SessionManager` holds TTL-scoped sessions, each with its own `ModelStore`. Thread-safe via `threading.Lock`. Background daemon thread purges expired sessions. Default session (`__default__`) is auto-created for MCP stdio mode. REST API uses `api/deps.py` singleton pattern with FastAPI `lifespan` context manager. **Important:** `httpx.ASGITransport` does NOT trigger lifespan — tests must manually call `init_session_manager()`.

### Parser (`parser/`)
Two distinct validators exist — don't confuse them:
- `parser/validator.py` — **SemanticValidator**: validates the OBML model (cycles, duplicate names, invalid refs)
- `compiler/validator.py` — **SQL validator**: post-generation sqlglot syntax check (non-blocking warnings)

`TrackedLoader` uses ruamel.yaml for line-faithful source positions. `ReferenceResolver` converts raw dict → `SemanticModel` + `ValidationResult`.

## Pydantic v2 Alias Convention

All models use `Field(alias="camelCase")` with `populate_by_name=True`. YAML/JSON uses camelCase aliases; Python code uses snake_case field names. Mypy only sees the Python names.

Key aliases: `data_objects` → `"dataObjects"`, `join_to` → `"joinTo"`, `columns_from` → `"columnsFrom"`, `columns_to` → `"columnsTo"`, `path_name` → `"pathName"`, `use_path_names` → `"usePathNames"`, `abstract_type` → `"abstractType"`, `result_type` → `"resultType"`, `join_type` → `"joinType"`, `time_grain` → `"timeGrain"`. `DataColumnRef.view` and `Dimension.view` both alias to `"dataObject"`.

When constructing models in Python, always use the Python field names (e.g., `data_type=`, `view=`), not the aliases.

## OBML Format — Single Source of Truth

OBML defines all types, enums, error codes, operators, and semantics for the project. **When OBML changes, all dependents must be updated together:**

1. **Python models** — `models/semantic.py`, `models/query.py`, `models/errors.py`
2. **MCP server** — separate repo (tool descriptions, prompts, `OBML_REFERENCE` resource)
3. **REST API** — `api/` (endpoint docs, OpenAPI descriptions)
4. **MkDocs** — `docs/` (guide pages, examples, reference)
5. **JSON Schema** — `schema/obml-schema.json`, `schema/query-schema.json`
6. **Tests & fixtures** — `tests/`, `tests/fixtures/`

Every new OBML field must also propagate to the OSI converter (`packages/osi-orionbelt`, custom_extensions roundtrip) and the ontology (`ontology/obsl.ttl` class + properties, `obsl.shacl.ttl` shapes). Never change any dependent without checking consistency with OBML and all other dependents.

The field surface is captured in the hand-maintained manifest `schema/obml-contract.yml`; `tests/unit/test_obml_contract.py` fails if a Pydantic field is added/removed without updating it, and checks the manifest against the JSON schema and ontology. See `docs/getting-started/development.md` ("Changing the OBML model").

Top-level YAML keys: `version`, `dataObjects`, `dimensions`, `measures`, `metrics`, `filters`.

- **Column names are unique within each data object** — dimensions, measures, and metrics must be unique across the whole model
- **Measure expressions** reference columns by data object + column: `{[DataObject].[Column]}`
- **Metric expressions** reference measures by name: `{[Measure Name]}`
- **Secondary joins**: `secondary: true` + `pathName` on `DataObjectJoin` — unique per (source, target) pair
- **Synthesized counts**: every countable `dataObject` yields a grain-anchored row-count measure `<object>.count` (an anchored `COUNT(*)`, integer-typed). Counts are **named measures — no ad-hoc aggregation in queries**; reference `Sales.count` like any measure. Knobs: per-object `countable: false` (opt-out) / `countLabel`; model-level `exposeCounts: false` / `countLabelPattern` (only the `{object}` token, interpolates the object's display label). A declared `<object>.count` measure overrides synthesis (e.g. `COUNT(DISTINCT pk)`). Synthesized counts are computed via `SemanticModel.effective_measures` (declared win) and are never persisted — they don't roundtrip through YAML/OSI (the knobs do, via `synthesis.py`). A `dataObject` is never a queryable FROM target.
- **Queries** use `select: {dimensions: [...], measures: [...]}` structure with optional `where`, `having`, `orderBy`, `limit`, `usePathNames`

## REST API

FastAPI app in `api/`; routers under `api/routes/`. All routes are prefixed `/v1/` except `/health` and `/robots.txt`. The **authoritative, always-current** endpoint list is the running OpenAPI — browse `/docs` (Swagger) or `/openapi.json`. Notable surfaces:

- Session CRUD + per-session model management (`/v1/sessions/...`); load model field is `model_yaml`
- `query/sql` (compile + explain), `query/execute` (`?format=tsv`, `?format_values=true`, `?locale=`, `?timezone=`)
- `query/semantic-ql` + `.../compile` — OBSQL, BI-style `SELECT dim, measure FROM <model>`
- OSI convert (`/v1/convert/osi-to-obml`, `/v1/convert/obml-to-osi`; stateless), and per-model `osi` export / `from-osi` load
- ACR `composables` (Artefacts Composability Resolution), RDF `graph` (Turtle) + `sparql`, `diagram/er` (Mermaid), `explain/{name}`, `find`, `join-graph`
- Result `cache/stats|sweep|clear`, `oneshot/batch`, `/v1/models` (admin multi-model mode), `/v1/reference/...`

Top-level shortcuts (`/v1/query/execute`, `/v1/schema`, `/v1/dimensions`, ...) auto-resolve when a single session/model exists.

## Configuration

Env vars / `.env` via pydantic-settings (the `Settings` model is the source of truth). Most are self-explanatory; the load-bearing / non-obvious ones:

- `MODEL_FILES` — comma-separated OBML YAML paths for admin-curated mode. Each loads into its own *named protected session*, addressable by the OBML `name:` (fallback: filename stem, normalized to `[a-z][a-z0-9_]{0,62}`). BI tools select via Flight `database` catalog or pgwire `database=`. (Legacy `MODEL_FILE` removed in v2.7.0.)
- `AUTH_MODE` — `none` | `api_key` | `oidc` (oidc not yet implemented). `API_KEYS` required for `api_key`; key sent via `API_KEY_HEADER` (default `X-API-Key`; `Authorization: Bearer` also accepted). `AUTH_ENABLED` is a deprecated alias.
- `LOG_FORMAT` — `console` | `json` | `cloudrun` (cloudrun = JSON, no access logs).
- `EXPOSE_API_DOCS` / `EXPOSE_OPENAPI_SCHEMA` — toggle `/docs`+`/redoc` and `/openapi.json` (hide on non-demo deploys).
- `PORT` (Cloud Run override), `SESSION_TTL_SECONDS` (1800), `DEFAULT_LOCALE`, `API_BASE_URL` / `ROOT_PATH` (standalone UI behind a load balancer).

## Test Structure

- `tests/conftest.py` — shared fixtures: `sales_model` (resolved SemanticModel), `SAMPLE_MODEL_YAML` (inline 2-table model); `tests/fixtures/sales_model/model.yaml` is the full multi-table model.
- `tests/unit/` (per-subsystem), `tests/integration/` (`test_api.py` via httpx ASGI, `test_compilation_e2e.py`). pytest config: `asyncio_mode = "auto"`, `testpaths = ["tests"]`.
- Shell suites: `tests/docker/test_docker.sh` (local container), `tests/cloudrun/test_cloudrun.sh <URL>` (live deployment).

## Tooling Notes

- Python 3.12+, `from __future__ import annotations` everywhere
- Ruff rules: `["E", "F", "I", "N", "UP", "B", "A", "SIM"]`, line-length 100
- mypy strict mode with `pydantic.mypy` plugin; needs `types-networkx` and `types-PyYAML` stubs
- `list` is invariant in mypy: `list[Literal]` != `list[Expr]` — annotate with `list[Expr]`
- ruamel.yaml: `data.lc.key(key)` is a method call (not dict access); always wrap in try/except
- `uv sync` installs all deps (dev, docs, ui, flight, drivers) via the default `dev` dependency group
