# OBSL vs Cube

A feature comparison between **OrionBelt Semantic Layer (OBSL)** and **Cube** (formerly Cube.js — the open-source semantic layer from Cube Dev). Captured 2026-05-23, refreshed 2026-07-31 against **Cube Core v1.7** (Tesseract GA, 2026-07-08).

---

## TL;DR

- **Cube wins on**: **pre-aggregations** (its flagship feature — materialized rollups with refresh, partitioning, and lambda strategies), GraphQL alongside REST, first-class multi-tenancy and row-level security via `query_rewrite` + JWT security contexts + **data access policies with conditional masking**, Twig templating for dynamic models, a **curated-view surface** (folders, view groups, hierarchies, AI context), **custom granularities and calendar cubes** (fiscal / 4-5-4), **multi-stage calculations**, and the broadest data-source portfolio of the OSS semantic layers.
- **OBSL wins on**: **first-class declarative metric types** for cumulative (with `partitionBy`), period-over-period, and **window** (rank / lag / lead / ntile / first_value / last_value) where Cube has `rolling_window`, `time_shift`, and multi-stage measures but no declarative rank/lag metric *type*; **9 statistical aggregates** (CORR / COVAR_* / REGR_* / STDDEV_* / VAR_*); **named secondary join paths with per-query selection** (`pathName` + `usePathNames`), which Cube has no primitive for; an **RDF/SPARQL graph view** of the model and a matching **interactive ontology-graph playground**; **two SQL wire protocols** — PostgreSQL wire AND Arrow Flight SQL (v2.5.0+) — where Cube ships Postgres wire only; 8 first-class **DB-API 2.0 drivers**; a **`UNION ALL` CFL multi-fact planner** that needs no per-fact-to-shared-dimension direct join; **OSI ↔ OBML format conversion**; and a simpler operational footprint (no Redis, no scheduler, no separate query orchestrator).
- **Changed since the 2026-05 capture**: multi-fact modeling is **no longer an OBSL-only capability**. Cube Core v1.7 made **Tesseract** the default planner and graduated **multi-fact views** to GA — a view spanning several fact tables now compiles to one aggregating subquery per fact, `FULL JOIN`ed on the shared dimensions. The remaining topology difference is narrower and more specific (see [§6](#6-data-modeling-topology-still-a-differentiator-but-a-narrower-one)).
- **Different niches**: Cube is "the production semantic-layer + caching + API gateway" — built to serve high-volume embedded analytics with millisecond response times. OBSL is "an embeddable semantic compiler with a clean REST surface and rich modeling primitives" — best when you don't need pre-aggregation infrastructure and want a smaller dependency footprint.

Cube is the closest peer to OBSL in the OSS space — both are self-hostable, both target embedded analytics, both expose REST/MCP. The interesting differences are in modeling topology, metric expressivity, and the caching/pre-aggregation layer.

---

## 1. Modeling philosophy

| Aspect | OBSL (OBML) | Cube |
|---|---|---|
| Format | Declarative YAML (`OBML`) | YAML or JavaScript (`.yml` or `.js` cube definitions); supports Twig/Jinja templating |
| Source of truth | YAML model files | `model/cubes/*.{yml,js}` and `model/views/*.{yml,js}` files |
| Top-level constructs | `dataObjects`, `dimensions`, `measures`, `metrics`, `filters` | `cubes` (with `measures`, `dimensions`, `segments`, `joins`, `pre_aggregations`), `views` (combining cubes), `pre_aggregations` |
| Repeated columns (`ARRAY<STRUCT>`) | Declared as a data object with `nestedIn`; unnested per dialect — see [Nested data objects](../guide/model-format.md#nested-data-objects-nestedin) | Flatten in the cube's `sql` / a pre-aggregation |
| Object scoping | Each `DataObject` has `columns:`; dimensions/measures/metrics live at model scope | Measures and dimensions live *inside* each `cube`; `view`s expose a curated subset for end users |
| Templating | None (static YAML) | Twig (Jinja2-like) — `COMPILE_CONTEXT` for compile-time multi-tenancy, dynamic SQL, masking |
| Runtime | OSS, self-hosted (single Python service) | OSS Cube Core (Node.js + Cube Store + optional Redis) **or** Cube Cloud (managed, paid). Since v1.7 the **Tesseract** planner and the native query orchestrator are on by default |
| Curation layer | None — one global namespace of dimensions / measures / metrics / filters | `views` are the primary consumer-facing surface: curated members, folders, view groups, hierarchies, access policies, AI context |

---

## 2. Concept mapping

| OBSL | Cube | Notes |
|---|---|---|
| `DataObject` | `cube` | Both wrap a physical table or SQL block |
| `DataObject.columns` | `dimensions` on a cube | |
| `Dimension` (model-scoped) | n/a — dimensions live inside cubes | OBSL has model-scoped dimensions; Cube does not |
| `Measure` | `measures` on a cube | |
| `Metric` `type: derived` | `measure: { type: number; sql: ... }` referencing other measures | Both first-class |
| `Metric` `type: cumulative` | <pre><code>measure:<br> type: sum<br> rolling_window:<br> trailing: 7 day</code></pre> | Cube has rolling windows but they live on individual measures, not as a separate metric type |
| `Metric` `type: period_over_period` | `time_shift` in queries (`compareDateRange`, or <pre><code>time_shift:<br> interval: 1 year<br> type: prior</code></pre>) | Cube does PoP at *query time*, not in the model |
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

### 3.2 SQL wire protocols for BI tools (OBSL has both; Cube has Postgres wire only)

Both projects expose a SQL wire protocol so BI tools can connect to the semantic layer like a database. As of OBSL v2.5.0, **OBSL ships two wires side-by-side** so you can pick the right transport per consumer:

| | OBSL | Cube |
|---|---|---|
| PostgreSQL wire | ✅ port 5432 (configurable via `PGWIRE_PORT`) — works with Tableau, DBeaver, Superset, Metabase, Power BI, plain `psql`, **Dremio's Postgres-source connector**, anything with a Postgres driver | ✅ port 15432 |
| Apache Arrow Flight SQL | ✅ gRPC port 8815 — columnar transport, JDBC/ODBC via Flight SQL drivers, `pyarrow.flight` programmatic | ❌ |
| DB-API 2.0 drivers (PEP 249) | ✅ 8 first-party packages (`ob-{bigquery,snowflake,postgres,mysql,duckdb,clickhouse,databricks,dremio}`) for direct programmatic access | ❌ (SQL API supplants this) |
| Read-only governance | Closed by design: raw SQL → `RAW_SQL_REJECTED`, DDL/DML → `WRITE_OPERATION_REJECTED`, catalog probes answered from the model | Cube SQL API rejects writes; raw SELECTs flow through |
| Self-hostable | Postgres wire is built into the API process; Flight SQL via `ob-flight-extension` daemon thread | Built into Cube core |

So OBSL covers the **same broad-BI-tool surface** as Cube (Postgres wire is everywhere), **plus** Arrow Flight SQL for consumers that benefit from modern columnar transport (cheaper round-trips for analytical payloads), **plus** DB-API drivers for Python-native programmatic access. Pick the wire that matches the consumer — for an existing Tableau farm, point it at OBSL's pgwire and it just works.

### 3.3 Multi-API parity

Cube exposes the same semantic model through:
- **REST API** (`/cubejs-api/v1/load`)
- **GraphQL API** (`/graphql`)
- **SQL API** (Postgres wire on port 15432)
- **MCP** (Model Context Protocol)

OBSL exposes:
- **REST API** (FastAPI)
- **PostgreSQL wire** (TCP port 5432, v2.5.0+, BI-tool / `psql` / Dremio-source compatible)
- **Arrow Flight SQL** (gRPC port 8815, JDBC/ODBC via Arrow Flight SQL drivers)
- **DB-API 2.0 drivers** (8 PEP 249 packages)
- **MCP**

Cube uniquely has GraphQL; OBSL uniquely has Arrow Flight SQL, DB-API drivers, and the RDF/SPARQL surface. Both have REST + Postgres-wire SQL + MCP.

### 3.4 Multi-tenancy and row-level security

Cube has first-class multi-tenancy:

- **Compile-time multi-tenancy** via `COMPILE_CONTEXT` in Twig templates — generate different schemas per tenant.
- **Runtime RLS** via `query_rewrite(query, { securityContext })` — inject filters based on JWT claims.
- **Member-level access control** via `public: false` on dimensions/measures, conditional masking in Twig.
- **JWT-based authentication** built into the API gateway.

**OBSL** has session-scoped models (TTL, max-age, rate limits) but no first-class user/auth model. Authn/authz is the host application's job.

### 3.5 Caching: different goals, different shapes

Both projects have caching, but they're solving adjacent problems with different primitives.

**Cube** has a tiered caching architecture aimed at high-throughput dashboard serving: in-memory query cache, optional Redis, and **Cube Store** (its purpose-built rollup store backing pre-aggregations). TTL is configured on each cube/measure as a refresh-key SQL or interval — the model author decides when entries are stale, and Cube's scheduler refreshes pre-aggs in the background. This is built for embedded-analytics workloads serving thousands of requests per second.

**OBSL** has a **result cache based on freshness inheritance** (`CACHE_BACKEND=file`, off by default). The structural difference is where the freshness contract lives:

- **Cube**: TTL is attached to the *abstraction* (cube / pre-agg / measure). Two cubes reading the same physical table each declare their own TTL — and they can drift.
- **OBSL**: TTL is derived from the `refresh:` contract on each touched physical `dataObject`. The minimum contribution wins. Two `dataObject` entries on the same warehouse table inherit the same contract automatically. One ETL `POST /v1/heartbeat` invalidates every cached query that depends on the refreshed table — across every dataObject and every session, in one call.

OBSL ships `static | scheduled | heartbeat | unknown` modes and exposes `GET /v1/cache/stats`, `POST /v1/cache/sweep`, `POST /v1/cache/clear` for observability and manual control. The backend is single-process (DuckDB metadata + Parquet sidecars), so it's per-replica — not a tiered store like Cube's. **For multi-replica deployments**, OBSL still recommends `CACHE_BACKEND=noop` until a shared backend (Redis or similar) lands.

**Pre-aggregations are still Cube-only.** OBSL caches *the result of a query that ran against the warehouse*; Cube can additionally pre-materialise rollups in Cube Store and route queries through them, which is a different category of optimisation. If your bottleneck is repeat queries, OBSL's result cache helps. If it's queries that scan billions of rows even on first hit, you still need Cube's pre-aggs (or warehouse-side materialised views, or dbt incremental models).

---

## 4. Metric types

### 4.1 Aggregation surface on `Measure`

| Family | OBSL | Cube |
|---|---|---|
| Standard | `sum`, `count`, `count_distinct`, `avg`, `min`, `max` | `sum`, `count`, `count_distinct`, `count_distinct_approx`, `avg`, `min`, `max` |
| Shape | `any_value`, `median`,<br>`mode`, `listagg` | `string`, `time`, `boolean`,<br>`number` (generic — wraps any<br>aggregate SQL expression) |
| Statistical | `stddev`, `stddev_pop`, `variance`, `var_pop` | Via `type: number` + raw `sql: STDDEV(...)` — not a first-class measure type |
| Association / regression | `corr`, `covar_pop`, `covar_samp`, `regr_slope`, `regr_intercept` | Via `type: number` + raw SQL — not a first-class measure type |
| Grand totals | `total: bool` on the measure | n/a — done at query/visualization time |

**The honest difference**: Cube's `type: number` is a generic escape hatch — anything the warehouse can express in SQL is reachable, but the model authors write raw SQL that's dialect-specific, arity-unchecked, and opaque to validation. OBSL ships these as first-class declarative aggregations with arity validation (2-column for `corr` / `covar_*` / `regr_*`), per-dialect gating (`UNSUPPORTED_AGGREGATION_FOR_DIALECT` at compile time), and identical YAML across all 8 dialects. Both approaches reach the same SQL functions in the end; the difference is who owns dialect portability.

### 4.2 Metric types

| OBSL | Cube | Notes |
|---|---|---|
| `Metric` `type: derived` — references measures **and other derived metrics, at any depth** | <pre><code>measure:<br> type: number<br> sql: "{revenue} / {orders}"</code></pre>, plus `multi_stage:` measures for staged aggregation (Tesseract) | Both first-class and both composable; Cube's `multi_stage` reaches staged shapes (`group_by`, `reduce_by`, `add_group_by`) OBSL has no answer for |
| `Metric` `type: cumulative` (running, rolling, grain-to-date, **per-dimension `partitionBy`**) | <pre><code>measure:<br> type: sum<br> rolling_window:<br> trailing: 7 day<br> offset: end</code></pre> rolling only | Cube has rolling windows but no grain-to-date, no unbounded cumulative, and no `partitionBy` as declarative types |
| `Metric` `type: period_over_period` (4 comparison modes) | Query-side: `compareDateRange` parameter, or <pre><code>time_shift:<br> interval: 1 year<br> type: prior</code></pre> | Cube does PoP at query time, not as a model-defined metric |
| `Metric` `type: window` — <br>`rank`, `dense_rank`, `row_number`, `ntile`,<br>`lag`, `lead`, `first_value`, `last_value` | Via `type: number` + raw window-function SQL | OBSL ships these as declarative metric types; Cube reaches them via the same `type: number` escape hatch |
| Reusable filter context / filtered measures | `segments` + per-measure `filters` | Comparable |

**Different philosophies**: OBSL bakes time-aware metrics into the model (write once, every query gets the comparison). Cube treats time comparisons as query-time concerns (more flexible, but the consumer has to know how to ask). Either fits depending on whether you're publishing a metrics catalog or empowering query authors.

**Cube's multi-stage measures** (GA with Tesseract in v1.7) are the generic version of this axis: `multi_stage: true` with `group_by` / `reduce_by` / `add_group_by` directives expresses staged aggregations — ratios against a partition total, rankings, nested aggregates — that OBSL would need a new typed metric for. v1.7 added grain directives and filter directives on top. OBSL is cleaner and more portable where it has a first-class type; Cube is more flexible for shapes the type system didn't anticipate.

See [Trend Analysis](../guide/trend-analysis.md) for OBSL's full v2.6 surface (window functions, statistical aggregates, dialect coverage matrix).

---

## 5. Joins

| | OBSL | Cube |
|---|---|---|
| Definition site | `joins:` array on `DataObject` | `joins:` block inside each `cube` |
| Cardinality | `joinType`: `many-to-one`, `one-to-one`, `many-to-many` | `relationship`: `one_to_one`, `one_to_many`, `many_to_one` |
| What cardinality drives | Static fanout detection + CFL multi-fact planning + grain dedup for one-side measures | Symmetric aggregates |
| Join condition | `columnsFrom`/`columnsTo` arrays | `sql: "{CUBE}.id = {other.foo_id}"` (free-form SQL with `{CUBE}` reference) |
| Multiple paths | First-class via `secondary: true` + named `pathName`, query-time selection via `usePathNames` | No path-name primitive; workaround via `view`s exposing one path or aliased cubes |
| Join direction | Directed, declared per data object | Bidirectional inference based on `relationship:` |
| Symmetric aggregates | ❌ — CFL, plus a grain-dedup CTE covering the one-side-measure case | ✅ general-purpose |

Cube's `relationship:` keyword drives symmetric aggregate logic (similar to LookML and Malloy). OBSL takes the static-fanout-detection + CFL approach instead, with a [grain-dedup CTE](../guide/compilation.md#phase-22-grain-deduplication-wrap) for the case a purely structural approach cannot cover: a measure sourced from the *one* side of a join, which the join replicates.

The dedup envelope has widened since it shipped. A deduplicated measure now composes with `total: true`, with cumulative and window metrics, with **derived metrics** (each component is computed on its own, the deduplicated ones in their own CTE, and the formula is rebuilt in the outer projection) and with **`HAVING`** (predicates naming a deduplicated measure move to the wrapper's outer `WHERE`, while base-grain predicates stay in `HAVING`). What the rewrite still cannot express — `filterContext`, period-over-period, `ROLLUP`/`CUBE`, a `grain` override on a deduplicated measure — raises a fanout-class error rather than returning an inflated number.

This is the structural trade against symmetric aggregates: Cube corrects fanout during aggregation for a broad class of queries and is quiet when it does; OBSL rewrites the query for the cases it can express and **refuses** the rest with a named reason. Neither is strictly better — a refusal is a worse experience than an answer, and a silently wrong number is worse than both.

---

## 6. Data modeling topology (still a differentiator, but a narrower one)

**This section changed materially in Cube Core v1.7 (2026-07-08).** The older framing — "Cube is single-rooted, OBSL is the only one that can span independent facts" — is no longer accurate and is corrected here.

Cube now ships **multi-fact views**, powered by the **Tesseract** planner (GA and default-on as of v1.7; before that it was behind `CUBEJS_TESSERACT_SQL_PLANNER`). When a view includes measures from several fact tables, Cube picks the root dynamically per query and compiles:

1. one **independent aggregating subquery per fact**, joining only the tables that fact needs, filtering, and grouping by the shared dimensions;
2. a **`FULL JOIN` across those subqueries** on all common dimension columns, so rows present in only one fact survive;
3. a side-by-side projection, `NULL` where a fact has no matching row.

A dedicated view isn't even required: through the SQL API, joining two views on shared dimensions and grouping by them is recognised and rewritten into the same multi-fact execution.

That is a genuine peer of OBSL's CFL, reached by a different SQL shape — and it is the same shape dbt MetricFlow uses (`FULL OUTER JOIN` on the shared entity), not the `UNION ALL` shape CFL uses.

| Topology | Star (single fact + dims) | Snowflake (chained dims) | Multi-rooted (multiple facts) | Multi-path (alt. joins between same pair) | Cycles |
|---|---|---|---|---|---|
| **OBSL** | ✅ | ✅ | ✅ via CFL `UNION ALL` legs with per-leg common root | ✅ first-class via `secondary: true` + `pathName` + per-query `usePathNames` | Detected and rejected |
| **Cube** | ✅ | ✅ | ✅ via multi-fact views — per-fact subqueries `FULL JOIN`ed on shared dimensions (Tesseract) | Workaround via duplicate cubes or a `view` pinning a `join_path` | Implicit |

**What still differs**, now that "can you query two facts at once" is answered yes on both sides:

| | OBSL (CFL) | Cube (multi-fact views) |
|---|---|---|
| Plan shape | `UNION ALL` legs, NULL-padded, re-aggregated in an outer query | Per-fact subqueries `FULL JOIN`ed on the shared dimension columns |
| Facts in one `FROM` | Never — the chasm trap is impossible by construction | Never in the *inner* subqueries either; the `FULL JOIN` is over pre-aggregated results |
| Requires a curated view | No — resolved from the join graph per query | Optional, but the modeling is view-shaped; the SQL API can infer it from a join of two views |
| Shared-dimension requirement | Each leg anchors on its own common root, found by graph search; intermediate hops are fine | Every fact cube must declare a **direct** join to each shared dimension, at a root-level join path |
| Time dimensions | Grain handled per leg | Join-key granularity must match the `GROUP BY` granularity |
| Path ambiguity | Per-query `usePathNames` selects the named path | Dijkstra + member-type heuristic; pin with a view's `join_path` at model-design time |

So the honest summary is: **Cube closed the multi-fact gap; it did not close the multi-path gap.** Choosing between `ship_address_id` and `billing_address_id` joins to the same address dimension is still a model-design decision in Cube (pin it in a view) and still a per-query one in OBSL. And Cube's multi-fact requirement that every fact join each conformed dimension *directly* is a real modeling constraint that CFL's common-root search does not impose.

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
| SQL wire protocol | **PostgreSQL wire** (port 5432) **AND Apache Arrow Flight SQL** (gRPC, columnar) — pick per consumer | **Postgres wire** (port 15432) only |
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
| Core license | Source-available (BUSL-1.1) | Apache 2.0 |
| Self-hostable | ✅ (one Python service) | ✅ (Cube Core: Node.js + optional Cube Store + optional Redis) |
| Commercial offering | Embedded analytics license · commercial cloud offering · enterprise features · consulting + support | Cube Cloud — managed runtime, multi-cluster, advanced security, Studio IDE, paid |
| Operational footprint | Light: one process, in-memory sessions, optional file-backed result cache (DuckDB metadata + Parquet) on local disk | Heavier: API server + Cube Store + Redis (optional) + scheduler + refresh workers in production |
| Self-host parity | OSS has full parity on the shipped v2.6 surface; enterprise tier adds enterprise-specific capabilities on top | Many advanced features (Studio, advanced workspaces, AI features) are Cube Cloud-only |

For a small embedded-analytics use case OBSL is operationally simpler. For high-throughput multi-tenant production with heavy caching across replicas, Cube's architecture is purpose-built and Cube Cloud provides the managed experience. OBSL's file cache based on freshness inheritance (v2.2.0) covers single-replica result-caching workloads — agents, dev/staging, modest production — without standing up Redis or a rollup store.

---

## 10. Other distinctives

| Feature | OBSL | Cube |
|---|---|---|
| Pre-aggregations / materialized rollups | ❌ | ✅ flagship |
| SQL wire protocol for BI tools | ✅ PostgreSQL wire **+** Arrow Flight SQL (two wires side-by-side, v2.5.0+) | ✅ Postgres wire only |
| DB-API 2.0 drivers (PEP 249) | ✅ 8 drivers shipped | ❌ |
| Interactive RDF/ontology graph in playground | ✅ vis-network ontology view | ❌ (schema browser only) |
| GraphQL API | ❌ | ✅ |
| Twig/Jinja templating | ❌ | ✅ |
| Multi-tenancy primitives | Session-scoped only | ✅ first-class (compile-time + runtime) |
| Row-level security in model | ❌ | ✅ via `query_rewrite` **and** declarative data access policies |
| Field-level masking | ❌ | ✅ via Twig conditionals **and** conditional masking in access policies (v1.7) |
| Curated business-facing surface | Global namespace + `examples` | ✅ views with folders, view groups, hierarchies, AI context |
| Dimension hierarchies / drill paths | ❌ | ✅ `hierarchies` on cubes, surfaced through views |
| Custom granularities / fiscal calendars | ❌ (fixed `TimeGrain` set) | ✅ custom granularities + calendar cubes (4-5-4, fiscal) |
| Multi-stage / staged calculations | ❌ (typed metrics only) | ✅ `multi_stage` measures (Tesseract) |
| First-class PoP metric type | ✅ (4 comparison modes) | ❌ (`time_shift` at query time) |
| First-class cumulative metric type | ✅ (running, rolling, grain-to-date, `partitionBy`) | Partial (`rolling_window` only) |
| First-class window metric type (rank / lag / lead / ntile / first_value / last_value) | ✅ | Via `type: number` + raw SQL |
| Statistical aggregates (`stddev`, `variance`, `corr`, `covar_*`, `regr_*`) as first-class measure types | ✅ 9 declarative aggregations | Via `type: number` + raw SQL |
| Multi-rooted DAG modeling | ✅ via CFL (`UNION ALL` legs, common root per leg) | ✅ via multi-fact views (per-fact subqueries `FULL JOIN`ed on shared dims) — **new in v1.7** |
| Named secondary join paths, selectable per query | ✅ | ❌ (pin a path in a view at design time) |
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
- You need **GraphQL** alongside REST (e.g. for a frontend that already uses Apollo).
- You need first-class **multi-tenancy + RLS** without writing it yourself.
- Your model needs **dynamic SQL via templating** (Twig/Jinja) — per-tenant table names, masking, conditional logic.
- You're building **high-throughput embedded analytics** with thousands of concurrent dashboard requests.
- You're willing to operate Node.js + Cube Store + Redis, or pay for Cube Cloud.
- Your model needs **hierarchies, folders, custom granularities, or a fiscal / 4-5-4 calendar** — all first-class in Cube, none present in OBSL.
- You need **staged calculations** (`multi_stage`) beyond what a typed metric system anticipates.

### Pick **OBSL** when:

- You need **per-query join-path selection** — two valid joins between the same pair of objects, chosen by the consumer rather than pinned in a view at design time. (Multi-fact alone is no longer a reason: Cube v1.7 does that too.)
- Your facts reach shared dimensions **through intermediate hops** — Cube's multi-fact views require each fact to join every shared dimension directly.
- You want **first-class declarative cumulative and period-over-period metric types** instead of expressing them via query-time `time_shift` or per-measure `rolling_window`.
- You want a **graph view of the model** (RDF/SPARQL) for governance/lineage tooling.
- You need **OSI interoperability** for moving models between semantic layer formats.
- Your operational appetite is small — **one Python service**, no Redis, no scheduler, no separate query orchestrator.
- You're targeting **Dremio** or otherwise want full feature parity in self-hosted OSS without a Cloud upgrade path.
- Your consumers are **agents/LLMs** primarily — both projects expose MCP, but OBSL's smaller surface is easier to point an agent at without query-shape ambiguity.

### They could coexist

A workable hybrid: use Cube as the production query gateway with pre-aggregations and a SQL API for BI tools, and OBSL as a complementary modeling/governance surface (RDF graph, OSI export, richer topology modeling, result cache based on freshness inheritance for agent workloads) that you can keep authoritative and mirror into Cube definitions. Two semantic surfaces is operationally heavier, but it's defensible if you need both Cube's pre-aggs and OBSL's modeling primitives.

---

## 12. Gap analysis

### To match Cube, OBSL would need:

1. **Pre-aggregations / materialization** — declarative rollup definitions, refresh strategies, query routing. This is a major piece of infrastructure (a Cube-Store-equivalent or an integration with an external materialization engine like dbt/MaterializedView). OBSL's result cache shipped in v2.2.0 (file backend, freshness-derived TTL) addresses the "repeat-query" axis but not the "first-hit on a billion rows" axis pre-aggs cover.
2. **GraphQL API** — modest effort given the existing FastAPI surface.
3. **Templating in the model** — Jinja2 or similar for compile-time dynamism.
4. **Multi-tenancy primitives** — compile-time per-tenant model generation, runtime RLS hooks, JWT integration.
5. **Field-level masking and access grants** — analogous to LookML's `required_access_grants`.
6. **More dialects** — Trino/Presto, Redshift, MSSQL, Oracle if those markets matter.
7. **Shared cache backend** for multi-replica deployments — current file backend is per-replica; Cube's Redis/Cube-Store layer is shared across the cluster.
8. **A curated view / perspective construct** — Cube's `views` are now the primary user-facing surface, carrying folders, view groups, hidden members, access policy, and AI context. OBSL exposes one global namespace, so consumers see every member unless the host filters them.
9. **Dimension hierarchies and drill paths** — `hierarchies` on cubes, surfaced through views for BI drill-down.
10. **Custom granularities and calendar cubes** — fiscal years, retail 4-5-4, weeks starting Sunday. OBSL's `TimeGrain` set is fixed.
11. **Multi-stage calculations** — a generic staged-aggregation surface (`group_by` / `reduce_by` / `add_group_by`) alongside the existing typed metrics.

### To match OBSL, Cube would need:

1. **First-class cumulative & period-over-period metric types** — declarative versions of what's currently `rolling_window` and query-side `time_shift`, including per-dimension `partitionBy` on cumulative.
2. **First-class window metric type** — `rank`, `dense_rank`, `row_number`, `ntile`, `lag`, `lead`, `first_value`, `last_value` as declarative metric types rather than reaching them through `type: number` + raw window-function SQL.
3. **First-class statistical / regression aggregations** — `stddev`, `variance`, `corr`, `covar_*`, `regr_slope`, `regr_intercept` as declarative measure types with arity validation and per-dialect gating, rather than via `type: number` + raw SQL.
4. ~~Multi-rooted DAG modeling~~ — **closed by Cube v1.7's multi-fact views.** What remains is narrower: multi-fact views require every fact cube to declare a *direct* join to each shared dimension at a root-level join path, where CFL anchors each leg on a common root found by graph search, so intermediate hops are fine and no view has to be authored up front.
5. **Named secondary join paths** with per-query selection — still no Cube primitive; the planner resolves by Dijkstra + a member-type heuristic, and a `view`'s `join_path` pins the choice at model-design time rather than per query.
6. **RDF/SPARQL graph surface** for governance/lineage.
7. **Apache Arrow Flight SQL** as a columnar wire protocol option alongside Postgres wire — modern analytical payloads transfer more efficiently in Arrow.
8. **First-class DB-API 2.0 drivers** for direct programmatic access from Python (PEP 249).
9. **Interactive RDF/ontology graph visualization** in the playground (vis-network-style) — Cube Playground's schema browser is functional but not graph-shaped.
10. **OSI ↔ Cube model round-trip** for portability between semantic layers.

---

## References

- OBSL `MetricType` enum: `src/orionbelt/models/semantic.py`
- OBSL CFL planner: `src/orionbelt/compiler/cfl.py`
- OBSL fanout detection: `src/orionbelt/compiler/fanout.py`
- OBSL docs: [Model Format](../guide/model-format.md), [Period-over-Period Metrics](../guide/period-over-period.md), [Compilation Pipeline](../guide/compilation.md)
- OBSL grain dedup: `src/orionbelt/compiler/grain_dedup.py`, `src/orionbelt/compiler/metric_expansion.py`
- Cube docs: https://cube.dev/docs
- Cube data modeling reference: https://cube.dev/docs/product/data-modeling/reference
- Cube multi-fact views: https://docs.cube.dev/docs/data-modeling/multi-fact-views
- Cube hierarchies: https://cube.dev/docs/product/data-modeling/reference/hierarchies
- Cube calendar cubes: https://cube.dev/docs/product/data-modeling/concepts/calendar-cubes
- Cube data access policies: https://cube.dev/docs/reference/data-modeling/data-access-policies
- Cube pre-aggregations: https://cube.dev/docs/product/caching/using-pre-aggregations
- Cube SQL API: https://cube.dev/docs/product/apis-integrations/sql-api
- Cube Core v1.7 (Tesseract GA, 2026-07-08): https://cube.dev/blog/cube-core-v1-7-tesseract-ga-data-modeling-performance
