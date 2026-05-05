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
| Multi-rooted modeling (peer-rooted facts) | ✅ independent `dataObject`s | ✅ independent `semantic_model`s | ❌ single-rooted `source:` | ❌ single-rooted `explore:` (joined facts only) | ✅ independent cubes | ✅ multiple facts in one Cube via conformed dims |
| Multi-fact query plan | `UNION ALL` legs (CFL) | `FULL OUTER JOIN` on shared entities | n/a | JOIN inside explore (symmetric agg) | Single JOIN path via Dijkstra-resolved cube graph | JOIN with OLAP aggregation |
| Multi-path joins (between same pair) | ✅ per-query selection via `pathName` + `usePathNames` | ❌ no path-name primitive | ❌ aliased sources (model-time) | `from:` aliasing (model-time) | Dijkstra + member-type priority heuristic; pin via `view`s | Role-playing dimensions (model-time) |
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

Two separate questions matter here, and they cut differently across tools:

1. **Modeling-time**: does the language let you declare independent peer entities (multiple facts) with their own joins to shared dimensions, or are joins scoped to a single base context (one explore, one source) so peer-rooted topology can't be expressed at all?
2. **Query-time**: when a query asks for measures from multiple facts, is the SQL plan a single `JOIN` graph (with fanout risk, mitigated by symmetric aggregates) or `UNION ALL` legs?

A constraint that's universal: every tool here requires you to **declare entities (tables/cubes/sources/dataObjects) first** — you can't ad-hoc add a join to an undefined table. The differences are about how those declared entities can be wired together.

| Tool | Peer-rooted modeling | Multi-fact query plan |
|---|---|---|
| **OBSL** | ✅ `dataObject`s are independent peers, each with its own joins | `UNION ALL` legs (CFL) with NULL-padding for missing measures |
| **dbt SL (MetricFlow)** | ✅ `semantic_model`s are independent peers; joins via shared entities | `FULL OUTER JOIN` on the shared entity (per dbt's `join-logic.md`) |
| **Malloy** | ❌ every `source:` is single-rooted; `join_one` / `join_many` always presume one root | n/a (separate queries) |
| **LookML** | ❌ joins live inside one `explore` (one base view + joined views) | JOIN inside the explore (symmetric agg) |
| **Looker (runtime)** | n/a | `merged_results` merges two queries' results in the API layer |
| **Cube** | ✅ cubes are independent peers with `joins:` blocks; planner traverses the cube graph (Dijkstra) | Single JOIN path resolved at query time; `view`s recommended when multiple paths exist |
| **AtScale** | ✅ multiple fact datasets in one Cube connected by conformed dimensions; virtual cubes compose Cubes | JOIN with OLAP-style aggregation |

So **peer-rooted modeling** isn't unique to OBSL — Cube, dbt MetricFlow, and AtScale all support it. **Single-rooted-only** sits with Malloy and LookML, by language design.

The **multi-fact query plans split three ways**:

- **`UNION ALL` (OBSL)** — one leg per fact; each leg joins only its own fact to its dim path; no fanout risk, no symmetric-aggregate accounting needed.
- **`FULL OUTER JOIN` on shared entity (dbt MetricFlow)** — peer fact tables joined on the entity; rows preserved when either side has data. Cleaner than a single-rooted LEFT JOIN graph; semantically close to UNION ALL but in a single SQL pass.
- **Single JOIN path (Cube, LookML, AtScale)** — query planner picks one path through the join graph (Dijkstra in Cube; explore-rooted in LookML; OLAP aggregation in AtScale) and uses symmetric aggregates to handle fanout.

For **multi-path joins** between the same pair of entities (e.g., `Order.ship_address_id` vs `Order.bill_address_id` both pointing to `Address`):

- **OBSL**: declare both with `secondary: true` + `pathName`; pick at query time via `usePathNames`. **Per-query selection — unique here.**
- **Cube**: graph allows multiple paths; planner resolves with Dijkstra + a member-type priority heuristic (measures → dimensions → segments → time dimensions); pin via a `view` for predictability. **Graph-aware but not per-query selectable** — and the choice of *shortest path* is a graph-theoretic answer to what is fundamentally a semantic question. When a query asks for "orders by address," shortest-path picks one of `ship_address_id` / `bill_address_id` based on edge weights, not on the consumer's intent. The two paths may yield genuinely different correct answers; Dijkstra picks the one the heuristic prefers. Views can pin the choice, but only at model-design time, not per query.
- **AtScale**: role-playing dimensions — same dim aliased into multiple roles (`OrderDate`, `ShipDate`). Model-time aliasing.
- **LookML**: `from:` aliasing — same view declared twice in an explore under different aliases. Model-time.
- **Malloy**: aliased sources (`source: ship_addr is address`). Model-time.
- **dbt MetricFlow**: no first-class path-name primitive.

**What's structurally distinct about OBSL** after the corrections:

1. **`UNION ALL` query plan** for multi-fact — closest peer is MetricFlow's `FULL OUTER JOIN` (similar peer-symmetric semantics, different mechanism); everyone else uses a single JOIN graph.
2. **Per-query path selection** for multi-path joins — Cube has graph-aware multi-path resolution (heuristic), but OBSL is the only one where the *consumer of the query* picks the path explicitly.
3. **Static fanout detection** as an explicit error class (`compiler/fanout.py` raises `FanoutError`) instead of relying on symmetric aggregates to silently correct.

AtScale's conformed-dimensions-within-a-Cube + virtual cubes is the closest peer in *modeling capability*; dbt MetricFlow's `FULL OUTER JOIN` is the closest peer in *query plan*. OBSL's combination of UNION-ALL plan + per-query path selection is what's unique.

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
