# Quick Start

This walkthrough takes you from a YAML semantic model to compiled and executed SQL in under 5 minutes.

!!! tip "Try it in Google Colab"
    [![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/ralfbecher/orionbelt-semantic-layer/blob/main/examples/quickstart_colab.ipynb) — Interactive notebook with TPC-H data: explore the model, compile queries, execute against DuckDB, and see results. Requires Python 3.12 runtime.

## Step 1: Define a Semantic Model

Create a file called `model.yaml`:

```yaml
# yaml-language-server: $schema=schema/obml-schema.json
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
        primaryKey: true
      Country:
        code: COUNTRY
        abstractType: string
      Segment:
        code: SEGMENT
        abstractType: string

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
        primaryKey: true
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Price:
        code: PRICE
        abstractType: float
        numClass: non-additive
      Quantity:
        code: QUANTITY
        abstractType: int
        numClass: additive
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Customer ID
        columnsTo:
          - Customer ID

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string
  Customer Segment:
    dataObject: Customers
    column: Segment
    resultType: string

measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'

  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
```

## Step 2: Load and Validate

```python
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

# Load YAML with source position tracking
loader = TrackedLoader()
raw, source_map = loader.load("model.yaml")

# Resolve references into typed Pydantic models
resolver = ReferenceResolver()
model, result = resolver.resolve(raw, source_map)

if not result.valid:
    for error in result.errors:
        print(f"  {error.code}: {error.message}")
else:
    print("Model is valid!")

# Run semantic validation (cycle check, reference resolution, etc.)
validator = SemanticValidator()
errors = validator.validate(model)
for error in errors:
    print(f"  {error.code}: {error.message}")
```

## Step 3: Compile a Query

```python
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import (
    QueryObject,
    QuerySelect,
    QueryFilter,
    FilterOperator,
    QueryOrderBy,
    SortDirection,
)

# Define a query: Revenue by country for SMB/MidMarket customers
query = QueryObject(
    select=QuerySelect(
        dimensions=["Customer Country"],
        measures=["Revenue", "Order Count"],
    ),
    where=[
        QueryFilter(
            field="Customer Segment",
            op=FilterOperator.IN,
            value=["SMB", "MidMarket"],
        ),
    ],
    order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
    limit=1000,
)

# Compile to Postgres SQL
pipeline = CompilationPipeline()
result = pipeline.compile(query, model, "postgres")
print(result.sql)
```

**Output:**

```sql
SELECT
  "Customers"."COUNTRY" AS "Customer Country",
  SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue",
  COUNT("Orders"."ORDER_ID") AS "Order Count"
FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
  ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
WHERE ("Customers"."SEGMENT" IN ('SMB', 'MidMarket'))
GROUP BY "Customers"."COUNTRY"
ORDER BY "Revenue" DESC
LIMIT 1000
```

## Step 4: Try a Different Dialect

Simply change the dialect parameter:

```python
# BigQuery
result = pipeline.compile(query, model, "bigquery")

# Snowflake
result = pipeline.compile(query, model, "snowflake")

# ClickHouse
result = pipeline.compile(query, model, "clickhouse")

# DuckDB / MotherDuck
result = pipeline.compile(query, model, "duckdb")

# Databricks
result = pipeline.compile(query, model, "databricks")
```

Each dialect applies its own identifier quoting, function names, and SQL syntax. See [SQL Dialects](../guide/dialects.md) for details.

## Step 5: Use the REST API with Sessions

Start the server:

```bash
uv run orionbelt-api
```

Create a session and load a model:

```bash
# Create a session
SESSION_ID=$(curl -s -X POST http://127.0.0.1:8000/v1/sessions | jq -r .session_id)

# Load a model into the session
MODEL_ID=$(curl -s -X POST "http://127.0.0.1:8000/v1/sessions/$SESSION_ID/models" \
  -H "Content-Type: application/json" \
  -d "{\"model_yaml\": \"$(cat model.yaml)\"}" | jq -r .model_id)

# Compile a query
curl -s -X POST "http://127.0.0.1:8000/v1/sessions/$SESSION_ID/query/sql" \
  -H "Content-Type: application/json" \
  -d "{
    \"model_id\": \"$MODEL_ID\",
    \"query\": {
      \"select\": {
        \"dimensions\": [\"Customer Country\"],
        \"measures\": [\"Revenue\"]
      }
    },
    \"dialect\": \"postgres\"
  }" | jq .sql

# Clean up
curl -s -X DELETE "http://127.0.0.1:8000/v1/sessions/$SESSION_ID"
```

## Step 6: Admin-Curated Mode

To serve fixed models without allowing uploads, start with `MODEL_FILES` (one or more paths):

```bash
MODEL_FILES=./model.yaml uv run orionbelt-api
```

Each model loads into its own *named protected session*. The session id is the OBML `name:` field (falling back to the filename stem), so you can address the model directly:

```bash
# The model name is the session id — use it in REST paths
MODEL_NAME="sales"          # whatever your OBML name: or filename stem resolves to

# List the protected model
curl -s "http://127.0.0.1:8000/v1/sessions/$MODEL_NAME/models"
MODEL_ID=$(curl -s "http://127.0.0.1:8000/v1/sessions/$MODEL_NAME/models" | jq -r '.[0].model_id')

# Query directly against the named session — no upload needed
curl -s -X POST "http://127.0.0.1:8000/v1/sessions/$MODEL_NAME/query/sql" \
  -H "Content-Type: application/json" \
  -d "{
    \"model_id\": \"$MODEL_ID\",
    \"query\": {
      \"select\": {
        \"dimensions\": [\"Customer Country\"],
        \"measures\": [\"Revenue\"]
      }
    },
    \"dialect\": \"postgres\"
  }" | jq .sql
```

`GET /v1/models` lists every preloaded model + its addressing name. Model upload (`POST /v1/sessions/{id}/models`) and removal are blocked (403) while admin-curated mode is on. BI tools through Flight SQL or pgwire select the same model via the `database` header / URL parameter.

## Next Steps

- [OBML Model Format](../guide/model-format.md) — Complete OrionBelt ML specification
- [Query Language](../guide/query-language.md) — Filters, operators, time grains
- [SQL Dialects](../guide/dialects.md) — Dialect capabilities and differences
- [API Endpoints](../api/endpoints.md) — Full REST API documentation
