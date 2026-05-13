# OBSL vs LookML / Looker

A feature comparison between **OrionBelt Semantic Layer (OBSL)** and **LookML**, the modeling language behind Google Cloud's **Looker** BI platform. Captured 2026-05-01.

---

## TL;DR

- **LookML wins on**: deep BI integration (drill fields, parameters, Liquid templating, PDTs, access filters/grants), `dimension_group` auto-timeframe expansion, symmetric aggregates (Looker invented the term), the broadest warehouse connector portfolio, and a polished proprietary IDE/UI.
- **OBSL wins on**: being **open-source and self-hostable** (LookML is Looker-only — proprietary, paid, vendor-locked), a **language-agnostic JSON Query API** consumable by any client, **richer modeling topologies** (multi-rooted DAG with first-class named secondary join paths) where LookML's explore is a single-rooted tree, **first-class cumulative and period-over-period metric types** (LookML expresses these via table calculations or filtered measures, not declarative metric types), an **RDF/SPARQL graph view** of the model, an explicit **CFL multi-fact planner**, and an MCP server for LLM/agent integration.
- **Different niches**: LookML is "the modeling language for Looker, your BI platform." OBSL is "an embeddable semantic compiler that exposes metrics over a stable API to apps, agents, and BI tools you didn't have to buy."

LookML and Looker are inseparable in practice — you cannot run LookML without Looker. So this is also a comparison of build-vs-buy on the runtime.

---

## 1. Modeling philosophy

| Aspect | OBSL (OBML) | LookML |
|---|---|---|
| Format | Declarative YAML (`OBML`) | Domain-specific declarative language (`.lkml` files) |
| Source of truth | YAML model files | LookML files, hosted in a Looker project (Git-backed) |
| Top-level constructs | `dataObjects`, `dimensions`, `measures`, `metrics`, `filters` | `connection`, `model`, `view`, `explore`, `dimension`, `dimension_group`, `measure`, `parameter`, `filter`, `derived_table`, `access_filter`, `access_grant` |
| Object scoping | Each `DataObject` has `columns:`; dimensions/measures/metrics live at model scope | Dimensions and measures are *inside* `view`s; joins live in `explore`s in `model` files |
| Runtime | OSS, self-hosted | **Looker proprietary platform only** (Google Cloud) |

---

## 2. Concept mapping

| OBSL | LookML | Notes |
|---|---|---|
| `DataObject` | `view` | Both wrap a physical table/SQL block |
| `DataObject.columns` | `dimension` declarations on a view | |
| `Dimension` (model-scoped) | n/a — dimensions live on views | LookML has no model-scoped dimensions; views are the unit of definition |
| `Measure` | `measure` (with `type: sum`, `count`, `count_distinct`, `avg`, `running_total`, etc.) | |
| `Metric` `type: derived` | `measure: { type: number; sql: ... ;; }` referencing other measures | |
| `Metric` `type: cumulative` | `measure: { type: running_total }` or table calculations in UI | LookML has narrow built-in support; richer cumulative is UI-side |
| `Metric` `type: period_over_period` | Liquid + filtered measures + table calculations | No first-class metric type |
| `DataObjectJoin` | `explore` `join:` blocks with `relationship: many_to_one` etc. | LookML's `relationship:` drives symmetric aggregates |
| `secondary: true` + `pathName` | Multiple aliased joins (`from:` clause + alternate names) | No first-class path naming |
| `QueryObject` JSON | Looker query (built via UI or sent via API; LookML files are not query targets directly) | |
| OBSL session-scoped REST | Looker API + SDK | |

---

## 3. The headline LookML features

### 3.1 Symmetric aggregates (the original)

Looker pioneered symmetric aggregates as a productionized concept. The `relationship:` keyword on each join (`one_to_one`, `many_to_one`, `one_to_many`, `many_to_many`) tells Looker how to wrap aggregates so joins can fan out without double-counting. OBSL uses a different approach: static fanout detection + the **CFL** planner emitting `UNION ALL` legs.

