<p align="center">
  <img src="assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

# OrionBelt Semantic Layer

**Compile and execute YAML semantic models as analytical SQL across multiple database dialects.**

OrionBelt Semantic Layer is an **API-first** semantic engine and query planner for AI agents that compiles and executes declarative YAML model definitions as optimized SQL for BigQuery, ClickHouse, Databricks, Dremio, DuckDB/MotherDuck, MySQL, Postgres, and Snowflake. Query using business concepts — dimensions, measures, and metrics — instead of raw SQL.

## The OrionBelt trio

OBSL ships three named pillars. You author models in **OBML**, query them in **OBSQL**, and run it all on **OBSL**.

| Short form | Full name | What it is | Reference |
|---|---|---|---|
| **OBSL** | OrionBelt **Semantic Layer** | The system itself — compiler, planner, runtime, REST / Flight / Postgres-wire surfaces | this site |
| **OBML** | OrionBelt **Modeling Language** | Declarative YAML format for defining models | [OBML Model Format](guide/model-format.md) |
| **OBSQL** | OrionBelt **Semantic QL** | SQL surface BI tools and humans actually write — bare-label or `MEASURE()` syntax, aggregation-match validation, `WITH ROLLUP` / `WITH CUBE`, no escape hatch to raw SQL | [OBSQL reference](guide/semantic-ql.md) |

OBSQL flows over **Apache Arrow Flight SQL** (v2.4+) and **PostgreSQL wire** (v2.5+). Same language, two transports — pick whichever your BI tool prefers.

## Why OrionBelt?

- **Analytics as Code** — Define your analytical semantics in version-controlled YAML, compile to dialect-specific SQL, and execute against live databases, all through a single API. No BI tool in the middle: the full loop from declarative model to query results is programmable, reviewable, and reproducible
- **One model, many dialects** — Define your semantic model once in YAML, compile and execute SQL for any supported warehouse
- **Cross-schema & cross-database** — Model data objects across multiple schemas and databases; dimensions, measures, and metrics can span schema boundaries in a single query
- **Safe by construction** — AST-based SQL generation prevents injection and ensures syntactic correctness
- **Precise error reporting** — Validation errors include line and column numbers from your YAML source
- **Static model filters** — Bake mandatory WHERE conditions into the model (by status, region, date range, etc.) — applied to every query automatically, with auto-join extension and ISO 8601 date/timestamp support
- **Automatic join resolution** — Declare relationships between data objects; OrionBelt finds optimal join paths using graph algorithms
- **Multi-fact support** — Composite Fact Layer (CFL) planning handles queries spanning multiple fact tables with UNION ALL and CTE-based aggregation
- **Machine-readable semantics** — Every loaded model is exported as an OBSL-Core 0.1 RDF graph and queryable via a read-only SPARQL endpoint, so AI agents and knowledge-graph tools can reason over your model
- **Session management** — TTL-scoped sessions isolate model state per client, enabling iterative development workflows

## Key Features

| Feature             | Description                                                                                                     |
| ------------------- | --------------------------------------------------------------------------------------------------------------- |
| 8 SQL Dialects      | BigQuery, ClickHouse, Databricks, Dremio, DuckDB/MotherDuck, MySQL, Postgres, Snowflake                         |
| OrionBelt ML (OBML) | YAML-based data objects, dimensions, measures, metrics, joins                                                   |
| Cross-Schema Queries | Dimensions, measures, and metrics can span multiple databases and schemas in a single query                     |
| Static Model Filters | Mandatory WHERE conditions on any column, auto-join, ISO 8601 dates, deduplicated with query-time filters       |
| Star Schema & CFL   | Automatic fact selection and join path resolution                                                               |
| Session Management  | TTL-scoped per-client sessions for the REST API                                                                 |
| REST API            | FastAPI endpoints for session-based model management, validation, compilation, execution, and OSI conversion               |
| Gradio UI           | Interactive web interface for model editing, query testing, SQL compilation, ER diagrams, and OSI import/export |
| Custom Extensions   | Vendor-specific metadata at all model levels (model, data object, column, dimension, measure, metric)           |
| DB-API 2.0 Drivers  | PEP 249 drivers for all 8 databases with transparent OBML compilation                                          |
| Arrow Flight SQL    | Embedded gRPC server for DBeaver, Tableau, Power BI — single container, two ports                               |
| PostgreSQL Wire     | Native Postgres-protocol surface (v2.5.0+) — connect Tableau, DBeaver, Superset, Power BI, `psql`, or **Dremio as a federated Postgres source** without any extra driver |
| OBSL Graph & SPARQL | Every loaded model is exported as an OBSL-Core 0.1 RDF graph (Turtle) with a read-only SPARQL (SELECT/ASK) endpoint |
| Plugin Architecture | Extensible dialect system with capability flags                                                                 |
| Source Tracking     | Error messages with YAML line/column positions                                                                  |

## Quick Example

Define a semantic model in YAML:

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
      Country:
        code: COUNTRY
        abstractType: string

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
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

Compile a query to SQL:

```python
result = pipeline.compile(query, model, "postgres")
```

```sql
SELECT
  "Customers"."COUNTRY" AS "Country",
  SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
  ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
GROUP BY "Customers"."COUNTRY"
```

## Getting Started

Ready to dive in? Start with [Installation](getting-started/installation.md) and then follow the [Quick Start](getting-started/quickstart.md) tutorial.

---

<p align="center">
  <a href="https://ralforion.com"><img src="assets/RALFORION_doo_Logo.png" alt="RALFORION d.o.o." width="200"></a>
  <br>
  Copyright 2025 RALFORION d.o.o. &mdash; Licensed under BSL 1.1
</p>
