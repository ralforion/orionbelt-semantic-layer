# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**OrionBelt Semantic Layer** is a SaaS semantic layer engine that compiles and executes YAML semantic models (OBML format) as analytical SQL across 8 database dialects: BigQuery, ClickHouse, Databricks, Dremio, DuckDB, MySQL, Postgres, Snowflake. It exposes all capabilities through a REST API (FastAPI). An MCP server is available as a separate thin client in [orionbelt-semantic-layer-mcp](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp).

## Commands

```bash
# Install
uv sync                           # main deps only
uv sync --all-extras --all-groups # all deps (dev, docs, ui, type stubs)

# Run servers
uv run orionbelt-api              # REST API on :8000
uv run orionbelt-ui               # Gradio UI (requires --extra ui)

# Tests
uv run pytest                     # all tests
uv run pytest tests/unit/test_compiler.py  # single file
uv run pytest tests/unit/test_compiler.py::TestClass::test_method  # single test
uv run pytest -k "test_revenue"   # by name pattern

# Quality
uv run ruff check src/            # lint
uv run ruff format src/ tests/    # format
uv run mypy src/                  # type check

# Docs
uv sync --extra docs && uv run mkdocs serve  # docs on :8080

# Docker (two separate images: API and UI)
docker build -t orionbelt-api .                             # API-only image
docker build -f Dockerfile.ui -t orionbelt-ui .             # UI image (Gradio)
docker run -p 8080:8080 orionbelt-api                       # run API
docker run -p 7860:7860 -e API_BASE_URL=http://host.docker.internal:8080 orionbelt-ui  # run UI

# Cloud Run deployment (deploys both services)
./scripts/deploy-gcloud.sh

# Tests
./tests/docker/test_docker.sh                    # 15 local Docker tests
./tests/cloudrun/test_cloudrun.sh <CLOUD_RUN_URL> # 30 live API tests
```

## Architecture — Compilation Pipeline

```
QueryObject + SemanticModel
        │
        ▼
  ┌─────────────┐
  │  Resolution  │  compiler/resolution.py — selects base object (fact table),
  │              │  resolves refs, determines join paths, classifies filters,
  │              │  sets requires_cfl=True only when measures span truly
  │              │  independent facts (directed reachability check via JoinGraph)
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │   Fanout    │  compiler/fanout.py — raises FanoutError if reversed
  │  Detection  │  many-to-one joins would cause row multiplication
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │   Planner   │  compiler/star.py — single-fact star schema (LEFT JOINs)
  │             │  compiler/cfl.py  — multi-fact CFL (UNION ALL + NULL padding)
  │             │  CFL uses common root per leg via JoinGraph.find_common_root()
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │ Total Wrap  │  compiler/total_wrap.py — AGG(x) OVER () window CTEs
  │  (optional) │  for measures with total=True
  └──────┬──────┘
         │
         ▼
    SQL AST (frozen dataclasses in ast/nodes.py)
         │
         ▼
  ┌─────────────┐
  │  Codegen    │  compiler/codegen.py + dialect/*.py — AST → SQL string
  └──────┬──────┘
         │
         ▼
  ┌─────────────┐
  │  Validate   │  compiler/validator.py — sqlglot post-gen check (non-blocking)
  └─────────────┘
```

The pipeline is orchestrated by `CompilationPipeline` in `compiler/pipeline.py`.

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

### MCP Server (separate repo)
The MCP server lives in [orionbelt-semantic-layer-mcp](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp) — a thin HTTP client that delegates to the REST API. It is not part of this repository.

## Pydantic v2 Alias Convention

All models use `Field(alias="camelCase")` with `populate_by_name=True`. YAML/JSON uses camelCase aliases; Python code uses snake_case field names. Mypy only sees the Python names.

Key aliases: `data_objects` → `"dataObjects"`, `join_to` → `"joinTo"`, `columns_from` → `"columnsFrom"`, `columns_to` → `"columnsTo"`, `path_name` → `"pathName"`, `use_path_names` → `"usePathNames"`, `abstract_type` → `"abstractType"`, `result_type` → `"resultType"`, `join_type` → `"joinType"`, `time_grain` → `"timeGrain"`. `DataColumnRef.view` and `Dimension.view` both alias to `"dataObject"`.

When constructing models in Python, always use the Python field names (e.g., `data_type=`, `view=`), not the aliases.

## OBML Format — Single Source of Truth

OBML defines all types, enums, error codes, operators, and semantics for the project. **When OBML changes, all dependents must be updated together:**

1. **Python models** — `models/semantic.py`, `models/query.py`, `models/errors.py`
2. **MCP server** — `mcp/server.py` (tool descriptions, prompts, `OBML_REFERENCE` resource)
3. **REST API** — `api/` (endpoint docs, OpenAPI descriptions)
4. **MkDocs** — `docs/` (guide pages, examples, reference)
5. **JSON Schema** — `schema/obml-schema.json`, `schema/query-schema.json`
6. **Tests & fixtures** — `tests/`, `tests/fixtures/`

Never change any dependent without checking consistency with OBML and all other dependents.

Top-level YAML keys: `version`, `dataObjects`, `dimensions`, `measures`, `metrics`.

