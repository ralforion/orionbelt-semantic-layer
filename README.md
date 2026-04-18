<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer/main/docs/assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

<h1 align="center">OrionBelt Semantic Layer</h1>

<p align="center"><strong>Compile and execute YAML semantic models as analytical SQL across multiple database dialects</strong></p>

[![GitHub stars](https://img.shields.io/github/stars/ralfbecher/orionbelt-semantic-layer?style=social)](https://github.com/ralfbecher/orionbelt-semantic-layer)
[![Version 1.6.1](https://img.shields.io/badge/version-1.6.1-purple.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer/releases)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: BSL 1.1](https://img.shields.io/badge/License-BSL_1.1-orange.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer/blob/main/LICENSE)
[![Docker Hub](https://img.shields.io/docker/pulls/ralforion/orionbelt-api?logo=docker&label=Docker%20Hub)](https://hub.docker.com/repositories/ralforion)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-E92063.svg?logo=pydantic&logoColor=white)](https://docs.pydantic.dev)
[![Gradio](https://img.shields.io/badge/Gradio-5.0+-F97316.svg?logo=gradio&logoColor=white)](https://www.gradio.app)

[![BigQuery](https://img.shields.io/badge/BigQuery-669DF6.svg?logo=googlebigquery&logoColor=white)](https://cloud.google.com/bigquery)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Snowflake](https://img.shields.io/badge/Snowflake-29B5E8.svg?logo=snowflake&logoColor=white)](https://www.snowflake.com)
[![ClickHouse](https://img.shields.io/badge/ClickHouse-FFCC01.svg?logo=clickhouse&logoColor=black)](https://clickhouse.com)
[![Dremio](https://img.shields.io/badge/Dremio-31B48D.svg)](https://www.dremio.com)
[![Databricks](https://img.shields.io/badge/Databricks-FF3621.svg?logo=databricks&logoColor=white)](https://www.databricks.com)
[![DuckDB](https://img.shields.io/badge/DuckDB-FFF000.svg?logo=duckdb&logoColor=black)](https://duckdb.org)
[![MySQL](https://img.shields.io/badge/MySQL-4479A1.svg?logo=mysql&logoColor=white)](https://www.mysql.com)

OrionBelt Semantic Layer is an **API-first** semantic engine and query planner for AI agents that compiles and executes declarative YAML model definitions as optimized SQL for BigQuery, ClickHouse, Databricks, Dremio, DuckDB/MotherDuck, MySQL, Postgres, and Snowflake. Query using business concepts — dimensions, measures, and metrics — instead of raw SQL.

**Analytics as Code** — Define your analytical semantics in version-controlled YAML, compile to dialect-specific SQL, and execute against live databases, all through a single API. No BI tool in the middle: the full loop from declarative model to query results is programmable, reviewable, and reproducible.

## Features

- **8 SQL Dialects** — BigQuery, ClickHouse, Databricks, Dremio, DuckDB/MotherDuck, MySQL, Postgres, Snowflake
- **OBML Model Format** — YAML-based semantic models with data objects, dimensions, measures, metrics, and joins
- **AST-Based SQL** — Custom SQL AST ensures correct, injection-safe SQL generation
- **Cross-Schema Queries** — Model data objects across multiple databases and schemas in a single model
- **Static Model Filters** — Mandatory WHERE conditions baked into the model, applied to every query with auto-join extension
- **Star Schema & CFL** — Automatic join resolution with Composite Fact Layer for multi-fact queries
- **REST API** — FastAPI endpoints for model management, validation, compilation, and execution
- **Gradio UI** — Interactive web interface for model editing, query testing, and ER diagrams
- **MCP Server** — Available as a [separate thin client](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp) for Claude, Copilot, Cursor, Windsurf
- **AI Integrations** — LangChain, OpenAI Agents SDK, CrewAI, Google ADK, Vercel AI SDK, n8n, ChatGPT
- **DB-API 2.0 + Flight SQL** — PEP 249 drivers and Arrow Flight SQL server for DBeaver, Tableau, Power BI
- **OBSL Graph & SPARQL** — RDF graph export and read-only SPARQL querying for every loaded model
- **OSI Interoperability** — Bidirectional conversion between OBML and Open Semantic Interchange format

## Quick Start

### Docker (fastest)

```bash
docker run -p 8080:8080 ralforion/orionbelt-api
```

API at `http://localhost:8080` — Swagger docs at [`/docs`](http://localhost:8080/docs)

### From Source

```bash
git clone https://github.com/ralfbecher/orionbelt-semantic-layer.git
cd orionbelt-semantic-layer
uv sync
uv run orionbelt-api
```

API at `http://localhost:8000` — Swagger docs at [`/docs`](http://localhost:8000/docs)

### Live Demo

> **[http://35.187.174.102/ui](http://35.187.174.102/ui/?__theme=dark)** — Gradio UI with pre-loaded example model

API: `http://35.187.174.102` — [Swagger UI](http://35.187.174.102/docs) | [ReDoc](http://35.187.174.102/redoc)

## Example

### Define a Semantic Model (OBML)

```yaml
version: 1.0
dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID: { code: CUSTOMER_ID, abstractType: string }
      Country:     { code: COUNTRY, abstractType: string }

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Order Customer ID: { code: CUSTOMER_ID, abstractType: string }
      Price:             { code: PRICE, abstractType: float }
      Quantity:          { code: QUANTITY, abstractType: int }
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]

dimensions:
  Country:
    dataObject: Customers
    column: Country
    resultType: string

measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: "{[Orders].[Price]} * {[Orders].[Quantity]}"
```

### Compile via REST API

```bash
# Create a session
curl -s -X POST http://localhost:8000/v1/sessions | jq .session_id
# -> "a1b2c3d4"

# Load the model
curl -s -X POST http://localhost:8000/v1/sessions/a1b2c3d4/models \
  -H "Content-Type: application/json" \
  -d '{"model_yaml": "..."}' | jq .model_id
# -> "abcd1234"

# Compile a query
curl -s -X POST http://localhost:8000/v1/sessions/a1b2c3d4/query/sql \
  -H "Content-Type: application/json" \
  -d '{"model_id":"abcd1234","query":{"select":{"dimensions":["Country"],"measures":["Revenue"]}},"dialect":"postgres"}' \
  | jq -r .sql
```

**Generated SQL (Postgres):**

```sql
SELECT
  "Customers"."COUNTRY" AS "Country",
  SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
  ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
GROUP BY "Customers"."COUNTRY"
```

Change `dialect` to `bigquery`, `clickhouse`, `databricks`, `dremio`, `duckdb`, `mysql`, or `snowflake` for dialect-specific SQL.

## Gradio UI

<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer/main/docs/assets/ui-sqlcompiler-dark.png" alt="SQL Compiler in Gradio UI" width="900">
</p>

Install the `ui` extra and the UI is mounted at `/ui` on the API server:

```bash
uv sync --extra ui && uv run orionbelt-api
# -> UI at http://localhost:8000/ui
```

## Documentation

| Topic | Link |
|-------|------|
| Full docs site | [ralforion.com/orionbelt-semantic-layer](https://ralforion.com/orionbelt-semantic-layer/) |
| Installation | [getting-started/installation](https://ralforion.com/orionbelt-semantic-layer/getting-started/installation/) |
| Quick Start | [getting-started/quickstart](https://ralforion.com/orionbelt-semantic-layer/getting-started/quickstart/) |
| Docker & Deployment | [getting-started/docker](https://ralforion.com/orionbelt-semantic-layer/getting-started/docker/) |
| Development | [getting-started/development](https://ralforion.com/orionbelt-semantic-layer/getting-started/development/) |
| OBML Model Format | [guide/model-format](https://ralforion.com/orionbelt-semantic-layer/guide/model-format/) |
| Query Language | [guide/query-language](https://ralforion.com/orionbelt-semantic-layer/guide/query-language/) |
| SQL Dialects | [guide/dialects](https://ralforion.com/orionbelt-semantic-layer/guide/dialects/) |
| Period-over-Period Metrics | [guide/period-over-period](https://ralforion.com/orionbelt-semantic-layer/guide/period-over-period/) |
| Compilation Pipeline | [guide/compilation](https://ralforion.com/orionbelt-semantic-layer/guide/compilation/) |
| OBSL Graph & SPARQL | [guide/obsl](https://ralforion.com/orionbelt-semantic-layer/guide/obsl/) |
| Gradio UI | [guide/ui](https://ralforion.com/orionbelt-semantic-layer/guide/ui/) |
| AI Integrations | [guide/integrations](https://ralforion.com/orionbelt-semantic-layer/guide/integrations/) |
| OSI Interoperability | [guide/osi](https://ralforion.com/orionbelt-semantic-layer/guide/osi/) |
| REST API Endpoints | [api/endpoints](https://ralforion.com/orionbelt-semantic-layer/api/endpoints/) |
| DB-API Drivers & Flight SQL | [drivers](https://ralforion.com/orionbelt-semantic-layer/drivers/) |
| Architecture | [reference/architecture](https://ralforion.com/orionbelt-semantic-layer/reference/architecture/) |
| Configuration | [reference/configuration](https://ralforion.com/orionbelt-semantic-layer/reference/configuration/) |
| Sales Model Walkthrough | [examples/sales-model](https://ralforion.com/orionbelt-semantic-layer/examples/sales-model/) |
| Multi-Dialect Output | [examples/multi-dialect](https://ralforion.com/orionbelt-semantic-layer/examples/multi-dialect/) |
| Multi-Fact: Sales & Returns | [examples/multi-fact](https://ralforion.com/orionbelt-semantic-layer/examples/multi-fact/) |
| TPC-DS Benchmark | [examples/tpcds](https://ralforion.com/orionbelt-semantic-layer/examples/tpcds/) |
| Quickstart Notebook | [examples/quickstart.ipynb](examples/quickstart.ipynb) |

## Companion Project

### [OrionBelt Analytics](https://github.com/ralfbecher/orionbelt-analytics)

An ontology-based MCP server that analyzes relational database schemas and generates RDF/OWL ontologies. Together with OrionBelt Semantic Layer, it enables AI assistants to navigate your data landscape through ontologies and compile safe, dialect-aware analytical SQL.

<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer/main/docs/assets/architecture.png" alt="OrionBelt Analytics Architecture" width="800">
</p>

## License

Copyright 2025 [RALFORION d.o.o.](https://ralforion.com)

Licensed under the [Business Source License 1.1](LICENSE). The Licensed Work will convert to Apache License 2.0 on 2030-03-16.

By contributing to this project, you agree to the [Contributor License Agreement](CLA.md).

---

<p align="center">
  <a href="https://ralforion.com">
    <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer/main/docs/assets/RALFORION_doo_Logo.png" alt="RALFORION d.o.o." width="200">
  </a>
</p>
