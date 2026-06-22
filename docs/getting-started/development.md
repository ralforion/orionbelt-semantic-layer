# Development

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Install Dependencies

```bash
uv sync
```

This installs everything — dev tools, docs, UI, Flight SQL drivers, and type stubs.

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

## Changing the OBML model

OBML is the single source of truth for the project. Every type, enum, and
field is mirrored across several dependent artifacts that must move together:

1. **Pydantic models** — `src/orionbelt/models/semantic.py` (and `models/query.py`, `models/errors.py`)
2. **JSON schema** — `schema/obml-schema.json` (and `schema/query-schema.json`)
3. **Ontology** — `ontology/obsl.ttl` (class + properties) and `obsl.shacl.ttl`
4. **OSI converter** — `packages/osi-orionbelt` (custom_extensions round-trip)
5. **MkDocs + REST API docs**, and **tests/fixtures**

### The contract manifest

`schema/obml-contract.yml` is a hand-maintained manifest that records the OBML
field surface in one place: every enum and class, and per field its camelCase
`alias`, whether it appears in the JSON schema (`json_schema`), its ontology
property (`ontology_property`), and OSI round-trip behaviour (`osi_roundtrip`).

### Schema validation at the API boundary

The API validates raw model and query payloads against the published JSON
Schemas *before* processing them (see `api/schema_guards.py`). Coverage spans
every OBML/QueryObject ingestion point: session and shortcut model-load and
query endpoints, the oneshot batch (its inline `model_yaml` and each query),
and the `MODEL_FILES` startup preload. Model documents validate against
`obml-schema.json`, query payloads against `query-schema.json`. A contract
violation returns HTTP 422 (or fails startup for `MODEL_FILES`). SQL-input
surfaces (OBSQL / pgwire / Flight) are out of scope: they receive SQL, not
OBML/QueryObject JSON, and build a trusted `QueryObject` internally. This makes the JSON Schema a load-bearing
gate that every real request exercises - so the published contract stays
provably correct and external consumers can rely on the same rules the engine
enforces. The schemas are camelCase-only; snake_case keys that Pydantic would
otherwise coerce are rejected at the boundary.

### Keeping the manifest honest

`tests/unit/test_obml_contract.py` keeps the manifest in sync with the live
Pydantic models, the JSON schema, and the ontology. **Adding or removing a
field on the Pydantic models without updating the manifest fails this test** -
that is the intended early-warning gate that you also need to touch the other
dependents.

When you change OBML:

1. Update the Pydantic model in `models/semantic.py`.
2. Add/update the field (or enum value) in `schema/obml-contract.yml`. Set
   `json_schema` / `ontology_property` to match the schema and ontology you are
   about to update. If a field should never be part of the contract, add it to
   `MANIFEST_FIELD_EXCLUSIONS` in the test with a comment.
3. Update the JSON schema, ontology, OSI converter, docs, and fixtures.
4. Run `uv run pytest tests/unit/test_obml_contract.py` (plus the schema and OSI
   drift tests) until green.
