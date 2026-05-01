# Comparison with Other Semantic Layers

How OrionBelt Semantic Layer (OBSL) stacks up against the leading semantic layer / metrics tools. These pages are honest, two-sided comparisons including gap analyses in both directions — useful when evaluating which tool fits your stack.

## At a glance

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
| Multi-rooted DAG | ✅ via CFL | Implicit | Workaround | One-explore-per-fact | Workaround via `view`s | Cube-rooted |
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
| Row-level security in model | ❌ | Via dbt | ❌ | ✅ | ✅ (`query_rewrite`) | ✅ enterprise |
| Multi-tenancy primitives | Sessions only | Cloud-managed | ❌ | ❌ | ✅ first-class | ✅ enterprise |
| OSI interoperability | ✅ converter | ❌ | ❌ | ❌ | ❌ | ✅ founding contributor |

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
