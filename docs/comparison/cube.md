# OBSL vs Cube

A feature comparison between **OrionBelt Semantic Layer (OBSL)** and **Cube** (formerly Cube.js — the open-source semantic layer from Cube Dev). Captured 2026-05-01.

---

## TL;DR

- **Cube wins on**: **pre-aggregations** (its flagship feature — materialized rollups with refresh, partitioning, and lambda strategies), a **Postgres-wire SQL API** with the broadest BI-tool compatibility today, GraphQL alongside REST, first-class multi-tenancy and row-level security via `query_rewrite` + JWT security contexts, Twig templating for dynamic models, and the broadest data-source portfolio of the OSS semantic layers.
- **OBSL wins on**: **richer modeling topologies** (multi-rooted DAG with named secondary join paths) where Cube assumes single-rooted cubes plus combining `views`; **first-class declarative metric types** for cumulative and period-over-period (Cube has `rolling_window` and `time_shift` but they're query/measure patterns, not metric *types*); an **RDF/SPARQL graph view** of the model and a matching **interactive ontology-graph playground**; **Apache Arrow Flight SQL** as a modern columnar wire protocol (vs. Cube's Postgres wire); 8 first-class **DB-API 2.0 drivers**; an explicit **CFL multi-fact planner**; OSI ↔ OBML format conversion; and a simpler operational footprint (no Redis, no scheduler, no separate query orchestrator).
- **Different niches**: Cube is "the production semantic-layer + caching + API gateway" — built to serve high-volume embedded analytics with millisecond response times. OBSL is "an embeddable semantic compiler with a clean REST surface and rich modeling primitives" — best when you don't need pre-aggregation infrastructure and want a smaller dependency footprint.

Cube is the closest peer to OBSL in the OSS space — both are self-hostable, both target embedded analytics, both expose REST/MCP. The interesting differences are in modeling topology, metric expressivity, and the caching/pre-aggregation layer.

---

## 1. Modeling philosophy

| Aspect | OBSL (OBML) | Cube |
|---|---|---|
| Format | Declarative YAML (`OBML`) | YAML or JavaScript (`.yml` or `.js` cube definitions); supports Twig/Jinja templating |
| Source of truth | YAML model files | `model/cubes/*.{yml,js}` and `model/views/*.{yml,js}` files |
| Top-level constructs | `dataObjects`, `dimensions`, `measures`, `metrics`, `filters` | `cubes` (with `measures`, `dimensions`, `segments`, `joins`, `pre_aggregations`), `views` (combining cubes), `pre_aggregations` |
| Object scoping | Each `DataObject` has `columns:`; dimensions/measures/metrics live at model scope | Measures and dimensions live *inside* each `cube`; `view`s expose a curated subset for end users |
| Templating | None (static YAML) | Twig (Jinja2-like) — `COMPILE_CONTEXT` for compile-time multi-tenancy, dynamic SQL, masking |
| Runtime | OSS, self-hosted (single Python service) | OSS Cube Core (Node.js + Cube Store + optional Redis) **or** Cube Cloud (managed, paid) |

---

## 2. Concept mapping

| OBSL | Cube | Notes |
|---|---|---|
| `DataObject` | `cube` | Both wrap a physical table or SQL block |
| `DataObject.columns` | `dimensions` on a cube | |
| `Dimension` (model-scoped) | n/a — dimensions live inside cubes | OBSL has model-scoped dimensions; Cube does not |
| `Measure` | `measures` on a cube | |
| `Metric` `type: derived` | `measure: { type: number; sql: ... }` referencing other measures | Both first-class |
| `Metric` `type: cumulative` | `measure: { type: sum; rolling_window: { trailing: '7 day' } }` | Cube has rolling windows but they live on individual measures, not as a separate metric type |
| `Metric` `type: period_over_period` | `time_shift` in queries (`compareDateRange`, `time_shift: { interval: '1 year', type: 'prior' }`) | Cube does PoP at *query time*, not in the model |
| `DataObjectJoin` | `joins:` inside a cube with `relationship: many_to_one`/`one_to_many`/`one_to_one` | Cube uses relationship-driven symmetric aggregates |
| Combining multiple data objects in one query | Native via join graph + CFL | `view` entity required to combine cubes; views are first-class but distinct from cubes |
| `secondary: true` + `pathName` | n/a — multiple joins between the same pair require workarounds | No path-name primitive |
| `QueryObject` JSON | Cube REST query shape (`measures`, `dimensions`, `filters`, `timeDimensions`, `segments`, `order`, `limit`) | Different shape but same idea |
| `Filter` (named, reusable) | `segments` (named WHERE-style filters reusable in queries) | |

