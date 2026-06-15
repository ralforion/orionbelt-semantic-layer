---
description: OrionBelt Semantic Layer is an open-source Semantic Sidecar for agentic AI, analytics, data quality, and governance. YAML models compile to SQL across 8 dialects, exposed via REST, MCP, Arrow Flight SQL, and Postgres wire.
---

<p align="center">
  <img src="assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

# OrionBelt Semantic Layer and Sidecar

**An open-source Semantic Sidecar for agentic AI, analytics, data quality, and governance systems.**

**Inject governed semantics into systems that never had them.**

OrionBelt Semantic Layer (OBSL) is a thin runtime that sits *next to* existing platforms — AI agents, BI tools, data-quality pipelines, regulatory and KPI reporting — and injects governed business semantics without forcing those systems to absorb a new semantic infrastructure.

Define dimensions, measures, metrics, business rules, and semantic context in declarative YAML models. OBSL compiles and executes them as optimized, dialect-specific SQL across **BigQuery, ClickHouse, Databricks, Dremio, DuckDB / MotherDuck, MySQL, PostgreSQL, and Snowflake** — surfaced over REST, MCP, Arrow Flight SQL, and the PostgreSQL wire protocol.

Query using **business concepts** — not raw tables and SQL. The same semantic model powers AI agents, analytics workflows, data-quality checks, regulatory and business KPIs, and reporting use cases. **Analytics as Code — and beyond.** The full loop from declarative YAML through executable SQL, DQ rules, KPIs, and semantic context to query results is programmable, reviewable, and reproducible — no BI tool in the middle.

## What is a Semantic Sidecar?

The *sidecar pattern* comes from container platforms: a small, focused process runs alongside a main application and adds capability the main process doesn't have, without changing it.

OBSL applies the same pattern to **semantics**:

- It's a **runtime, not a rewrite.** Existing BI tools, AI agents, governance systems, and DQ pipelines keep talking to their databases — OBSL attaches a governed semantic interface alongside them.
- It's **multi-surface by design.** The same model is reachable over REST (for agents and apps), MCP (for LLM clients), Arrow Flight SQL + PostgreSQL wire (for BI tools), and direct DB-API drivers (for Python). One model, many channels.
- It's **opinionated about correctness, not deployment.** Dialect-aware SQL generation, fan-trap-safe joins, and dimensional metrics are non-negotiable; where to run OBSL — embedded in your app, alongside a warehouse, behind a proxy, in a single container — is your call.
- It's **open by default.** BSL 1.1 today, converts to Apache 2.0 in 2030. No SaaS lock-in is required to use the full v2.6 surface.

## The OrionBelt trio

OBSL ships three named pillars. You author models in **OBML**, query them in **OBSQL**, and run it all on **OBSL**.

| Short form | Full name | What it is | Reference |
|---|---|---|---|
| **OBSL** | OrionBelt **Semantic Layer** | The system itself — compiler, planner, runtime, REST / Flight / Postgres-wire surfaces | this site |
| **OBML** | OrionBelt **Modeling Language** | Declarative YAML format for defining models | [OBML Model Format](guide/model-format.md) |
| **OBSQL** | OrionBelt **Semantic QL** | SQL surface BI tools and humans actually write — bare-label or `MEASURE()` syntax, aggregation-match validation, `WITH ROLLUP` / `WITH CUBE`, no escape hatch to raw SQL | [OBSQL reference](guide/semantic-ql.md) |

OBSQL flows over **Apache Arrow Flight SQL** (v2.4+) and **PostgreSQL wire** (v2.5+). Same language, two transports — pick whichever your BI tool prefers.

## Why OrionBelt?

