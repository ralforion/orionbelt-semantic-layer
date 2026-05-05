# API Endpoints

Complete reference for all OrionBelt REST API endpoints.

## Health Check

### `GET /health`

Returns the service status and version.

**Response:**

```json
{
  "status": "ok",
  "version": "2.2.0"
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
  "model_yaml": "version: 1.0\ndataObjects:\n  Orders:\n    code: ORDERS\n    ...",
  "dedup": true
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `model_yaml` | string | — | OBML YAML (or use `model_json`). |
| `model_json` | object/string | — | OBML as JSON (or use `model_yaml`). |
| `extends` | array | — | Inline YAML fragments to merge. |
| `inherits` | string | — | Parent model_id to inherit from. |
| `dedup` | bool | `true` | When true, identical OBML content already loaded in this session reuses the existing `model_id`. Set `false` to force a fresh parse. |

**Response (201):**

```json
{
  "model_id": "abcd1234",
  "data_objects": 2,
  "dimensions": 3,
  "measures": 2,
  "metrics": 1,
  "warnings": [],
  "model_load": "fresh",
  "health": {
    "status": "ok",
    "data_objects": 2,
    "joins": 1,
    "orphan_data_objects": [],
    "fan_trap_risks": [],
    "unreachable_dimensions": [],
    "warnings_count": 0
  }
}
```

`model_load` is `"fresh"` when the OBML was parsed and loaded normally, `"reused"` when an identical model was already present in the session (no parsing/validation work was done; the existing `model_id` is returned). Dedup applies only to plain `model_yaml` loads — supplying `extends` or `inherits` always loads fresh.

#### `health` block

Structural health of the model's join graph, computed during load (no extra round trip required). Always present on `fresh` loads and on `reused` dedup hits. Fields:

| Field | Type | Description |
|---|---|---|
| `status` | string | `ok` when nothing surfaced, `warnings` when one or more risks were detected. |
| `data_objects` | int | Count of dataObjects in the model. |
| `joins` | int | Count of (non-secondary) joins detected. |
| `orphan_data_objects` | array of strings | DataObjects with no incoming or outgoing joins. May be intentional in single-table models. |
| `fan_trap_risks` | array of objects | Pairs of facts that share a dimension via the same FK columns. Each entry has `tables` (qualified physical names), `reason`, and `suggested_pattern` (typically `"composite_fact_layer"`). |
| `unreachable_dimensions` | array of strings | Dimensions whose dataObject is not reachable from any fact via directed joins. |
| `warnings_count` | int | Total warnings across orphans, fan-traps, and unreachable dims. |

#### Structured `warnings`

Every `warnings` list in this API uses the same shape so agents can branch on stable codes without parsing message text:

```json
{
  "code": "FAN_TRAP_RISK",
  "severity": "warning",
  "message": "Measure 'Revenue' (SUM): cross-join through 'Movie Directors' …",
  "path": "select.measures[0]",
  "hint": "Add the junction-table dimension to the GROUP BY, …",
  "context": { "measure": "Revenue", "junction": "Movie Directors" }
}
```

Initial warning code taxonomy: `GRAIN_OVERRIDE_INCOMPATIBLE`, `FILTER_CONTEXT_OVERRIDE_INCOMPATIBLE`, `POP_CONSTRAINT_VIOLATED`, `CUMULATIVE_CONSTRAINT_VIOLATED`, `FAN_TRAP_RISK`, `ORPHAN_DATA_OBJECT`, `SHARED_TABLE_CONTRACT_DISAGREEMENT`, `LARGE_RESULT_SET`, `CACHE_TTL_FLOOR_HIT`, `INCOMPATIBLE_COMBINATION`, `SQL_VALIDATION`, `MERGE_WARNING`. Codes are extended over time, never repurposed.

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

### `POST /v1/sessions/{session_id}/query/plan`

Return the planner's understanding of a query without compiling SQL or executing. Cheap by default (no warehouse round trip) — agents use it as a "would this work?" probe in their planning loop.

**Request:**

```json
{
  "model_id": "abcd1234",
  "query": {
    "select": {
      "dimensions": ["Customer Country"],
      "measures": ["Revenue"]
    }
  },
  "dialect": "postgres",
  "include_database_explain": false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `model_id` | string | — | Loaded model id. |
| `query` | object | — | QueryObject (same shape as `/query/sql`). |
| `dialect` | string | model/env default | SQL dialect. |
| `include_database_explain` | bool | `false` | When `true`, also runs `EXPLAIN <sql>` against the configured warehouse and includes the raw output. Costs one round trip; some warehouses bill compute for `EXPLAIN`. |

**Response (200, OBSL-only plan):**

```json
{
  "status": "ok",
  "planner": "Star Schema",
  "planner_reason": "All requested objects are reachable from a single base via directed joins",
  "physical_tables": ["WAREHOUSE.PUBLIC.ORDERS", "WAREHOUSE.PUBLIC.CUSTOMERS"],
  "join_path": [
    {
      "from_object": "Orders",
      "to_object": "Customers",
      "cardinality": "many-to-one",
      "fk": "CUSTOMER_ID = CUSTOMER_ID"
    }
  ],
  "filters_applied": 0,
  "warnings": [],
  "would_compile": true,
  "compiled_sql_length_estimate": 312,
  "database_explain": null
}
```

**Response (200, with `include_database_explain: true`):**

Adds a `database_explain` block. The `explain_output` is opaque text in the dialect's native EXPLAIN format — OBSL does not normalize across dialects.

```json
{
  "...": "...",
  "database_explain": {
    "dialect": "postgres",
    "compiled_sql": "SELECT ...",
    "explain_output": "Hash Join (cost=128.50..1247.30 rows=1042 width=48) ...",
    "explain_format": "text"
  }
}
```

**Failure modes:**

- Resolution / fanout / unsupported-aggregation errors → `status: "error"`, `would_compile: false`, structured `warnings` with the failure cause.
- `include_database_explain: true` but the warehouse rejects `EXPLAIN` → OBSL plan still returned with a `DATABASE_EXPLAIN_FAILED` warning describing why; `database_explain` is `null`.

The plan endpoint never executes the actual query, even with `include_database_explain: true`.

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
    {"name": "Revenue", "type": "decimal(18, 2)", "format": "#,##0.00"}
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

**Query parameters** (apply to both the session and shortcut form):

| Param | Type | Default | Description |
|---|---|---|---|
| `format` | `json` \| `tsv` | `json` | When `tsv`, returns `text/tab-separated-values`; cells with tab/newline/CR/double-quote are RFC 4180-quoted. Implies `format_values=true`. |
| `format_values` | bool | `false` | When `true`, numeric cells in the JSON response are rendered as locale-aware display strings using each column's `format` pattern (matches the Gradio UI). |
| `locale` | string | `DEFAULT_LOCALE` env | BCP-47 tag (e.g. `de`, `en-US`). Drives thousand/decimal separators. Falls back to the `DEFAULT_LOCALE` env when omitted. |
| `timezone` | string | model `default_timezone` | IANA TZ name (e.g. `Europe/Berlin`). Overrides the model's default for naive timestamp coercion. |

**Example (TSV with German locale):**

```bash
curl -X POST 'http://localhost:8080/v1/query/execute?format=tsv&locale=de' \
     -H 'Content-Type: application/json' \
     -d '{ "select": { "dimensions": ["Customer Country"], "measures": ["Revenue"] } }'
```

```
Customer Country	Revenue
US	15.230,50
UK	9.870,00
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

## One-shot Batch

### `POST /v1/oneshot/batch`

Load (or reference) a model and run multiple independent queries against it in a single round trip. Designed for agent workflows: one model, N sub-questions, parallel execution under a server-capped semaphore. See `design/PLAN_oneshot_batch.md` for the full design.

**Request:**

```json
{
  "session_id": null,
  "model_yaml": "version: 1.0\ndataObjects: ...",
  "model_id": null,
  "queries": [
    {
      "id": "revenue_by_country",
      "query": {
        "select": {"dimensions": ["Customer Country"], "measures": ["Total Revenue"]},
        "limit": 100
      }
    },
    {
      "id": "revenue_by_product",
      "query": {"select": {"dimensions": ["Product"], "measures": ["Total Revenue"]}},
      "execute": false
    }
  ],
  "dialect": "postgres",
  "execute": true,
  "max_parallelism": 4,
  "fail_fast": false,
  "persist_model": false,
  "dedup": true
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `session_id` | string | auto-create | Existing session, otherwise OBSL creates one. |
| `model_yaml` | string | — | OBML YAML. Mutually exclusive with `model_id`. |
| `model_id` | string | — | ID of an already-loaded model in the session. Mutually exclusive with `model_yaml`. |
| `queries` | array | required | List of queries (1..max_queries). |
| `queries[].id` | string | auto (`q0`,`q1`,...) | Optional caller ID. Must be unique within the batch when supplied. Omit to let the server assign positional IDs. |
| `queries[].execute` | bool | inherits batch | Per-query override for compile-only vs. execute. |
| `queries[].dialect` | string | inherits batch | Per-query dialect override. |
| `dialect` | string | model/env | Default dialect for the batch. |
| `execute` | bool | `false` | Default execute flag for the batch. |
| `max_parallelism` | int | server cap | Concurrency cap (silently lowered to the server max). |
| `fail_fast` | bool | `false` | Cancel remaining queries on first failure. |
| `persist_model` | bool | `false` | Keep the model loaded after the batch (only for `model_yaml` loads). |
| `dedup` | bool | `true` | Reuse an existing identical model loaded in this session. |

**Response (200):**

```json
{
  "session_id": "a1b2c3d4...",
  "model_id": "abcd1234",
  "model_persisted": false,
  "model_load": "fresh",
  "results": [
    {
      "id": "revenue_by_country",
      "status": "ok",
      "sql": "SELECT ...",
      "dialect": "postgres",
      "sql_valid": true,
      "executed": true,
      "columns": [{"name": "Customer Country", "type": "string"}],
      "rows": [["US", 15230.5]],
      "row_count": 42,
      "execution_time_ms": 38.2,
      "warnings": []
    },
    {
      "id": "revenue_by_product",
      "status": "ok",
      "sql": "SELECT ...",
      "dialect": "postgres",
      "sql_valid": true,
      "executed": false,
      "warnings": []
    }
  ],
  "batch_warnings": []
}
```

`model_load` is `"fresh"` (parsed and loaded), `"reused"` (matched dedup index), or `"referenced"` (caller supplied `model_id`).

Per-query `status` is `"ok"`, `"error"` (with an `error` envelope: `{code, message, path, hint}`), or `"cancelled"` (only when `fail_fast: true` triggers and remaining queries are short-circuited).

**Error (422):** Validation failure (duplicate query IDs, both/neither of `model_yaml`/`model_id`, batch over `ONESHOT_BATCH_MAX_QUERIES`, or model load failure).

**Error (404):** Session or `model_id` not found.

**Error (410):** Session expired.

**Limits** (configurable, see `GET /v1/settings.oneshot_batch`):

| Setting | Default |
|---|---|
| `ONESHOT_BATCH_MAX_QUERIES` | 50 |
| `ONESHOT_BATCH_MAX_PARALLELISM` | 8 |
| `ONESHOT_BATCH_DEFAULT_TIMEOUT_MS` | 30000 (per-query) |
| `ONESHOT_BATCH_BATCH_TIMEOUT_MS` | 120000 (whole batch) |

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

Search across model artefacts by name or synonym. When the query produces zero exact and zero synonym matches, deterministic fuzzy fallback (Levenshtein + trigram-Jaccard) returns the closest near-miss candidates. Threshold is 0.5; up to 10 results are returned.

**Request:**

```json
{
  "query": "Revenue",
  "types": ["measure", "metric"]
}
```

The `types` filter is optional. Valid types: `dimension`, `measure`, `metric`, `data_object`.

**Response (200, exact / synonym hits):**

```json
{
  "query": "Revenue",
  "results": [
    { "name": "Revenue", "type": "measure", "match_field": "name", "score": 1.0 },
    { "name": "Revenue per Order", "type": "metric", "match_field": "name", "score": 1.0 }
  ],
  "exact_matches": [...],
  "synonym_matches": [],
  "fuzzy_matches": []
}
```

**Response (200, no exact/synonym hit — fuzzy fallback fires):**

```json
{
  "query": "Custmr Cuntry",
  "results": [],
  "exact_matches": [],
  "synonym_matches": [],
  "fuzzy_matches": [
    {
      "name": "Customer Country",
      "kind": "dimension",
      "score": 0.78,
      "reason": "trigram overlap"
    }
  ]
}
```

`fuzzy_matches` is empty when the query produced exact or synonym hits, and also when nothing scored above the 0.5 threshold (truly no match).

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

## Model Examples

Canonical example queries authored alongside the model in OBML's optional `examples:` block. Surfaced through these endpoints so agents can discover what kinds of questions a model is designed to answer in one round trip.

See `docs/guide/model-format.md` for the OBML `examples:` syntax.

### `GET /v1/sessions/{session_id}/models/{model_id}/examples`

List every example summary in the loaded model.

**Query parameters (optional):**

| Param | Description |
|---|---|
| `intent` | Filter by intent tag (case-insensitive). Resolution: exact tag match → substring match → fuzzy match against the tag corpus. |

**Response (200):**

```json
{
  "examples": [
    {
      "name": "revenue_by_country",
      "description": "Total completed-order revenue, broken down by customer country.",
      "intent_tags": ["revenue", "geography"]
    }
  ],
  "suggestion": null
}
```

**Response (200, `?intent=` did not match anything):**

```json
{
  "examples": [],
  "suggestion": "no examples for 'foo'; available tags: revenue, orders, geography"
}
```

### `GET /v1/sessions/{session_id}/models/{model_id}/examples/{example_name}`

Return a single example by name, with the full query payload and a best-effort compiled SQL preview.

**Response (200):**

```json
{
  "name": "revenue_by_country",
  "description": "Total completed-order revenue, broken down by customer country.",
  "intent_tags": ["revenue", "geography"],
  "query": {
    "select": {
      "dimensions": ["Customer Country"],
      "measures": ["Total Revenue"]
    }
  },
  "compiled_sql_preview": "SELECT ..."
}
```

`compiled_sql_preview` is `null` when the example fails to compile against the current model (e.g. the model has drifted since the example was authored).

**Error (404):** Example name not found in model.

---

## OBSL Graph & SPARQL

### `GET /v1/sessions/{session_id}/models/{model_id}/graph`

Return the OBSL-Core RDF graph as Turtle. The graph is generated at model load time.

**Response (200):** `text/turtle`

```turtle
@prefix obsl: <https://ralforion.com/ns/obsl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<https://ralforion.com/ns/model/abc123> a obsl:SemanticModel ;
    obsl:hasDataObject <.../data-object/orders> ;
    obsl:hasMeasure <.../measure/revenue> .
```

**Error (404):** Session or model not found.

### `POST /v1/sessions/{session_id}/models/{model_id}/sparql`

Execute a read-only SPARQL query against the model's OBSL graph.

**Request:**

```json
{
  "query": "PREFIX obsl: <https://ralforion.com/ns/obsl#> PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> SELECT ?label WHERE { ?m a obsl:Measure ; rdfs:label ?label . }"
}
```

Only `SELECT` and `ASK` queries are allowed. The `query` field has a maximum length of 100,000 characters.

**Response (200):**

```json
{
  "type": "select",
  "variables": ["label"],
  "results": [
    {"label": "Revenue"},
    {"label": "Order Count"}
  ],
  "boolean": null
}
```

For `ASK` queries:

```json
{
  "type": "ask",
  "variables": [],
  "results": [],
  "boolean": true
}
```

**Error (400):** Update query rejected or invalid SPARQL syntax.

**Error (404):** Session or model not found.

---

## Result cache

See `docs/guide/result-cache.md` for the full design and operational notes. Cache is **off by default** (`CACHE_BACKEND=noop`). Enable with `CACHE_BACKEND=file` and a writable `CACHE_DIR`.

### `GET /v1/cache/stats`

Always responds — when `CACHE_BACKEND=noop` the response shows `backend: "noop"` with zero counters.

**Response (200):**

```json
{
  "backend": "file",
  "entry_count": 1247,
  "total_size_bytes": 234567890,
  "max_size_bytes": 5368709120,
  "hit_count_total": 9821,
  "miss_count_total": 4203,
  "hit_rate": 0.700,
  "oldest_entry": "2026-04-15T12:30:00Z",
  "next_sweep_at": "2026-04-15T12:45:00Z",
  "tracked_physical_tables": 8,
  "heartbeat_invalidations_total": 142
}
```

### `POST /v1/cache/sweep`

Triggers a single TTL + capacity eviction pass on demand — equivalent to one tick of the periodic sweeper. Safe to call at any time. With `CACHE_BACKEND=noop` returns zero counts.

**Response (200):**

```json
{
  "backend": "file",
  "ttl_evicted": 17,
  "capacity_evicted": 0
}
```

### `POST /v1/cache/clear`

Drops every cache entry regardless of TTL or freshness contract. Useful for manual resets and debugging. Counters (`hit_count_total`, `miss_count_total`, `heartbeat_invalidations_total`) are preserved as historical telemetry. With `CACHE_BACKEND=noop` returns zero.

**Response (200):**

```json
{
  "backend": "file",
  "entries_cleared": 1247
}
```

### `POST /v1/heartbeat`

ETL pings this endpoint after refreshing a physical table. The cache invalidates every entry whose dependency set includes that table — across every dataObject and every session.

Authentication: `Authorization: Bearer <HEARTBEAT_AUTH_TOKEN>`. When the env var is unset, the route returns 404.

**Request:**

```json
{
  "database": "WAREHOUSE",
  "schema": "PUBLIC",
  "table": "ORDERS",
  "timestamp": "2026-04-29T14:32:15Z"
}
```

`timestamp` is optional; defaults to server `now()`. Future timestamps are clamped to `now()`.

**Response (200):**

```json
{
  "table_ref": "WAREHOUSE.PUBLIC.ORDERS",
  "recorded_at": "2026-04-29T14:32:15Z",
  "invalidated_cache_entries": 47,
  "affected_data_objects": ["Orders", "OrderReturns", "OrdersPivoted"]
}
```

`affected_data_objects` lists every OBML name tied to this physical table at the moment of the heartbeat — useful for verifying your model maps the way you expect.

| Status | Cause |
|--------|-------|
| 401 | Missing or invalid bearer token |
| 404 | Heartbeat endpoint disabled (no `HEARTBEAT_AUTH_TOKEN` configured) |
| 422 | Invalid timestamp format |

### Per-query response fields

Every `query/execute` JSON response gains a cache observability block:

| Field | Description |
|---|---|
| `cached` | Whether this result came from the cache. |
| `cached_at` | ISO 8601 timestamp the cached result was first computed (null when fresh). |
| `ttl_seconds` | Effective TTL applied to this entry. |
| `ttl_source` | `freshness_derived`, `caller_capped`, `default_unknown`, `no_cache:<reason>`. |
| `ttl_limiting_table` | Physical table whose contract drove the effective TTL. |
| `physical_tables` | Deduplicated `database.schema.code` strings the query touched. |

`physical_tables` is also surfaced on `query/sql` responses for clients that want to inspect plan reach without executing.

**`execution_time_ms` on cache hits:** when `cached: true`, this field reports the wall-clock time spent reading and decoding the cached entry — *not* the original database run time. The original DB timing is preserved on disk in the Parquet sidecar for forensic inspection but not surfaced on the wire. Combine with the `cached` flag to distinguish "fresh from warehouse" vs "served from cache" durations.

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
| `GET /v1/graph` | `GET /v1/sessions/{id}/models/{mid}/graph` |
| `POST /v1/sparql` | `POST /v1/sessions/{id}/models/{mid}/sparql` |
| `POST /v1/query/sql` | `POST /v1/sessions/{id}/query/sql` (auto-resolves model_id) |

---

## Settings

### `GET /v1/settings`

Return public configuration for API clients (UI, MCP, etc.).

**Query parameters (optional)** — both default to `null`:

| Param | Description |
|-------|-------------|
| `session_id` | Scope `model_settings`, `timezone`, and `dialect.model` to this session. If the session holds exactly one model, that model is used; otherwise the model-specific blocks are omitted (request `model_id` to disambiguate). |
| `model_id` | Pin to a specific model in `session_id`. Returns 400 without `session_id`, 404 if the session or model is unknown. |

**Resolution rules without query parameters:**

- single-model mode → uses the preloaded model
- multi-model mode → uses the unique model across all sessions if exactly one is loaded; otherwise the model-specific blocks are omitted

**Response (200) — multi-model mode:**

```json
{
  "version": "2.2.0",
  "api_version": "v1",
  "single_model_mode": false,
  "session_ttl_seconds": 1800,
  "session_max_age_seconds": 86400,
  "max_sessions": 500,
  "max_models_per_session": 10,
  "query_execute": false,
  "dialect": {
    "env": "duckdb",
    "effective": "duckdb"
  }
}
```

**Response (200) — single-model mode (`MODEL_FILE` is configured):**

```json
{
  "version": "2.2.0",
  "api_version": "v1",
  "single_model_mode": true,
  "model_yaml": "version: 1.0\nsettings:\n  defaultTimezone: Europe/Berlin\n  ...",
  "session_ttl_seconds": 1800,
  "session_max_age_seconds": 86400,
  "max_sessions": 500,
  "max_models_per_session": 10,
  "query_execute": true,
  "model_settings": {
    "defaultTimezone": "Europe/Berlin",
    "defaultDialect": "snowflake",
    "overrideDatabaseTimezone": false,
    "defaultNumericDataType": "decimal(38, 4)"
  },
  "timezone": {
    "model": "Europe/Berlin",
    "host": "Europe/Berlin",
    "database": null,
    "effective": "Europe/Berlin",
    "override_database_timezone": false,
    "now": "2026-04-29T15:30:00+02:00",
    "utc": "2026-04-29T13:30:00Z"
  },
  "dialect": {
    "model": "snowflake",
    "env": "duckdb",
    "effective": "snowflake"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `version` | string | OrionBelt Semantic Layer release version |
| `api_version` | string | REST API version prefix (`v1`) |
| `single_model_mode` | bool | Whether model upload/removal is disabled |
| `model_yaml` | string \| null | Pre-loaded OBML YAML (single-model mode only) |
| `session_ttl_seconds` | int | Session inactivity timeout |
| `session_max_age_seconds` | int | Absolute max session lifetime |
| `max_sessions` | int | Global concurrent session cap |
| `max_models_per_session` | int | Maximum models per session |
| `query_execute` | bool | Whether `POST /query/execute` is available |
| `flight` | object \| null | Arrow Flight SQL info (when Flight is enabled) |
| `model_settings` | object \| null | Loaded model's `settings:` block (single-model mode) |
| `timezone` | object \| null | Timezone resolution chain (single-model mode) |
| `dialect` | object | SQL dialect resolution chain (always present) |

**`model_settings`** mirrors the OBML `settings:` block in camelCase — `defaultTimezone`, `defaultDialect`, `overrideDatabaseTimezone`, `defaultNumericDataType`. Any key the model omits is also omitted from the response.

**`timezone`** is the chain `db_executor.resolve_timezone()` walks at execute time. Always present so clients can show the wall clock even without a loaded model:

- `override_database_timezone: true` → `model` wins, falling back to `host` then `UTC`.
- otherwise → cached `database` session timezone wins (when known), then `model`, then `host`, then `UTC`.

The endpoint never probes the database — `database` is `null` until a query has run for that dialect. `effective` is the timezone that will be applied right now. `now` is the current wall-clock time in the effective TZ (ISO 8601 with offset suffix); `utc` is the same instant in UTC.

**`dialect`** mirrors how the planner resolves the dialect when the request body omits `dialect`: `model.defaultDialect` → `DB_VENDOR` env → `postgres`. `effective` is what would be used for a dialect-less request.

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
