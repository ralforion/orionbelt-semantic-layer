<p align="center">
  <img src="assets/ORIONBELT Logo.png" alt="OrionBelt Logo" width="400">
</p>

# OrionBelt Semantic Layer

**Compile and execute YAML semantic models as analytical SQL across multiple database dialects.**

OrionBelt Semantic Layer is an **API-first** semantic engine and query planner for AI agents that compiles and executes declarative YAML model definitions as optimized SQL for BigQuery, ClickHouse, Databricks, Dremio, DuckDB/MotherDuck, Postgres, and Snowflake. Query using business concepts — dimensions, measures, and metrics — instead of raw SQL.

## Why OrionBelt?

- **One model, many dialects** — Define your semantic model once in YAML, compile and execute SQL for any supported warehouse
- **Safe by construction** — AST-based SQL generation prevents injection and ensures syntactic correctness
- **Precise error reporting** — Validation errors include line and column numbers from your YAML source
- **Automatic join resolution** — Declare relationships between data objects; OrionBelt finds optimal join paths using graph algorithms
- **Multi-fact support** — Composite Fact Layer (CFL) planning handles queries spanning multiple fact tables with UNION ALL and CTE-based aggregation
- **Session management** — TTL-scoped sessions isolate model state per client, enabling iterative development workflows

## Key Features

| Feature             | Description                                                                                                     |
| ------------------- | --------------------------------------------------------------------------------------------------------------- |
| 8 SQL Dialects      | BigQuery, ClickHouse, Databricks, Dremio, DuckDB/MotherDuck, MySQL, Postgres, Snowflake                         |
| OrionBelt ML (OBML) | YAML-based data objects, dimensions, measures, metrics, joins                                                   |
| Star Schema & CFL   | Automatic fact selection and join path resolution                                                               |
| Session Management  | TTL-scoped per-client sessions for the REST API                                                                 |
| REST API            | FastAPI endpoints for session-based model management, validation, compilation, execution, and OSI conversion               |
| Gradio UI           | Interactive web interface for model editing, query testing, SQL compilation, ER diagrams, and OSI import/export |
| Custom Extensions   | Vendor-specific metadata at all model levels (model, data object, column, dimension, measure, metric)           |
| DB-API 2.0 Drivers  | PEP 249 drivers for all 8 databases with transparent OBML compilation                                          |
| Arrow Flight SQL    | Embedded gRPC server for DBeaver, Tableau, Power BI — single container, two ports                               |
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
  <a href="https://ralforion.com"><img src="assets/RALFORION doo Logo.png" alt="RALFORION d.o.o." width="200"></a>
  <br>
  Copyright 2025 RALFORION d.o.o. &mdash; Licensed under BSL 1.1
</p>
