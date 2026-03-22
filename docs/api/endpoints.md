# API Endpoints

Complete reference for all OrionBelt REST API endpoints.

## Health Check

### `GET /health`

Returns the service status and version.

**Response:**

```json
{
  "status": "ok",
  "version": "1.2.0"
}
```

---

## Sessions

### `POST /v1/sessions`

Create a new session. Each session has its own model store.

**Request (optional):**

```json
{
  "metadata": {
    "user": "alice",
    "purpose": "revenue analysis"
  }
}
```

**Response (201):**

```json
{
  "session_id": "a1b2c3d4e5f6",
  "created_at": "2025-01-15T10:30:00Z",
  "last_accessed_at": "2025-01-15T10:30:00Z",
  "model_count": 0,
  "metadata": {
    "user": "alice",
    "purpose": "revenue analysis"
  }
}
```

### `GET /v1/sessions`

List all active sessions.

**Response (200):**

```json
{
  "sessions": [
    {
      "session_id": "a1b2c3d4e5f6",
      "created_at": "2025-01-15T10:30:00Z",
      "last_accessed_at": "2025-01-15T10:35:00Z",
      "model_count": 2,
      "metadata": {}
    }
  ]
}
```

### `GET /v1/sessions/{session_id}`

Get info for a specific session. Also refreshes the session's last-accessed time.

**Response (200):** Same as single session in list response.

**Error (404):** Session not found or expired.

### `DELETE /v1/sessions/{session_id}`

Close a session and release its resources.

**Response (204):** No content.

**Error (404):** Session not found.

---

## Session Models

### `POST /v1/sessions/{session_id}/models`

Load an OBML semantic model into a session. The model is parsed, validated, and stored.

!!! note "Single-model mode"
    Returns **403 Forbidden** when `MODEL_FILE` is configured. The model is pre-loaded automatically.

**Request:**

```json
{
  "model_yaml": "version: 1.0\ndataObjects:\n  Orders:\n    code: ORDERS\n    ..."
}
```

**Response (201):**

```json
{
  "model_id": "abcd1234",
  "data_objects": 2,
  "dimensions": 3,
  "measures": 2,
  "metrics": 1,
  "warnings": []
}
```

**Error (403):** Single-model mode: model upload is disabled.

**Error (422):** Model has validation errors.

**Error (404):** Session not found.

### `GET /v1/sessions/{session_id}/models`

List all models loaded in a session.

**Response (200):**

```json
[
  {
    "model_id": "abcd1234",
    "data_objects": 2,
    "dimensions": 3,
    "measures": 2,
    "metrics": 1
  }
]
```

### `GET /v1/sessions/{session_id}/models/{model_id}`

Describe a model's contents — data objects (with fields and joins), dimensions, measures, and metrics.

**Response (200):**

```json
{
  "model_id": "abcd1234",
  "data_objects": [
    {
      "label": "Orders",
      "code": "WAREHOUSE.PUBLIC.ORDERS",
      "columns": ["Order ID", "Price", "Quantity"],
      "join_targets": ["Customers"]
    }
  ],
  "dimensions": [
    {
      "name": "Country",
      "result_type": "string",
      "data_object": "Customers",
      "column": "Country",
      "time_grain": null
    }
  ],
  "measures": [...],
  "metrics": [...]
}
```

**Error (404):** Model or session not found.

### `DELETE /v1/sessions/{session_id}/models/{model_id}`

Remove a model from a session.

!!! note "Single-model mode"
    Returns **403 Forbidden** when `MODEL_FILE` is configured.

**Response (204):** No content.

**Error (403):** Single-model mode: model removal is disabled.

**Error (404):** Model or session not found.

---

## Session Validation

### `POST /v1/sessions/{session_id}/validate`

Validate OBML YAML within a session context. Does not store the model.

**Request:**

```json
{
  "model_yaml": "version: 1.0\ndataObjects:\n  ..."
}
```

**Response (200):**

```json
{
  "valid": true,
  "errors": [],
  "warnings": []
}
```

**Validation failure:**

