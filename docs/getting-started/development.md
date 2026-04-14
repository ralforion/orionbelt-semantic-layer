# Development

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Install Dependencies

```bash
# Main dependencies only
uv sync

# All dependencies (dev tools, docs, UI, type stubs)
uv sync --all-extras --all-groups
```

## Run Tests

```bash
uv run pytest                     # all tests
uv run pytest tests/unit/         # unit tests only
uv run pytest tests/integration/  # integration tests only
uv run pytest -k "test_revenue"   # by name pattern
```

## Code Quality

```bash
uv run ruff check src/            # lint
uv run ruff format src/ tests/    # format
uv run mypy src/                  # type check
```

## Build Documentation

```bash
uv sync --extra docs
uv run mkdocs serve               # docs at http://127.0.0.1:8080
```

## Project Structure

```
src/orionbelt/
  api/          REST API (FastAPI routers, middleware, schemas)
  ast/          SQL AST node definitions (frozen dataclasses)
  compiler/     Compilation pipeline (resolution, star, cfl, codegen)
  dialect/      8 SQL dialect implementations (self-registering)
  models/       Pydantic v2 models (semantic model, query, errors)
  obsl/         OBSL-Core RDF graph exporter and SPARQL engine
  parser/       YAML loader, reference resolver, semantic validator
  service/      Session manager, model store
  ui/           Gradio web interface
```