---

## 3. The headline Cube features

### 3.1 Pre-aggregations (Cube's flagship)

Pre-aggregations are materialized rollups that Cube builds, refreshes, and routes queries through automatically. This is the single biggest Cube feature OBSL has no answer for.

```yaml
cubes:
  - name: orders
    pre_aggregations:
      - name: monthly_revenue
        measures: [total_revenue, order_count]
        dimensions: [status]
        time_dimension: created_at
        granularity: month
        partition_granularity: month
        refresh_key:
          every: 1 hour
        build_range_start:
          sql: SELECT DATE('2020-01-01')
        build_range_end:
          sql: SELECT NOW()
```

Capabilities:
- **Rollup** (most common), **original_sql**, **rollup_join**, **rollup_lambda** strategies
- **Partitioning** for incremental refresh
- **Cube Store** — Cube's columnar materialization engine (SQLite/Parquet-backed)
- **Query routing** — Cube transparently rewrites incoming queries to hit the matching pre-aggregation
- Sub-second query response times even on billion-row source tables

**OBSL** has no materialization story. "Make this faster" is the warehouse's job (materialized views, dbt models, scheduled jobs).

### 3.2 SQL wire protocol for BI tools (both have one)

Both projects expose a SQL wire protocol so BI tools can connect to the semantic layer like a database — just over **different wires**:

| | OBSL | Cube |
|---|---|---|
| Protocol | **Apache Arrow Flight SQL** (gRPC, port 8815) | **Postgres wire** (port 15432) |
| Underlying format | Columnar (Arrow) — modern, efficient for analytics | Row-based (Postgres) |
| BI tool support | DBeaver, Tableau (Flight SQL JDBC), Power BI (Flight SQL ODBC), `pyarrow.flight` | Tableau, Superset, Metabase, PowerBI, `psql`, anything with a Postgres driver |
| Maturity in BI ecosystem | Newer (Arrow Flight SQL is the modern standard but tool support is still rolling out) | Very wide — anything that speaks Postgres wire works |
| Self-hostable | Via `ob-flight-extension` package, daemon thread inside the API process | Built into Cube core |
| OBSL also ships | 8 PEP 249 DB-API drivers (`ob-bigquery`, `ob-snowflake`, `ob-postgres`, `ob-mysql`, `ob-duckdb`, `ob-clickhouse`, `ob-databricks`, `ob-dremio`) for direct programmatic access | n/a |

So this is a wash on capability, with Cube currently winning on **breadth of compatible tooling today** (Postgres wire is everywhere) and OBSL winning on **modern columnar transport** (Arrow Flight SQL is faster for analytical payloads, especially over the wire).

### 3.3 Multi-API parity

Cube exposes the same semantic model through:
- **REST API** (`/cubejs-api/v1/load`)
- **GraphQL API** (`/graphql`)
- **SQL API** (Postgres wire on port 15432)
- **MCP** (Model Context Protocol)

OBSL exposes:
- **REST API** (FastAPI)
- **Arrow Flight SQL** (gRPC port 8815, JDBC/ODBC via Arrow Flight SQL drivers)
- **DB-API 2.0 drivers** (8 PEP 249 packages)
- **MCP**

Cube uniquely has GraphQL; OBSL uniquely has DB-API drivers and the RDF/SPARQL surface. Both have REST + a SQL wire protocol + MCP.

### 3.4 Multi-tenancy and row-level security

Cube has first-class multi-tenancy:

- **Compile-time multi-tenancy** via `COMPILE_CONTEXT` in Twig templates — generate different schemas per tenant.
- **Runtime RLS** via `query_rewrite(query, { securityContext })` — inject filters based on JWT claims.
- **Member-level access control** via `public: false` on dimensions/measures, conditional masking in Twig.
- **JWT-based authentication** built into the API gateway.

**OBSL** has session-scoped models (TTL, max-age, rate limits) but no first-class user/auth model. Authn/authz is the host application's job.

### 3.5 Pre-aggregations + caching = production-grade serving

Beyond pre-aggregations, Cube has a tiered caching architecture: in-memory query cache, Redis (optional), and Cube Store. This is built for the embedded-analytics use case where you serve thousands of dashboard requests per second.

**OBSL** has no built-in caching. It's a stateless query compiler.

---

## 4. Metric types