```json
{
  "valid": false,
  "errors": [
    {
      "code": "UNKNOWN_DATA_OBJECT",
      "message": "Data object 'Unknown' not found",
      "path": "dimensions.Bad.dataObject"
    }
  ],
  "warnings": []
}
```

---

## Session Query Compilation & Execution

### `POST /v1/sessions/{session_id}/query/sql`

Compile a semantic query against a model loaded in the session.

**Request:**

```json
{
  "model_id": "abcd1234",
  "query": {
    "select": {
      "dimensions": ["Customer Country"],
      "measures": ["Revenue"]
    },
    "where": [
      {
        "field": "Customer Segment",
        "op": "in",
        "value": ["SMB", "MidMarket"]
      }
    ],
    "order_by": [
      { "field": "Revenue", "direction": "desc" }
    ],
    "limit": 1000
  },
  "dialect": "postgres"
}
```

**Response (200):**

```json
{
  "sql": "SELECT ...",
  "dialect": "postgres",
  "sql_valid": true,
  "explain": {
    "planner": "Star Schema",
    "planner_reason": "All measures come from a single fact table",
    "base_object": "Orders",
    "base_object_reason": "Orders has the most joins and contains all requested measures",
    "joins": [
      {
        "from_object": "Orders",
        "to_object": "Customers",
        "join_columns": ["CUSTOMER_ID"],
        "reason": "Required for dimension 'Customer Country'"
      }
    ],
    "where_filter_count": 1,
    "having_filter_count": 0,
    "has_totals": false,
    "cfl_legs": 0
  },
  "warnings": []
}
```

**Error responses:**

| Status | Cause |
|--------|-------|
| 400 | Unsupported dialect |
| 404 | Model or session not found |
| 422 | Resolution error |

### `POST /v1/sessions/{session_id}/query/execute`

Compile **and execute** a semantic query against the configured database. Requires `FLIGHT_ENABLED=true` with `DB_VENDOR` and vendor credentials configured.

If the query has no explicit `limit`, a default of 10,000 rows is enforced.

**Request:**

```json
{
  "model_id": "abcd1234",
  "query": {
    "select": {
      "dimensions": ["Customer Country"],
      "measures": ["Revenue"]
    },
    "limit": 100
  },
  "dialect": "postgres"
}
```

**Response (200):**

```json
{
  "sql": "SELECT ...",
  "dialect": "postgres",
  "columns": [
    {"name": "Customer Country", "type": "string"},
    {"name": "Revenue", "type": "number"}
  ],
  "rows": [
    ["US", 15230.50],
    ["UK", 9870.00]
  ],
  "row_count": 2,
  "execution_time_ms": 42.5,
  "resolved": {
    "fact_tables": ["Orders"],
    "dimensions": ["Customer Country"],
    "measures": ["Revenue"]
  },
  "sql_valid": true,
  "warnings": [],
  "explain": { "..." : "..." }
}
```

**Error responses:**

| Status | Cause |
|--------|-------|
| 400 | Unsupported dialect |
| 404 | Model or session not found |
| 422 | Resolution error |
| 502 | Database execution failed |
| 503 | Query execution not available (`FLIGHT_ENABLED` not set) |

**Top-level shortcut:** `POST /v1/query/execute` — auto-resolves session/model, auto-detects dialect from `DB_VENDOR`.

---

## OSI ↔ OBML Conversion