- **Column names are unique within each data object** — dimensions, measures, and metrics must be unique across the whole model
- **Measure expressions** reference columns by data object + column: `{[DataObject].[Column]}`
- **Metric expressions** reference measures by name: `{[Measure Name]}`
- **Secondary joins**: `secondary: true` + `pathName` on `DataObjectJoin` — unique per (source, target) pair
- **Queries** use `select: {dimensions: [...], measures: [...]}` structure with optional `where`, `having`, `order_by`, `limit`, `usePathNames`

## Test Structure

- `tests/conftest.py` — shared fixtures: `sales_model` (resolved SemanticModel), `SAMPLE_MODEL_YAML` (inline 2-table model)
- `tests/unit/` — 14 files covering AST, compiler, dialects, fanout, graph, MCP, parser, validator, etc.
- `tests/integration/` — `test_api.py` (FastAPI via httpx ASGI), `test_compilation_e2e.py`
- `tests/fixtures/sales_model/model.yaml` — full multi-table model used by fixtures
- `tests/docker/test_docker.sh` — 15 endpoint tests against local Docker container
- `tests/cloudrun/test_cloudrun.sh` — 30 endpoint tests against live Cloud Run deployment
- pytest config: `asyncio_mode = "auto"`, `testpaths = ["tests"]`

## REST API Endpoints

All API routes are prefixed with `/v1/` except `/health` and `/robots.txt`.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check (returns version) |
| GET | `/v1/settings` | Public config (single-model mode, TTL) |
| GET | `/v1/dialects` | List 8 dialects with capabilities |
| POST | `/v1/sessions` | Create session |
| GET | `/v1/sessions` | List sessions |
| GET | `/v1/sessions/{id}` | Get session info |
| DELETE | `/v1/sessions/{id}` | Close session |
| POST | `/v1/sessions/{id}/models` | Load model (field: `model_yaml`) |
| GET | `/v1/sessions/{id}/models` | List models |
| GET | `/v1/sessions/{id}/models/{mid}` | Describe model |
| DELETE | `/v1/sessions/{id}/models/{mid}` | Remove model |
| POST | `/v1/sessions/{id}/validate` | Validate YAML |
| POST | `/v1/sessions/{id}/query/sql` | Compile query (includes explain) |
| POST | `/v1/sessions/{id}/query/execute` | Compile and execute query (requires FLIGHT_ENABLED) |
| GET | `/v1/sessions/{id}/models/{mid}/diagram/er` | Mermaid ER diagram |
| GET | `/v1/sessions/{id}/models/{mid}/schema` | Full model as JSON |
| GET | `/v1/sessions/{id}/models/{mid}/dimensions` | List dimensions |
| GET | `/v1/sessions/{id}/models/{mid}/dimensions/{name}` | Get dimension |
| GET | `/v1/sessions/{id}/models/{mid}/measures` | List measures |
| GET | `/v1/sessions/{id}/models/{mid}/measures/{name}` | Get measure |
| GET | `/v1/sessions/{id}/models/{mid}/metrics` | List metrics |
| GET | `/v1/sessions/{id}/models/{mid}/metrics/{name}` | Get metric |
| GET | `/v1/sessions/{id}/models/{mid}/explain/{name}` | Lineage explain |
| POST | `/v1/sessions/{id}/models/{mid}/find` | Search artefacts |
| GET | `/v1/sessions/{id}/models/{mid}/join-graph` | Join graph adjacency |
| POST | `/v1/convert/osi-to-obml` | Convert OSI YAML → OBML YAML |
| POST | `/v1/convert/obml-to-osi` | Convert OBML YAML → OSI YAML |

Top-level shortcuts (auto-resolve when single session/model): `/v1/schema`, `/v1/dimensions`, `/v1/measures`, `/v1/metrics`, `/v1/explain/{name}`, `/v1/find`, `/v1/join-graph`, `/v1/query/sql`, `/v1/query/execute`.

## Configuration

Environment variables or `.env` file (via pydantic-settings):

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | — | Cloud Run override (takes precedence) |
| `API_SERVER_HOST` | `localhost` | REST API bind host |
| `API_SERVER_PORT` | `8000` | REST API port |
| `DISABLE_SESSION_LIST` | `false` | Disable `GET /sessions` endpoint (security) |
| `SESSION_TTL_SECONDS` | `1800` | Session timeout |
| `SESSION_CLEANUP_INTERVAL` | `60` | Cleanup sweep interval |
| `MODEL_FILE` | — | Path to OBML YAML for single-model mode |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_FORMAT` | `console` | `console` (pretty) or `json` (structured for cloud) |
| `API_BASE_URL` | — | API URL for standalone UI |
| `ROOT_PATH` | — | ASGI root path for UI behind load balancer |

## Tooling Notes

- Python 3.12+, `from __future__ import annotations` everywhere
- Ruff rules: `["E", "F", "I", "N", "UP", "B", "A", "SIM"]`, line-length 100
- mypy strict mode with `pydantic.mypy` plugin; needs `types-networkx` and `types-PyYAML` stubs
- `list` is invariant in mypy: `list[Literal]` != `list[Expr]` — annotate with `list[Expr]`
- ruamel.yaml: `data.lc.key(key)` is a method call (not dict access); always wrap in try/except
- `uv sync` without `--all-extras --all-groups` skips dev/docs/ui deps
