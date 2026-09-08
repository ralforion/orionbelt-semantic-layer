---
description: "OrionBelt generates interactive OpenAPI and Swagger documentation from its FastAPI routes and Pydantic schemas, served next to the running API."
---

# OpenAPI / Swagger

OrionBelt auto-generates interactive API documentation from its FastAPI route definitions and Pydantic schemas.

## Swagger UI

Available at:

```
http://127.0.0.1:8000/docs
```

Swagger UI provides an interactive interface to explore and test all API endpoints. You can:

- Browse all available endpoints grouped by tags
- See request/response schemas with examples
- Execute requests directly from the browser
- View detailed parameter descriptions and validation rules

## ReDoc

Available at:

```
http://127.0.0.1:8000/redoc
```

ReDoc provides a clean, readable API reference with:

- Three-panel layout (navigation, documentation, request/response examples)
- Nested schema visualization
- Search functionality

## OpenAPI JSON Schema

The raw OpenAPI 3.1 specification is available at:

```
http://127.0.0.1:8000/openapi.json
```

You can use this with any OpenAPI-compatible tool for:

- Client code generation (e.g., `openapi-generator`)
- API testing tools (e.g., Postman, Insomnia)
- Documentation hosting (e.g., Stoplight, ReadMe)

## Starting the Server

```bash
uv run orionbelt-api
# or with reload:
uv run uvicorn orionbelt.api.app:create_app --factory --reload
```

The `--reload` flag enables auto-reload during development. The `--factory` flag tells uvicorn to call `create_app()` to get the FastAPI instance.

## API Tags

Endpoints are grouped by the following tags in the OpenAPI spec:

| Tag | Endpoints |
|-----|-----------|
| `sessions` | `/sessions`, `/sessions/{id}`, `/sessions/{id}/models`, `/sessions/{id}/models/{mid}`, `/sessions/{id}/validate`, `/sessions/{id}/query/sql` |
| `dialects` | `/dialects` |
| `health` | `/health` |