- **One model, many consumers** — Author dimensions / measures / metrics / business rules once in YAML; agentic AI, BI tools, DQ pipelines, regulatory KPIs, and reporting all read from the same governed surface
- **Analytics as Code — and beyond** — Version-controlled YAML compiles to dialect-specific SQL, executable DQ rules, KPI definitions, and semantic context. No BI tool in the middle: the full loop from declarative model to query results is programmable, reviewable, and reproducible
- **One model, many dialects** — BigQuery, ClickHouse, Databricks, Dremio, DuckDB / MotherDuck, MySQL, PostgreSQL, and Snowflake — no runtime lock-in to any single warehouse
- **One model, many transports** — REST + MCP (for agents), Arrow Flight SQL + PostgreSQL wire (for BI tools), DB-API drivers (for Python). Choose the surface; the semantics are the same
- **Cross-schema & cross-database** — Model data objects across multiple schemas and databases; dimensions, measures, and metrics can span schema boundaries in a single query
- **Safe by construction** — AST-based SQL generation prevents injection and ensures syntactic correctness
- **Precise error reporting** — Validation errors include line and column numbers from your YAML source
- **Static model filters** — Bake mandatory WHERE conditions into the model (by status, region, date range, etc.) — applied to every query automatically, with auto-join extension and ISO 8601 date/timestamp support
- **Automatic join resolution** — Declare relationships between data objects; OrionBelt finds optimal join paths using graph algorithms
- **Multi-fact support** — Composite Fact Layer (CFL) planning handles queries spanning multiple fact tables with UNION ALL and CTE-based aggregation
- **Artefacts Composability Resolution (ACR)** — Given the query so far, the `composables` endpoint returns which dimensions, measures, and metrics can still be added and compile (including CFL candidates), powering guided query building for people and AI agents
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
| PostgreSQL Wire     | Native Postgres-protocol surface (v2.5.0+) — Tableau, DBeaver, Superset, Power BI, `psql`, and **Dremio as a federated Postgres source** connect via their built-in Postgres ODBC/JDBC driver; no new connector to install |
| OBSL Graph & SPARQL | Every loaded model is exported as an OBSL-Core 0.1 RDF graph (Turtle) with a read-only SPARQL (SELECT/ASK) endpoint |
| Plugin Architecture | Extensible dialect system with capability flags                                                                 |
| Source Tracking     | Error messages with YAML line/column positions                                                                  |

## Try the demo

**Hosted Gradio UI:** [orionbelt.ralforion.com](https://orionbelt.ralforion.com/ui/?__theme=dark) — pre-loaded example model, compile across dialects, see SQL instantly. ([Swagger](https://orionbelt.ralforion.com/docs) · [ReDoc](https://orionbelt.ralforion.com/redoc))

The hosted demo runs on Cloud Run (HTTPS-only by design), so the **PostgreSQL wire** and **Arrow Flight SQL** transports can't be exposed publicly there. Spin the same demo up locally in one `docker run` — same image, same baked-in `orionbelt_1_commerce` DuckDB dataset, plus all three transports:

```bash
docker run --rm -d --name orionbelt-demo \
  -p 8080:8080 -p 5432:5432 -p 8815:8815 \
  -e PGWIRE_ENABLED=true \
  -e FLIGHT_ENABLED=true \
  ralforion/orionbelt-api:latest

# REST + Gradio UI:
#   http://localhost:8080/ui
# pgwire (any psql / DBeaver / Tableau / Power BI / Superset / Metabase):
psql "host=localhost port=5432 user=obsl dbname=orionbelt_1_commerce sslmode=disable" \
  -c 'SELECT "Client Name", "Total Sales" LIMIT 5'
# Flight SQL smoke test:
uv run python examples/obsql.py 'SELECT "Client Name", "Total Sales" LIMIT 5'

docker stop orionbelt-demo
```

The container ships with `PGWIRE_AUTH_MODE=trust` (default) — fine for `localhost`, **not** safe to expose to the public internet until SCRAM / password auth lands on the pgwire surface.

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

## Commercial Offerings

OrionBelt Semantic Layer is open by default — the OSS distribution has full parity on the shipped v2.6 surface and is production-grade for self-hosted use. For teams that want production support, a managed runtime, or embedded analytics terms, RALFORION offers:

- **Embedded analytics license** — relicensing terms for shipping OBSL inside a commercial product
- **Commercial cloud offering** — managed OrionBelt runtime with SLAs
- **Enterprise features** — capabilities tailored for enterprise deployments
- **Consulting + support** — implementation, modeling, and production support

Contact [RALFORION d.o.o.](https://ralforion.com) for details.

---

<p align="center">
  <a href="https://ralforion.com"><img src="assets/RALFORION_doo_Logo.png" alt="RALFORION d.o.o." width="200"></a>
  <br>
  Copyright 2025 RALFORION d.o.o. &mdash; Licensed under BSL 1.1
</p>