| OBSL | Cube | Notes |
|---|---|---|
| `Measure` (sum/avg/count/min/max, `total: bool`) | `measures` (`type: sum/count/count_distinct/avg/min/max/number/string/time/boolean`) | Cube has more types out of the box |
| `Metric` `type: derived` | `measure: { type: number; sql: ... }` referencing other measures | Both first-class |
| `Metric` `type: cumulative` (running, rolling, grain-to-date) | `measure: { rolling_window: { trailing: '7 day', offset: 'end' } }` — rolling only | Cube has rolling windows but no grain-to-date or unbounded cumulative as a declarative type |
| `Metric` `type: period_over_period` (4 comparison modes) | Query-side: `compareDateRange` parameter or `time_shift: { interval: '1 year', type: 'prior' }` | Cube does PoP at query time, not as a model-defined metric |
| Reusable filter context / filtered measures | `segments` (named, reusable filter sets) + `filters` on individual measure definitions | Comparable |
| Grand totals (`total: true`) | n/a — done at query/visualization time | OBSL has it as a measure attribute |

**Different philosophies**: OBSL bakes time-aware metrics into the model (write once, every query gets the comparison). Cube treats time comparisons as query-time concerns (more flexible, but the consumer has to know how to ask). Either fits depending on whether you're publishing a metrics catalog or empowering query authors.

---

## 5. Joins

| | OBSL | Cube |
|---|---|---|
| Definition site | `joins:` array on `DataObject` | `joins:` block inside each `cube` |
| Cardinality | `joinType`: `many-to-one`, `one-to-one`, `many-to-many` | `relationship`: `one_to_one`, `one_to_many`, `many_to_one` |
| What cardinality drives | Static fanout detection + CFL multi-fact planning | Symmetric aggregates |
| Join condition | `columnsFrom`/`columnsTo` arrays | `sql: "{CUBE}.id = {other.foo_id}"` (free-form SQL with `{CUBE}` reference) |
| Multiple paths | First-class via `secondary: true` + named `pathName`, query-time selection via `usePathNames` | No path-name primitive; workaround via `view`s exposing one path or aliased cubes |
| Join direction | Directed, declared per data object | Bidirectional inference based on `relationship:` |
| Symmetric aggregates | ❌ (uses CFL) | ✅ |

Cube's `relationship:` keyword drives symmetric aggregate logic (similar to LookML and Malloy). OBSL takes the static-fanout-detection + CFL approach instead.

---

## 6. Data modeling topology (a major differentiator)

Cube is fundamentally **single-rooted per cube**: each cube has its own measures and dimensions, joins fan out from there. To combine multiple cubes into a unified end-user surface, Cube uses **views** — a separate entity that selectively re-exposes measures/dimensions from joined cubes. Views are good, but they don't escape the underlying single-rooted-per-cube topology, and multi-path scenarios (two valid joins between the same pair of cubes) aren't a first-class primitive.

OBSL is built on a **directed join graph (DAG)** with explicit support for richer topologies:

| Topology | Star (single fact + dims) | Snowflake (chained dims) | Multi-rooted (multiple facts) | Multi-path (alt. joins between same pair) | Cycles |
|---|---|---|---|---|---|
| **OBSL** | ✅ | ✅ | ✅ via CFL `UNION ALL` legs with per-leg common root | ✅ first-class via `secondary: true` + `pathName` + per-query `usePathNames` | Detected and rejected |
| **Cube** | ✅ | ✅ | Via `view`s combining cubes (workable but indirect) | Workaround via duplicate cubes or views | Implicit |

**Why this matters**: When you need a single semantic surface that exposes revenue + support tickets by customer, or to choose between `ship_address_id` and `billing_address_id` joins to the same address dimension per query, Cube wants you to design `view`s upstream and pick the right one. OBSL lets you model the messy graph as-is and resolve at query time.

---

## 7. Dialects / data sources

Cube supports a very broad list including all OBSL dialects plus more.

| Source | OBSL | Cube |
|---|---|---|
| BigQuery | ✅ | ✅ |
| Snowflake | ✅ | ✅ |
| Postgres | ✅ | ✅ |
| MySQL | ✅ | ✅ |
| DuckDB | ✅ | ✅ |
| Databricks | ✅ | ✅ |
| ClickHouse | ✅ | ✅ |
| Dremio | ✅ | ❌ |
| Redshift | ❌ | ✅ |
| Trino / Presto | ❌ | ✅ |
| MS SQL / Oracle | ❌ | ✅ |
| Druid | ❌ | ✅ |
| Athena, Hive, Vertica, etc. | ❌ | ✅ |

