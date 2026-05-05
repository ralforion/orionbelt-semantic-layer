# Comparison with Other Semantic Layers

How OrionBelt Semantic Layer (OBSL) stacks up against the leading semantic layer / metrics tools. These pages are honest, two-sided comparisons including gap analyses in both directions — useful when evaluating which tool fits your stack.

## At a glance

<div class="compact-table" markdown>

| | OBSL | dbt SL | Malloy | LookML | Cube | AtScale |
|---|---|---|---|---|---|---|
| License | Source-available (BSL) | Definitions OSS; runtime in dbt Cloud | Open source | Proprietary | Apache 2.0 (core) + Cube Cloud | Proprietary; **free Developer Community Edition** for non-prod |
| Self-hostable | ✅ | Definitions yes, runtime no | ✅ | ❌ | ✅ | ✅ (licensed) |
| Standalone (no transformation tool dep.) | ✅ | ❌ requires dbt | ✅ | ✅ | ✅ | ✅ |
| Format | YAML (`OBML`) | YAML on dbt models | DSL (`.malloy`) | DSL (`.lkml`) | YAML / JS + Twig | Visual designer |
| Query interface | REST + **Arrow Flight SQL** + DB-API | GraphQL/JDBC (Cloud) | Malloy language | Looker UI / API | REST + GraphQL + **Postgres-wire SQL** | **MDX + DAX** + JDBC/ODBC + REST |
| First-class cumulative metric | ✅ | ✅ | Per-query | Partial | Partial (`rolling_window`) | Via MDX |
| First-class period-over-period metric | ✅ | Via `offset_window` | Per-query | Via table calc | Query-time `time_shift` | Via MDX |
| Conversion / funnel metrics | ❌ | ✅ | Patterns | Patterns | Patterns | Patterns |
| Symmetric aggregates | ❌ (uses CFL) | ❌ | ✅ | ✅ | ✅ | ✅ (OLAP) |
| Multi-rooted DAG | ✅ via CFL `UNION ALL` planner | Implicit via shared entities (no union planner) | Aliased / joined sources (no union planner) | Explore-per-fact + Looker `merged_results` (API-side merge) | `view`s stitching joined cubes (model-time) | Per-Cube fact base; multi-cube via conformed dims or virtual cubes |
| Named secondary join paths | ✅ first-class | ❌ | ❌ | ❌ | ❌ | ✅ (role-playing dims) |
| Nested / hierarchical results | ❌ | ❌ | ✅ (`nest:`) | ❌ | ❌ | ❌ |
| OLAP hierarchies (multi-level, parent-child) | ❌ | ❌ | ❌ | Partial | ❌ | ✅ first-class |
| MDX / Excel pivot tables | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ unique |
| RDF/SPARQL graph view | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| MCP server | ✅ | ✅ (dbt-mcp) | ✅ (Publisher) | ❌ | ✅ | Limited |
| Interactive playground / UI | ✅ Gradio (incl. RDF ontology graph) | dbt Cloud Studio (paid) | VS Code + Publisher | ✅ Looker IDE | Cube Playground / Studio | ✅ Design Center |
| Notebook authoring (VS Code / Colab) | ✅ `quickstart.ipynb` runs natively in VS Code or Colab | Via dbt-cli in any notebook | Notebook tutorials | ❌ | ❌ | ❌ |
| Built-in BI dashboards | ❌ | ❌ | VS Code | ✅ Looker | ❌ | Via MDX in Tableau/Excel |
| Pre-aggregations / materialization | ❌ | Via dbt models | ❌ | ✅ (PDTs) | ✅ flagship | ✅ autonomous |
| Result cache | ✅ freshness-driven file cache (TTL from `dataObject.refresh:`; ETL heartbeat invalidation) | dbt Cloud query cache | ❌ | Looker query cache (`persist_for`) + aggregate awareness | Tiered (in-memory + optional Redis + Cube Store) with refresh-key TTL | Built-in cache + autonomous aggregates |
| Row-level security in model | ❌ | Via dbt | ❌ | ✅ | ✅ (`query_rewrite`) | ✅ enterprise |
| Multi-tenancy primitives | Sessions only | Cloud-managed | ❌ | ❌ | ✅ first-class | ✅ enterprise |
| OSI interoperability | ✅ converter | ❌ | ❌ | ❌ | ❌ | ✅ founding contributor |

</div>

## Detailed comparisons

- [vs. dbt Semantic Layer (MetricFlow)](dbt.md) — coupled to dbt projects, served via dbt Cloud
- [vs. Malloy](malloy.md) — a query language with semantic modeling, plus the Publisher REST/MCP server
- [vs. LookML / Looker](lookml.md) — the proprietary modeling language behind Google Cloud Looker
- [vs. Cube](cube.md) — the OSS production semantic layer with pre-aggregations, multi-API parity, and a Postgres-wire SQL surface
- [vs. AtScale](atscale.md) — the enterprise universal semantic layer with native MDX/DAX for Excel and Power BI live connections

## Topology: a recurring theme

Most semantic layers assume a **single-rooted, tree-shaped** model (one fact at the center, dimensions fanning out). OBSL is built on a **directed join graph (DAG)** that supports:

- **Star** and **snowflake** schemas (the common cases)
- **Multi-rooted** models — query across multiple unrelated facts in a single semantic surface, resolved via the **CFL (Composite Fact Layer)** planner that emits `UNION ALL` legs
- **Multi-path** joins — multiple valid join paths between the same pair of objects, named via `pathName` and selected per query via `usePathNames`
- **Cycle detection** — explicit, not silent

This matters when your warehouse doesn't fit a clean star: you need ship-address vs. billing-address joins to the same dimension, or a single API surface that exposes revenue *and* support tickets together. See the [Compilation Pipeline](../guide/compilation.md) guide for how this flows through the planner.

### How each tool actually handles multi-fact queries

The at-a-glance row is necessarily terse. The substance:

- **OBSL** — `compiler/cfl.py` (the **Composite Fact Layer** planner) emits one `UNION ALL` leg per fact, with NULL padding for measures that don't apply to a leg. Multi-fact is a first-class query-time primitive; the planner detects when measures span genuinely independent facts (via directed reachability on the join graph) and routes only those queries through CFL.
- **dbt SL (MetricFlow)** — each `semantic_model` declares one primary entity. When a query references metrics from two semantic models, MetricFlow attempts to find a shared entity and joins through it. Works when entities line up; fails (or silently produces wrong rows) when they don't. **No explicit multi-fact UNION planner.**
- **Malloy** — every `source:` is single-rooted by language design (one underlying table or one parent source, extended via `join_one` / `join_many`). There's **no syntactic primitive for a multi-rooted source**: `join_one` / `join_many` are extensions of one source's tree, so they always presume a single root. Attempting to declare two unrelated facts as peer roots in one source fails at the join definitions — there is no peer-of-peer join construct. Multi-fact reporting therefore means separate queries or query-layer stitching; symmetric aggregates make the single-rooted tree query-safe but don't compose multiple roots.
- **LookML / Looker** — each `explore` is single-rooted (one base view + joins fanning out). Multi-fact reporting builds two explores. Looker (the *runtime*, not LookML the language) provides **`merged_results`** which executes two queries and merges their results in the API layer — useful but post-join, not a model-time primitive.
- **Cube** — `view`s stitch measures/dimensions from joined cubes into a unified surface. This is the intended pattern (not a workaround) but it's **predesigned at model time**: you decide which cubes a view exposes upfront. Query-time choice between two valid join paths between the same cube pair isn't a first-class primitive.
- **AtScale** — each AtScale **Cube** (its term for a data model) has a single fact base. Multi-fact via (a) multiple cubes, (b) one cube joining facts via **conformed dimensions**, or (c) **virtual cubes** that combine cubes. Multi-path joins are handled via **role-playing dimensions** — elegant but model-time, not the per-query `pathName` selection OBSL provides.

The structural difference: OBSL plans multi-fact at *query time* via a graph reachability check + UNION ALL; the others either restrict to single-rooted (Malloy, LookML) or push multi-fact resolution upstream into model design (Cube views, AtScale virtual cubes, dbt entity alignment).

## Where OBSL fits best

- **Embedded analytics** in a SaaS product where consumers (apps, agents, BI tools) need a stable JSON Query API and you don't want to ship a DSL interpreter.
- **Multi-tenant** semantic models with TTL-scoped sessions.
- **LLM/agent integration** via MCP — a clean, schema-driven query surface beats teaching the agent a new language.
- **Modern cloud warehouses** including ClickHouse, Databricks, Dremio, and DuckDB.
- **Open-source / self-hostable / air-gapped** deployments.

## Where another tool may be a better fit

- **dbt SL** if you've standardized on dbt and want metrics tightly coupled to your transformation pipeline, with dbt Cloud governance.
- **Malloy** for analyst-driven exploration and BI authoring, especially if you need hierarchical (`nest:`) result shapes.
- **Looker** if you're buying an end-to-end BI platform with dashboards, alerts, RLS, and PDTs — and the per-user licensing fits your org.
- **Cube** if you need pre-aggregations for sub-second analytics on large datasets, a Postgres-wire SQL API for BI-tool connectivity, or first-class multi-tenancy/RLS — and you're willing to operate (or pay for Cube Cloud to operate) the heavier runtime.
- **AtScale** if your business users live in Excel pivot tables and need native MDX, or you need DAX for Power BI live connections — no other tool in this comparison set speaks those protocols.

These tools are not mutually exclusive — it's plausible to ship a BI platform (Looker / AtScale) for the human audience and OBSL alongside it for the embedded / API / agent audience.

---

## About OSI

Several comparisons reference **OSI (Open Semantic Interchange)** — an open standard for portable semantic models, founded to let metric and dimension definitions move between BI tools, semantic layers, and data platforms without rewriting. See [open-semantic-interchange.org](https://open-semantic-interchange.org/) for the specification.

OBSL ships bidirectional converters (`POST /v1/convert/osi-to-obml`, `POST /v1/convert/obml-to-osi`) and Import/Export buttons in the Gradio playground. AtScale is a founding contributor to the OSI initiative; the other tools in this comparison do not currently support OSI directly. See the [OSI Interoperability](../guide/osi.md) guide for usage details.