Stateless endpoints for converting between [OSI (Open Semantic Interchange)](https://github.com/open-semantic-interchange/OSI) and OBML formats. No session required.

### `POST /v1/convert/osi-to-obml`

Convert an OSI YAML model to OBML format.

**Request:**

```json
{
  "input_yaml": "version: \"0.1.1\"\nsemantic_model:\n  - name: my_model\n    ..."
}
```

**Response (200):**

```json
{
  "output_yaml": "version: 1.0\ndataObjects:\n  ...",
  "warnings": [
    "Relationship 'sales_to_date': no type specified, defaulting to many-to-one."
  ],
  "validation": {
    "schema_valid": true,
    "semantic_valid": true,
    "schema_errors": [],
    "semantic_errors": [],
    "semantic_warnings": []
  }
}
```

**Error (400):** Invalid YAML input.

**Error (422):** Conversion failed (e.g. unsupported OSI structure).

### `POST /v1/convert/obml-to-osi`

Convert an OBML YAML model to OSI format.

**Request:**

```json
{
  "input_yaml": "version: 1.0\ndataObjects:\n  ...",
  "model_name": "my_model",
  "model_description": "Sales analytics model",
  "ai_instructions": ""
}
```

The `model_name`, `model_description`, and `ai_instructions` fields are optional (defaults: `"semantic_model"`, `""`, `""`).

**Response (200):** Same structure as `POST /v1/convert/osi-to-obml`.

**Error (400):** Invalid YAML input.

**Error (422):** Conversion failed.

---

## Model Discovery

These endpoints provide structured access to model metadata. All fields include an optional `owner` property when set in the OBML model.

### `GET /v1/sessions/{session_id}/models/{model_id}/schema`

Full model structure as JSON, including all data objects, dimensions, measures, and metrics.

**Response (200):**

```json
{
  "model_id": "abcd1234",
  "version": 1.0,
  "owner": "team-data",
  "data_objects": [
    {
      "name": "Orders",
      "code": "ORDERS",
      "database": "WAREHOUSE",
      "schema": "PUBLIC",
      "columns": [
        { "name": "Price", "code": "PRICE", "abstract_type": "float" }
      ],
      "join_targets": ["Customers"],
      "owner": "team-sales"
    }
  ],
  "dimensions": [
    { "name": "Country", "data_object": "Customers", "column": "Country", "result_type": "string" }
  ],
  "measures": [
    { "name": "Revenue", "aggregation": "sum", "result_type": "float", "columns": [...] }
  ],
  "metrics": [
    { "name": "Revenue per Order", "expression": "...", "component_measures": ["Revenue", "Order Count"] }
  ]
}
```

### `GET /v1/sessions/{session_id}/models/{model_id}/dimensions`

List all dimensions.

**Response (200):** Array of dimension objects.

### `GET /v1/sessions/{session_id}/models/{model_id}/dimensions/{name}`

Get a single dimension by name.

**Response (200):**

```json
{
  "name": "Country",
  "data_object": "Customers",
  "column": "Country",
  "result_type": "string",
  "time_grain": null,
  "owner": null
}
```

**Error (404):** Dimension not found.

### `GET /v1/sessions/{session_id}/models/{model_id}/measures`

List all measures.

**Response (200):** Array of measure objects.

### `GET /v1/sessions/{session_id}/models/{model_id}/measures/{name}`

Get a single measure by name.

**Response (200):**

```json
{
  "name": "Revenue",
  "aggregation": "sum",
  "result_type": "float",
  "columns": [
    { "data_object": "Orders", "column": "Price" }
  ],
  "expression": "{[Orders].[Price]} * {[Orders].[Quantity]}",
  "total": false,
  "owner": null
}
```

**Error (404):** Measure not found.

### `GET /v1/sessions/{session_id}/models/{model_id}/metrics`

List all metrics.

**Response (200):** Array of metric objects.

### `GET /v1/sessions/{session_id}/models/{model_id}/metrics/{name}`

Get a single metric by name. Returns the expression formula and its component measures.

**Error (404):** Metric not found.

### `GET /v1/sessions/{session_id}/models/{model_id}/explain/{name}`

Explain the lineage of a dimension, measure, or metric — traces back through the dependency chain to the underlying data objects and columns.

**Response (200):**

```json
{
  "name": "Revenue",
  "type": "measure",
  "lineage": [
    { "type": "data_object", "name": "Orders" },
    { "type": "column", "name": "Price", "detail": "referenced in expression" },
    { "type": "column", "name": "Quantity", "detail": "referenced in expression" }
  ]
}
```

**Error (404):** Name not found in model.

### `POST /v1/sessions/{session_id}/models/{model_id}/find`

Search across model artefacts by name or synonym.

**Request:**

```json
{
  "query": "Revenue",
  "types": ["measure", "metric"]
}
```

The `types` filter is optional. Valid types: `dimension`, `measure`, `metric`, `data_object`.

**Response (200):**

```json
{
  "results": [
    { "name": "Revenue", "type": "measure", "match": "name" },
    { "name": "Revenue per Order", "type": "metric", "match": "name" }
  ]
}
```

### `GET /v1/sessions/{session_id}/models/{model_id}/join-graph`

Return the join graph as nodes and edges.

**Response (200):**

```json
{
  "nodes": ["Orders", "Customers", "Products"],
  "edges": [
    {
      "from_object": "Orders",
      "to_object": "Customers",
      "cardinality": "many-to-one",
      "secondary": false
    }
  ]
}
```

---

## Top-level Shortcuts

These endpoints auto-resolve the session and model when only one exists. They mirror the session-scoped model discovery endpoints without requiring session/model IDs.

Returns **404** if no sessions exist, **409 Conflict** if multiple sessions or models exist.

| Shortcut | Equivalent |
|----------|------------|
| `GET /v1/schema` | `GET /v1/sessions/{id}/models/{mid}/schema` |
| `GET /v1/dimensions` | `GET /v1/sessions/{id}/models/{mid}/dimensions` |
| `GET /v1/dimensions/{name}` | `GET /v1/sessions/{id}/models/{mid}/dimensions/{name}` |
| `GET /v1/measures` | `GET /v1/sessions/{id}/models/{mid}/measures` |
| `GET /v1/measures/{name}` | `GET /v1/sessions/{id}/models/{mid}/measures/{name}` |
| `GET /v1/metrics` | `GET /v1/sessions/{id}/models/{mid}/metrics` |
| `GET /v1/metrics/{name}` | `GET /v1/sessions/{id}/models/{mid}/metrics/{name}` |
| `GET /v1/explain/{name}` | `GET /v1/sessions/{id}/models/{mid}/explain/{name}` |
| `POST /v1/find` | `POST /v1/sessions/{id}/models/{mid}/find` |
| `GET /v1/join-graph` | `GET /v1/sessions/{id}/models/{mid}/join-graph` |
| `POST /v1/query/sql` | `POST /v1/sessions/{id}/query/sql` (auto-resolves model_id) |

---

## Settings

### `GET /v1/settings`

Return public configuration for API clients (UI, MCP, etc.).

**Response (200):**

```json
{
  "single_model_mode": false,
  "model_yaml": null,
  "session_ttl_seconds": 1800
}
```

When `MODEL_FILE` is configured:

```json
{
  "single_model_mode": true,
  "model_yaml": "version: 1.0\ndataObjects:\n  ...",
  "session_ttl_seconds": 1800
}
```

| Field | Type | Description |
|-------|------|-------------|
| `single_model_mode` | bool | Whether model upload/removal is disabled |
| `model_yaml` | string \| null | Pre-loaded OBML YAML (only when single-model mode is active) |
| `session_ttl_seconds` | int | Session inactivity timeout |

---

## Dialects

### `GET /v1/dialects`

List all available SQL dialects and their capability flags.

**Response (200):**

```json
{
  "dialects": [
    {
      "name": "bigquery",
      "capabilities": {
        "supports_cte": true,
        "supports_qualify": true,
        "supports_arrays": true,
        "supports_window_filters": true,
        "supports_ilike": false,
        "supports_time_travel": false,
        "supports_semi_structured": true
      }
    },
    { "name": "clickhouse", "capabilities": { "..." : true } },
    { "name": "databricks", "capabilities": { "..." : true } },
    { "name": "dremio", "capabilities": { "..." : true } },
    {
      "name": "duckdb",
      "capabilities": {
        "supports_cte": true,
        "supports_qualify": true,
        "supports_arrays": true,
        "supports_window_filters": true,
        "supports_ilike": true,
        "supports_time_travel": false,
        "supports_semi_structured": false
      }
    },
    { "name": "postgres", "capabilities": { "..." : true } },
    { "name": "snowflake", "capabilities": { "..." : true } }
  ]
}
```