Cube wins on breadth (especially legacy enterprise sources); OBSL covers modern cloud warehouses well and is competitive on Databricks/ClickHouse, with Dremio unique to OBSL.

---

## 8. APIs / interfaces

| | OBSL | Cube |
|---|---|---|
| REST API | Yes — first-party FastAPI service | Yes — `/cubejs-api/v1/load`, mature |
| GraphQL | No | Yes |
| SQL wire protocol | **Apache Arrow Flight SQL** (gRPC, columnar) — JDBC/ODBC via Flight SQL drivers | **Postgres wire** (port 15432) — works with anything that speaks Postgres |
| DB-API 2.0 drivers | Yes — 8 PEP 249 drivers shipped (`ob-{bigquery,snowflake,postgres,mysql,duckdb,clickhouse,databricks,dremio}`) | No (SQL API supplants this) |
| MCP | Yes — first-party server | Yes — first-party server |
| Native SDKs | Python (FastAPI client) + DB-API drivers | JS/TS (`@cubejs-client/*`), React, Vue, Angular bindings |
| UI / Playground | Interactive Gradio playground: SQL Compiler, Query Results, auto-generated Mermaid ER diagrams, **interactive RDF/OBSL ontology graph** (vis-network), OSI import/export, settings panel | Cube Playground (OSS): query builder, schema browser, generated SQL preview. Cube Cloud Studio (paid) adds advanced workspace features |
| RDF graph + SPARQL | Yes (`/graph`, `/sparql`) | No |
| Format conversion | OSI ↔ OBML (`/convert/*`) | No equivalent |

Cube's SQL API is a structural advantage for BI-tool connectivity. OBSL's RDF/SPARQL surface is unique for governance/lineage tooling.

---

## 9. Open-source vs. commercial story

Both projects ship a free OSS core and offer commercial extensions, but the split is different:

| | OBSL | Cube |
|---|---|---|
| Core license | Source-available (BSL 1.1) | Apache 2.0 |
| Self-hostable | ✅ (one Python service) | ✅ (Cube Core: Node.js + optional Cube Store + optional Redis) |
| Commercial offering | Hosted instance (the public demo on Cloud Run) | Cube Cloud — managed runtime, multi-cluster, advanced security, Studio IDE, paid |
| Operational footprint | Light: one process, in-memory sessions | Heavier: API server + Cube Store + Redis (optional) + scheduler + refresh workers in production |
| Self-host parity | Full feature parity in OSS | Many advanced features (Studio, advanced workspaces, AI features) are Cube Cloud-only |

For a small embedded-analytics use case OBSL is operationally simpler. For high-throughput multi-tenant production with heavy caching, Cube's architecture is purpose-built and Cube Cloud provides the managed experience.

---

## 10. Other distinctives

| Feature | OBSL | Cube |
|---|---|---|
| Pre-aggregations / materialized rollups | ❌ | ✅ flagship |
| SQL wire protocol for BI tools | ✅ Arrow Flight SQL (gRPC, columnar) | ✅ Postgres wire (row-based, broader BI tool support today) |
| DB-API 2.0 drivers (PEP 249) | ✅ 8 drivers shipped | ❌ |
| Interactive RDF/ontology graph in playground | ✅ vis-network ontology view | ❌ (schema browser only) |
| GraphQL API | ❌ | ✅ |
| Twig/Jinja templating | ❌ | ✅ |
| Multi-tenancy primitives | Session-scoped only | ✅ first-class (compile-time + runtime) |
| Row-level security in model | ❌ | ✅ via `query_rewrite` |
| Field-level masking | ❌ | ✅ via Twig conditionals |
| First-class PoP metric type | ✅ (4 comparison modes) | ❌ (`time_shift` at query time) |
| First-class cumulative metric type | ✅ (running, rolling, grain-to-date) | Partial (`rolling_window` only) |
| Multi-rooted DAG modeling | ✅ via CFL | ❌ (single-rooted cubes + views) |
| Named secondary join paths | ✅ | ❌ |
| Symmetric aggregates | ❌ (uses CFL) | ✅ |
| RDF/SPARQL graph view | ✅ | ❌ |
| OSI ↔ OBML conversion | ✅ | ❌ |
| Built-in caching layer | ❌ | ✅ multi-tier |
| Cube Store / materialization engine | n/a | ✅ |
| Operational simplicity | Single Python process | Multiple services in production |

---

## 11. When to pick which

### Pick **Cube** when:

- You need **pre-aggregations** for sub-second analytics on large datasets — this is Cube's defining feature and OBSL has no equivalent.
- Your BI tools speak **Postgres wire protocol** and you want the broadest tool compatibility today (vs. OBSL's Arrow Flight SQL, which is the modern columnar standard but has narrower current tool coverage).
- You need **GraphQL** alongside REST (e.g. for a frontend that already uses Apollo).
- You need first-class **multi-tenancy + RLS** without writing it yourself.
- Your model needs **dynamic SQL via templating** (Twig/Jinja) — per-tenant table names, masking, conditional logic.
- You're building **high-throughput embedded analytics** with thousands of concurrent dashboard requests.
- You're willing to operate Node.js + Cube Store + Redis, or pay for Cube Cloud.

### Pick **OBSL** when:

- You need **richer modeling topologies** — multi-rooted facts, named alternative join paths — and don't want to design around them via views.
- You want **first-class declarative cumulative and period-over-period metric types** instead of expressing them via query-time `time_shift` or per-measure `rolling_window`.
- You want a **graph view of the model** (RDF/SPARQL) for governance/lineage tooling.
- You need **OSI interoperability** for moving models between semantic layer formats.
- Your operational appetite is small — **one Python service**, no Redis, no scheduler, no separate query orchestrator.
- You're targeting **Dremio** or otherwise want full feature parity in self-hosted OSS without a Cloud upgrade path.
- Your consumers are **agents/LLMs** primarily — both projects expose MCP, but OBSL's smaller surface is easier to point an agent at without query-shape ambiguity.

### They could coexist

A workable hybrid: use Cube as the production query gateway with pre-aggregations and a SQL API for BI tools, and OBSL as a complementary modeling/governance surface (RDF graph, OSI export, richer topology modeling) that you can keep authoritative and mirror into Cube definitions. Two semantic surfaces is operationally heavier, but it's defensible if you need both Cube's caching and OBSL's modeling primitives.

---

## 12. Gap analysis

### To match Cube, OBSL would need:

1. **Pre-aggregations / materialization** — declarative rollup definitions, refresh strategies, query routing. This is a major piece of infrastructure (a Cube-Store-equivalent or an integration with an external materialization engine like dbt/MaterializedView).
2. **Postgres-wire SQL API** — Arrow Flight SQL covers the BI-tool-connectivity use case via JDBC/ODBC drivers, but Postgres wire still has broader native tool support today (Tableau, Superset, Metabase, plain `psql`, etc.).
3. **GraphQL API** — modest effort given the existing FastAPI surface.
4. **Templating in the model** — Jinja2 or similar for compile-time dynamism.
5. **Multi-tenancy primitives** — compile-time per-tenant model generation, runtime RLS hooks, JWT integration.
6. **Field-level masking and access grants** — analogous to LookML's `required_access_grants`.
7. **More dialects** — Trino/Presto, Redshift, MSSQL, Oracle if those markets matter.
8. **Built-in caching** — at minimum, an in-memory query result cache.

### To match OBSL, Cube would need:

1. **First-class cumulative & period-over-period metric types** — declarative versions of what's currently `rolling_window` and query-side `time_shift`.
2. **Multi-rooted DAG modeling** — the ability to query across genuinely independent fact tables in one go without the consumer needing to pre-design a `view`.
3. **Named secondary join paths** with per-query selection.
4. **RDF/SPARQL graph surface** for governance/lineage.
5. **Apache Arrow Flight SQL** as a columnar wire protocol option alongside Postgres wire — modern analytical payloads transfer more efficiently in Arrow.
6. **First-class DB-API 2.0 drivers** for direct programmatic access from Python (PEP 249).
7. **Interactive RDF/ontology graph visualization** in the playground (vis-network-style) — Cube Playground's schema browser is functional but not graph-shaped.
8. **OSI ↔ Cube model round-trip** for portability between semantic layers.

---

## References

- OBSL `MetricType` enum: `src/orionbelt/models/semantic.py`
- OBSL CFL planner: `src/orionbelt/compiler/cfl.py`
- OBSL fanout detection: `src/orionbelt/compiler/fanout.py`
- OBSL docs: [Model Format](../guide/model-format.md), [Period-over-Period Metrics](../guide/period-over-period.md), [Compilation Pipeline](../guide/compilation.md)
- Cube docs: https://cube.dev/docs
- Cube data modeling reference: https://cube.dev/docs/product/data-modeling/reference
- Cube pre-aggregations: https://cube.dev/docs/product/caching/using-pre-aggregations
- Cube SQL API: https://cube.dev/docs/product/apis-integrations/sql-api