| | LookML | OBSL |
|---|---|---|
| Strategy | Symmetric aggregates driven by `relationship:` | Static fanout detection + CFL `UNION ALL` planner |
| User experience | Implicit, "it just works" if you set `relationship` correctly | Explicit error/plan; CFL output is inspectable |
| Failure mode | Wrong `relationship` → silently wrong numbers | Wrong join model → `FanoutError` raised at compile |

### 3.2 `dimension_group` (time auto-expansion)

A single LookML declaration generates many time-grain dimensions:

```lookml
dimension_group: created {
  type: time
  timeframes: [raw, date, week, month, quarter, year, day_of_week]
  sql: ${TABLE}.created_at ;;
}
```

That produces `created_raw`, `created_date`, `created_week`, etc. as separate dimensions, each ready to group by.

**OBSL**: a single `Dimension` carries a `timeGrain` enum, and queries pass the grain at query time. More compact in the model, less ergonomic from a "list everything available" UI perspective.

### 3.3 Liquid templating + parameters

LookML embeds Liquid (Shopify's template language) for dynamic SQL and UI logic. Combined with `parameter:`, this drives dynamic dimensions, swappable measures, and personalization on user attributes.

```lookml
parameter: metric_selector {
  type: unquoted
  allowed_value: { value: "revenue" }
  allowed_value: { value: "profit" }
}

measure: selected_metric {
  type: sum
  sql: {% if metric_selector._parameter_value == 'revenue' %}
         ${TABLE}.revenue
       {% else %}
         ${TABLE}.profit
       {% endif %} ;;
}
```

**OBSL** has no equivalent. The model is static YAML — runtime swapping is handled by the consumer composing different queries, or by maintaining multiple measure definitions.

### 3.4 PDTs / NDTs (materialization)

Looker can materialize derived tables into the warehouse on a schedule — **PDTs** (Persistent Derived Tables) for SQL-defined datasets, **NDTs** (Native Derived Tables) for derived-from-explore datasets. Looker manages the build, dependency graph, and refresh cadence.

**OBSL** has no materialization story. It's a query-time compiler; "make this faster" is the warehouse's or upstream tool's job (e.g., dbt models, scheduled jobs, materialized views).

### 3.5 Access filters and access grants

LookML has fine-grained row-level security baked into the model: `access_filter: { field: ...; user_attribute: ... }` injects a WHERE based on the logged-in user, and `required_access_grants:` gates fields/measures by role.

**OBSL** has no first-class user/auth model. Authn/authz is the host application's job. (For the public demo, no auth by design.)

### 3.6 Drill fields and visualization metadata

LookML carries UI metadata: `drill_fields: [order_id, customer_name, ...]`, `value_format`, `html`, `link`, label/group_label, etc. — Looker uses these to render a coherent BI experience.

**OBSL** carries minimal UI metadata. Rendering is the consumer's job.

---

## 4. Metric types

| OBSL | LookML | Notes |
|---|---|---|
| `Measure` (sum/avg/count/min/max, `total: bool`) | `measure: { type: sum/avg/count/count_distinct/... }` | LookML has more aggregate types out of the box (`sum_distinct`, `percentile`, `median`, etc.) |
| `Metric` `type: derived` | `measure: { type: number; sql: ...references other measures... }` | Both first-class |
| `Metric` `type: cumulative` (running, rolling, grain-to-date) | `type: running_total` (basic), or table calculations in UI for richer cases | OBSL has richer **declarative** cumulative |
| `Metric` `type: period_over_period` (4 comparison modes) | Patterns: filtered measures (`{% if date.is_in_period %} ... {% endif %}`) or table calculations | OBSL has a dedicated metric type |
| `Measure.filterContext` / `filteredMeasures` | Filtered measure pattern: `measure: { type: sum; filters: [is_paid: "yes"] }` | Comparable |

Bottom line: LookML's **breadth of aggregate types** is wider; OBSL's **time-aware metric types** (cumulative/PoP) are more declarative and reusable. Looker-style PoP usually ends up as a *table calculation* in the UI — not portable to other consumers.

---

## 5. Joins

| | OBSL | LookML |
|---|---|---|
| Definition site | `joins:` array on `DataObject` (model-level) | `join:` blocks inside an `explore` (explore-level) |
| Cardinality | `joinType`: `many-to-one`, `one-to-one`, `many-to-many` | `relationship`: `one_to_one`, `many_to_one`, `one_to_many`, `many_to_many` |
| What cardinality drives | Static fanout detection + CFL multi-fact planning | Symmetric aggregates |
| Join condition | `columnsFrom`/`columnsTo` arrays | `sql_on: ${a.id} = ${b.a_id} ;;` (free-form SQL) |
| Multiple paths | First-class via `secondary: true` + named `pathName`, query-time selection via `usePathNames` | Multiple aliased joins via `from:` keyword + different names — no path naming primitive |
| Multiple "starting points" | Each query picks a base data object | Each `explore` is a separate starting point with its own join tree |

LookML's `sql_on:` is more flexible (any SQL); OBSL's column lists are more constrained but easier to validate and reason about programmatically.

## 6. Data modeling topology (a major differentiator)

LookML `explore`s are **single-rooted trees**: each explore has one base view and joins fan out from it. To query against two different fact tables you typically build two explores, or design carefully around a shared dimension. Multi-path scenarios (two valid joins between the same view pair) require duplicating the join with a `from:` alias, and there is no first-class "name this path and pick it per query" primitive — the consumer has to know which alias to use.

OBSL is built on a **directed join graph (DAG)** with explicit support for richer topologies:

| Topology | Star (single fact + dims) | Snowflake (chained dims) | Multi-rooted (multiple facts) | Multi-path (alt. joins between same pair) | Cycles |
|---|---|---|---|---|---|
| **OBSL** | ✅ | ✅ | ✅ via CFL `UNION ALL` legs with per-leg common root | ✅ first-class via `secondary: true` + `pathName` + per-query `usePathNames` | Detected and rejected |
| **LookML** | ✅ | ✅ | One explore per fact; no in-explore multi-fact union | Workaround: `from:` aliasing; no path-name primitive | Implicit |

**Why this matters**: Looker's "explore-per-fact" pattern works well when the org's analytics are organized around a few well-curated explores. It works less well when you want a single semantic surface that an embedded app or agent can hit and ask "give me revenue *and* support tickets by customer this month," or when the same dimension table is joined by different keys in different contexts. OBSL's named secondary paths and CFL planner make those messy real-world topologies first-class rather than something to design around.

---

## 7. Dialect / warehouse support

LookML connects via Looker's connector library. Looker supports a very broad list including all the OBSL dialects plus many more (Redshift, Athena, Vertica, Teradata, Oracle, SQL Server, Spark SQL, etc.).

| Dialect | OBSL | Looker |
|---|---|---|
| BigQuery | ✅ | ✅ |
| Snowflake | ✅ | ✅ |
| Postgres | ✅ | ✅ |
| MySQL | ✅ | ✅ |
| DuckDB | ✅ | ❌ (not a typical Looker target) |
| Databricks | ✅ | ✅ |
| ClickHouse | ✅ | ✅ |
| Dremio | ✅ | ✅ |
| Redshift | ❌ | ✅ |
| Athena / Trino / Presto | ❌ | ✅ |
| SQL Server / Oracle / Teradata | ❌ | ✅ |

Looker is broader on enterprise legacy databases; OBSL is competitive on modern cloud warehouses and uniquely supports DuckDB out of the box.

---

## 8. APIs / interfaces

| | OBSL | LookML / Looker |
|---|---|---|
| Natural SQL surface | **OrionBelt Semantic QL (OBSQL)** — `SELECT "dim", "measure" FROM <model>` (or no FROM); `MEASURE()` marker; aggregate-wrap matching; `WITH ROLLUP` / `WITH CUBE` first-class. Routes BI-tool SQL through the same compiler as the JSON API. | Looker has a "SQL Runner" that runs raw warehouse SQL bypassing LookML — useful for inspection but not governed by the semantic layer. The semantic surface itself is reached only through the Looker UI or Looker API. |
| Catalog discovery from BI tools | `SHOW TABLES`, `DESCRIBE`, `information_schema.*`, `pg_catalog.*` answered from the model in-process — BI tools browse the catalog without warehouse round-trips | n/a (Looker is the BI tool — no catalog endpoint to expose) |
| Governance | **Closed by design** — raw warehouse SQL and DDL/DML always reject (`RAW_SQL_REJECTED` / `WRITE_OPERATION_REJECTED`). No env flag to bypass. | SQL Runner is intentionally a hole; access grants gate the *Looker* surface, not raw SQL |
| REST API | Yes — first-party FastAPI service | Yes — Looker API (rich, mature, but proprietary) |
| Arrow Flight SQL | Yes — gRPC server on port 8815 for BI tool connectivity (DBeaver, Tableau, Power BI via Arrow Flight SQL JDBC/ODBC). Multi-model addressing via the `database` gRPC header. | No (Looker is the BI front-end itself) |
| JDBC | Yes — via Arrow Flight SQL JDBC driver | n/a (Looker is the BI tool) |
| DB-API 2.0 drivers | Yes — 8 drivers shipped | No |
| MCP | Yes — first-party server | Not native; community efforts exist |
| GraphQL | No | No |
| Native SDK | Python (FastAPI client) | Python, Ruby, TypeScript, Java, etc. (`looker-sdk`) |
| UI / Playground | Interactive Gradio playground: SQL Compiler, Query Results table, auto-generated Mermaid ER diagrams, interactive RDF/OBSL ontology graph (vis-network), OSI import/export, settings panel | Looker IDE (very polished) + Looker Explore + dashboards |
| RDF graph + SPARQL | Yes (`/graph`, `/sparql`) | No |
| Format conversion | OSI ↔ OBML (`/convert/*`) | n/a |
| Visualization | Not built-in | First-class: tables, charts, dashboards, alerts |

---

## 9. Open source vs. proprietary

This is the most consequential difference and worth calling out separately.

| | OBSL | LookML |
|---|---|---|
| License | Open source | Proprietary (Google Cloud / Looker) |
| Self-hostable | Yes — runs anywhere Python runs | Limited: Looker is a hosted SaaS; "Looker (original)" had a self-hosted option but is being deprecated |
| Cost | Free | Per-user licensing on the Looker platform |
| Vendor lock-in | None | LookML files only run inside Looker |
| Air-gapped deploy | Possible | Not supported |
| Format portability | OBML is plain YAML, can be read by any tool | LookML is a Looker-specific DSL with no widely-adopted external parser other than `pylookml` |

For embedded SaaS, multi-tenant analytics, or air-gapped/on-prem use cases, OBSL is the only option of the two.

---

## 10. Other distinctives

| Feature | OBSL | LookML |
|---|---|---|
| `dimension_group` time auto-expansion | ❌ (single dim + grain at query time) | ✅ |
| Liquid templating | ❌ | ✅ |
| `parameter:` runtime knobs | ❌ | ✅ |
| Persistent Derived Tables (PDTs) | ❌ (no materialization) | ✅ |
| Access filters / access grants | ❌ | ✅ |
| Drill fields / UI metadata | Minimal | ✅ |
| Symmetric aggregates | ❌ (uses CFL instead) | ✅ |
| First-class PoP metric type | ✅ | ❌ (table calc / filtered measures) |
| First-class cumulative metric type | ✅ | Partial (`running_total` only) |
| RDF/SPARQL graph view | ✅ | ❌ |
| Named secondary join paths | ✅ | ❌ |
| Explicit CFL multi-fact planner | ✅ | n/a |
| MCP server (LLM/agent) | ✅ | ❌ (not native) |
| OSS / self-hostable | ✅ | ❌ |
| Built-in BI front-end | ❌ | ✅ |

---

## 11. When to pick which

### Pick **Looker / LookML** when:

- You're already buying Looker, or your org needs an end-to-end BI platform (modeling + dashboards + alerts + governance).
- You need **fine-grained row/field-level security** baked into the model (`access_filter`, `access_grant`).
- You need **PDT/NDT materialization** managed by the modeling layer.
- You need **drill paths** and visualization metadata to drive a coherent BI UI.
- You're running on enterprise legacy warehouses (Redshift, Oracle, Teradata, Vertica).
- Your team is happy living inside the Looker IDE.

### Pick **OBSL** when:

- You need an **open-source, self-hostable, embeddable** semantic layer — no vendor lock-in.
- Your consumers are **applications, agents, or LLMs** — a stable JSON Query API beats requiring callers to know LookML.
- You need first-class, *reusable* **cumulative** and **period-over-period** metric types instead of expressing them as table calculations.
- You target ClickHouse, Databricks, Dremio, or DuckDB.
- You want a **graph view of the model** (RDF/SPARQL) for governance/lineage tooling.
- You need **multi-tenant** semantic models (sessions, TTL) without provisioning a Looker instance per tenant.
- Cost matters and per-user Looker licensing isn't justifiable for your use case.

### They could coexist

A common hybrid: ship Looker for the human BI audience and run OBSL alongside it for the embedded / API / agent audience. The models are expressed twice (different formats), but neither is a strict superset of the other. OSI conversion may help if you're going from another semantic format into OBSL.

---

## 12. Gap analysis

### To match LookML, OBSL would need:

1. **`dimension_group`-style time auto-expansion** — a single dimension declaration that surfaces multiple grain variants in introspection / autocomplete.
2. **Templating / parameters** — a way to inject runtime values into measure SQL or pick between expressions (Liquid-equivalent or simpler conditionals).
3. **Materialization** — first-class PDT-style derived table support, or at least integration hooks to a materialization tool.
4. **Row-level security primitives** — access filters / grants tied to a session-level user attribute.
5. **Symmetric aggregates** as an alternative to (or alongside) CFL — let users pick the strategy.
6. **Drill paths and UI metadata** — `drill_fields:`, `value_format:`, etc., to enable consumer UIs to render coherently.
7. **More dialect coverage** for legacy enterprise databases (Redshift, Trino/Presto, SQL Server) if those markets matter.

### To match OBSL, LookML/Looker would need:

1. **Open-source / self-hostable runtime** (the structural blocker).
2. **First-class cumulative & period-over-period metric types** — declarative versions of what's currently table calculations.
3. **Named secondary join paths** with per-query selection.
4. **RDF/SPARQL graph surface** for governance/lineage.
5. **Native MCP server** for LLM/agent consumers.
6. **OSI ↔ model round-trip** for portability.

---

## References

- OBSL `MetricType` enum: `src/orionbelt/models/semantic.py`
- OBSL CFL planner: `src/orionbelt/compiler/cfl.py`
- OBSL fanout detection: `src/orionbelt/compiler/fanout.py`
- OBSL docs: [Model Format](../guide/model-format.md), [Period-over-Period Metrics](../guide/period-over-period.md), [Compilation Pipeline](../guide/compilation.md)
- LookML docs: https://cloud.google.com/looker/docs/what-is-lookml
- Looker API: https://cloud.google.com/looker/docs/api-and-integration
- pylookml (programmatic LookML generation): https://github.com/looker-open-source/pylookml
