# Installation

## Prerequisites

- **Python 3.12+**
- [**uv**](https://docs.astral.sh/uv/) — fast Python package manager (recommended)

## Clone the Repository

```bash
git clone https://github.com/ralfbecher/orionbelt-semantic-layer.git
cd orionbelt-semantic-layer
```

## Install Dependencies

```bash
uv sync
```

This installs all dependencies — runtime, development tools, UI, Flight SQL drivers, and docs — via the default `dev` dependency group. No extra flags needed.

## Verify the Installation

```bash
# Run the test suite
uv run pytest

# Type check
uv run mypy src/

# Lint
uv run ruff check src/
```

## Configuration

OrionBelt reads configuration from environment variables and a `.env` file. Copy the example:

```bash
cp .env.template .env
```

Key settings:

| Variable                   | Default     | Description                                 |
| -------------------------- | ----------- | ------------------------------------------- |
| `LOG_LEVEL`                | `INFO`      | Logging level                               |
| `API_SERVER_HOST`          | `localhost` | REST API bind host                          |
| `API_SERVER_PORT`          | `8000`      | REST API bind port                          |
| `SESSION_TTL_SECONDS`      | `1800`      | Session inactivity timeout (30 min)         |
| `SESSION_MAX_AGE_SECONDS`  | `86400`     | Absolute max session lifetime (24 h)        |
| `SESSION_CLEANUP_INTERVAL` | `60`        | Cleanup sweep interval (seconds)            |
| `MAX_SESSIONS`             | `500`       | Global concurrent session cap (429 when full) |
| `MAX_MODELS_PER_SESSION`   | `10`        | Max models a single session may hold        |
| `SESSION_RATE_LIMIT`       | `10`        | Max `POST /sessions` per IP per minute      |
| `MODEL_FILES`              | —           | Comma-separated OBML YAML paths for admin-curated mode |
| `FLIGHT_ENABLED`           | `false`     | Enable Flight SQL + query execution         |
| `DB_VENDOR`                | `duckdb`    | Database vendor for query execution         |

See `.env.template` for the full list including database credentials.

### Admin-Curated Mode

Set `MODEL_FILES` to pre-load one or more OBML models. Each model lands in its own named protected session (addressing name = OBML `name:` field or filename stem); REST model upload/removal endpoints return 403 while the flag is on. A single path is fine — that's the simplest production layout.

## Start the Servers

### REST API

```bash
uv run orionbelt-api
# or with reload:
uv run uvicorn orionbelt.api.app:create_app --factory --reload
```

The API is available at:

- `http://127.0.0.1:8000` — API root
- `http://127.0.0.1:8000/docs` — Swagger UI
- `http://127.0.0.1:8000/redoc` — ReDoc
- `http://127.0.0.1:8000/health` — Health check

## Project Structure

```
orionbelt-semantic-layer/
├── src/orionbelt/
│   ├── api/            # FastAPI app, routers, schemas, deps, middleware
│   │   └── routers/    # sessions, validate, query, dialects
│   ├── ast/            # SQL AST nodes, builder, visitor
│   ├── compiler/       # Resolution, planning (star/CFL), codegen pipeline
│   ├── dialect/        # 8 SQL dialect implementations
│   ├── models/         # Pydantic models (semantic, query, errors)
│   ├── parser/         # YAML loader, reference resolver, validator
│   ├── service/        # ModelStore, SessionManager
│   └── settings.py     # Shared configuration
├── tests/
│   ├── unit/           # Unit tests for each module
│   ├── integration/    # End-to-end compilation and API tests
│   └── fixtures/       # Sample models and queries
├── examples/           # Model examples and JSON Schema
├── schema/             # OBML JSON Schema
├── docs/               # MkDocs documentation source
├── mkdocs.yml          # MkDocs configuration
└── pyproject.toml      # Project metadata and dependencies
```
