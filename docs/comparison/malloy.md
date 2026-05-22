# OBSL vs Malloy

A feature comparison between **OrionBelt Semantic Layer (OBSL)** and **Malloy** (the open-source data language and semantic modeling tool from the Malloy Data project). Captured 2026-05-01.

---

## TL;DR

- **Malloy wins on**: expressive query language (pipeline operator `->`, refinements, nesting), **symmetric aggregates** that handle fanout automatically, hierarchical query results via `nest:`, and a polished VS Code extension with autocomplete and inline visualizations (OBSL has a different VS Code path via its Jupyter notebook, but it's a Python-driven loop rather than a language-aware editor).
- **OBSL wins on**: more dialects (8 vs. ~6, with ClickHouse/Databricks/Dremio unique to OBSL), **richer modeling topologies** (multi-rooted DAG with first-class named secondary join paths) where Malloy assumes a single-rooted source tree, **first-class metric types** for cumulative and period-over-period (Malloy expresses these ad-hoc per query), an RDF/SPARQL graph view of the model, an explicit CFL planner, and a stable JSON Query API that's trivial for non-Malloy clients to call.
- **Different niches**: Malloy is "a new query language with semantic modeling baked in" — best for analyst-driven exploration and BI development. OBSL is "an API-first semantic compiler" — best for embedding into SaaS products and exposing metrics to LLMs/agents/external apps without teaching them a DSL.

---

## 1. Modeling philosophy

| Aspect | OBSL (OBML) | Malloy |
|---|---|---|
| Format | Declarative YAML (`OBML`) | A purpose-built **language** (`.malloy` files) — both modeling *and* querying |
| Source of truth | YAML model file | `.malloy` source files; `source: foo is duckdb.table('...') extend { ... }` |
| Top-level objects | `dataObjects`, `dimensions`, `measures`, `metrics`, `filters` | `source` (with extensions: `dimension:`, `measure:`, `view:`, `join_one:`, `join_many:`) |
| Queries | JSON `QueryObject` (`select`, `where`, `having`, `order_by`, ...) | Malloy query syntax: `run: source -> { group_by: ...; aggregate: ...; nest: ... }` |
| Embedding | Drop-in compiler, no client language needed | Requires a Malloy-aware client/parser |

**Key cultural difference**: Malloy is a *language*; OBSL is a *contract*. Malloy gives expressiveness in a `.malloy` file. OBSL gives a stable JSON over HTTP that any tool, agent, or LLM can call without learning a DSL.

---

## 2. Query model

### Malloy

```malloy
source: flights is duckdb.table('flights.parquet') extend {
  measure: flight_count is count()
  view: by_carrier is {
    group_by: carrier
    aggregate: flight_count
    limit: 10
  }
}

run: flights -> by_carrier + {
  where: distance > 1000
  nest: top_origins is {
    group_by: origin
    aggregate: flight_count
    limit: 3
  }
}
```

### OBSL

```yaml
# Model
metrics:
  - name: flight_count
    type: derived
    expr: "{[Flights].[Count]}"
```

```json
// Query
{
  "select": { "dimensions": ["Carrier"], "measures": ["flight_count"] },
  "where": [{ "field": "Distance", "op": ">", "value": 1000 }],
  "limit": 10
}
```

**Trade-off**: Malloy's pipeline-and-refinement syntax is more expressive and composable; OBSL's JSON is dumber but trivial for any client (curl, Python, an LLM tool call) to produce.

---

## 3. The headline Malloy features

These are genuinely distinctive and OBSL has no direct equivalent:

### 3.1 Symmetric aggregates (fanout safety, automatic)

Malloy uses *symmetric aggregates* — the engine emits SQL that prevents double-counting when joining one-to-many. You write `line_items.amount.sum()` and it Just Works regardless of how the join graph fans out.

OBSL takes a different route: it **detects** fanout statically (`compiler/fanout.py` raises `FanoutError`) and uses the **CFL planner** to emit `UNION ALL` legs across independent fact paths. Different mechanism, same goal: correctness on multi-fact queries.

| | Malloy | OBSL |
|---|---|---|
| Strategy | Symmetric aggregates (per-aggregate path qualification) | Static fanout detection + CFL `UNION ALL` planner |
| Visibility | Implicit, automatic | Explicit error/plan; CFL is inspectable |
| User experience | "It just works" | "Compiler tells you what it did" |

### 3.2 Nested queries (`nest:`)

Malloy returns hierarchical (tree-shaped) results in a single query — a parent group_by row contains a child query result inline. This is uniquely powerful for dashboard-style "row + drill-down" data.

```malloy
run: flights -> {
  group_by: origin
  aggregate: flight_count
  nest: top_carriers is { group_by: carrier; aggregate: flight_count; limit: 3 }
}
```

OBSL has **no equivalent**. OBSL queries return flat tabular result sets. To produce a parent/child shape you'd run multiple queries.

### 3.3 Refinements (`+ { ... }`)

Malloy lets you take a named view and add filters/limits/aggregates inline:

```malloy
run: flights -> by_carrier + { where: carrier = 'WN'; limit: 5 }
```

OBSL has no named-view-with-refinements concept. Queries are constructed fresh each call (though the Query API is small enough that programmatic composition is straightforward).

---

## 4. Metric types

| OBSL | Malloy | Notes |
|---|---|---|
| `Measure` (sum/avg/count/min/max, `total: bool` for grand totals) | `measure:` declarations inside `source` | Both first-class |
| `Metric` `type: derived` (`{[Measure A]}/{[Measure B]}`) | Composed by referencing other measures inside aggregate expressions | Both first-class |
| `Metric` `type: cumulative` (running, rolling, grain-to-date) | Express via **calculations** (window functions) inside queries | OBSL is declarative; Malloy is per-query |
| `Metric` `type: period_over_period` with 4 comparison modes | Pattern via `prior_period` style queries; renderer has a `big_value { comparison_field=... }` for visual deltas | OBSL has a dedicated metric type; Malloy treats it as "just write the query" |

**Different philosophies**: OBSL prefers reusable metric definitions (write once, every query gets PoP). Malloy prefers expressive ad-hoc queries (write the comparison in the query itself). Either fits depending on whether you're publishing a metrics catalog or empowering analysts.

---

## 5. Joins

| | OBSL | Malloy |
|---|---|---|
| Definition site | YAML `joins:` array on each `DataObject` | `join_one:`, `join_many:`, `join_cross:` inside `source extend { ... }` |
| Cardinality | `joinType`: `many-to-one`, `one-to-one`, `many-to-many` | Cardinality is part of the join keyword: `join_one`, `join_many`, `join_cross` |
| What cardinality drives | Static fanout detection + CFL multi-fact planning | Symmetric aggregate logic |
| Multiple paths between same tables | First-class via `secondary: true` + named `pathName`, selected per-query via `usePathNames: [{source, target, pathName}]` | Multiple `join_one`/`join_many` declarations with different aliases — no path-name primitive |
| Cycle / multi-path validation | Built into resolver | Compiler-level checks |

OBSL's named secondary paths are more explicit for ambiguous join graphs; Malloy's cardinality keywords are more elegant for the symmetric-aggregate runtime.

## 6. Data modeling topology (a major differentiator)

Malloy `source`s are **rooted at a single source** and extended outward via `join_one`/`join_many`. Conceptually that's a tree from the perspective of any one query — and Malloy's symmetric aggregates make that tree query-safe — but multi-rooted scenarios (querying across two unrelated facts in one go) and multi-path scenarios (two valid joins between the same pair of tables) are not first-class.

OBSL is built on a **directed join graph (DAG)** with explicit support for richer topologies:

| Topology | Star (single fact + dims) | Snowflake (chained dims) | Multi-rooted (multiple facts) | Multi-path (alt. joins between same pair) | Cycles |
|---|---|---|---|---|---|
| **OBSL** | ✅ | ✅ | ✅ via CFL `UNION ALL` legs with per-leg common root | ✅ first-class via `secondary: true` + `pathName` + per-query `usePathNames` | Detected and rejected |
| **Malloy** | ✅ | ✅ | Workaround: separate sources, join-as-source patterns; no explicit multi-fact union planner | Workaround: aliased sources; no path-name primitive | Implicit |

**Why this matters**: Real-world warehouses are messy. You routinely need a customer→order→order_item path *and* a customer→returns path queryable together, or to choose between "ship_address_id" and "billing_address_id" joins to the same address dimension per-query. Malloy expects you to denormalize or flatten upstream; OBSL lets you model the graph as-is and resolve at query time.

---

## 7. Dialects

| Dialect | OBSL | Malloy |
|---|---|---|
| BigQuery | ✅ | ✅ |
| Postgres | ✅ | ✅ |
| MySQL | ✅ | ✅ |
| DuckDB | ✅ | ✅ (native — Malloy's reference dialect) |
| Snowflake | ✅ | ✅ |
| Databricks | ✅ | ❌ |
| ClickHouse | ✅ | ❌ |
| Dremio | ✅ | ❌ |
| Trino / Presto | ❌ | ✅ |

**OBSL: 8 dialects, Malloy: ~6.** OBSL covers a wider modern-warehouse footprint (ClickHouse, Databricks, Dremio); Malloy adds Trino/Presto. Both projects have strong DuckDB stories.

---

## 8. APIs / interfaces

| | OBSL | Malloy |
|---|---|---|
| Query language | **OBSQL** (BI-style SQL: `SELECT "dim", "measure" FROM <model>` or no FROM) **+** native JSON `QueryObject` | **Malloy DSL** — language-defined query syntax (`run: source -> { group_by: x, aggregate: y }`) |
| Natural SQL surface | **OrionBelt Semantic QL (OBSQL)** through the same Flight SQL endpoint BI tools use; aggregate-wrap matching against declared aggregation; `WITH ROLLUP` / `WITH CUBE` first-class | No — Malloy is its own language; BI tools speak SQL, not Malloy |
| Catalog discovery from BI tools | `SHOW TABLES`, `DESCRIBE`, `information_schema.*`, `pg_catalog.*` answered from the model in-process | n/a (Malloy clients call the Malloy language) |
| Governance | **Closed by design** — `RAW_SQL_REJECTED` / `WRITE_OPERATION_REJECTED`. No env flag to bypass. | n/a |
| REST API | Yes — first-party FastAPI service in this repo | Yes — via the **Publisher** companion project (`malloydata/publisher`) |
| Arrow Flight SQL | Yes — gRPC server on port 8815; BI tools (DBeaver, Tableau, Power BI) connect via Arrow Flight SQL JDBC. Multi-model addressing via the `database` gRPC header. | No |
| JDBC | Yes — via Arrow Flight SQL JDBC driver | No |
| DB-API 2.0 drivers | Yes — 8 drivers shipped | No |
| MCP | Yes — first-party server | Yes — via Publisher |
| GraphQL | No | No |
| Native SDK | Python (FastAPI client) | TypeScript (`@malloydata/malloy`, `@malloydata/malloy-query-builder`) |
| UI / Playground | Interactive Gradio playground (SQL Compiler, Query Results, Mermaid ER, interactive RDF ontology graph, OSI import/export) **plus** a Jupyter notebook (`examples/quickstart.ipynb`) that runs natively in VS Code or in **Google Colab** with one click | VS Code extension (very polished) + Publisher web UI |
| RDF graph + SPARQL | Yes (`/graph`, `/sparql`) | No |
| Format conversion | OSI ↔ OBML (`/convert/*`) | n/a |

Both projects converge on REST + MCP for serving models. OBSL additionally exposes **two SQL wire protocols** for BI tool integration — PostgreSQL wire (v2.5.0+, works with Tableau / DBeaver / Superset / Power BI / `psql` / Dremio's Postgres-source connector) and Arrow Flight SQL (gRPC, columnar; JDBC/ODBC via Flight SQL drivers). Malloy doesn't have a comparable BI-tool wire protocol.

The authoring story differs more than it looks at first glance:
- **Malloy's VS Code extension** is a language-aware editor with autocomplete, inline visualizations, and a model-design feel. Strong for analysts writing `.malloy` files by hand.
- **OBSL's Jupyter notebook** (`examples/quickstart.ipynb`, also one-click in **Google Colab**) gives a Python-driven authoring loop that runs natively inside VS Code without any extension. Edit OBML, compile, execute, inspect results, iterate. Different shape, same goal: a real editor-resident dev loop. Plus the Gradio playground for browser-based model exploration with interactive ER and ontology graphs.

---

## 9. Time handling

| | OBSL | Malloy |
|---|---|---|
| Time grain | `TimeGrain` enum on dimensions/queries (year/quarter/month/week/day/hour/minute/second) | First-class field operators: `field.month`, `field.year_quarter`, `field.day_of_week`, etc. |
| Period comparison | Dedicated `period_over_period` metric type (4 comparison modes) | Ad-hoc query patterns + `prior_period` techniques |
| Cumulative / windowed | Dedicated `cumulative` metric type (running, rolling, grain-to-date) | `calculation:` declarations using window functions |

Malloy's time syntax is more ergonomic in a query; OBSL's metric types are more reusable across queries.

---

## 10. Other distinctives

| Feature | OBSL | Malloy |
|---|---|---|
| Hierarchical / nested results | ❌ flat tables only | ✅ `nest:` is a headline feature |
| Symmetric aggregates | ❌ uses static fanout detection + CFL | ✅ |
| Pipeline operator / refinements | ❌ JSON queries are atomic | ✅ `->` and `+ { ... }` |
| RDF/SPARQL graph view | ✅ | ❌ |
| Named secondary join paths | ✅ | ❌ |
| Explicit CFL multi-fact planner | ✅ | n/a (symmetric aggregates) |
| OSI ↔ OBML conversion | ✅ | ❌ |
| First-class PoP metric type | ✅ | ❌ (ad-hoc) |
| First-class cumulative metric type | ✅ | ❌ (calculations) |
| Dialect breadth (ClickHouse/Databricks/Dremio) | ✅ | ❌ |
| Trino/Presto | ❌ | ✅ |
| VS Code-native authoring | ✅ via Jupyter notebook (also one-click in Colab) | ✅ first-party extension with autocomplete + inline viz |
| Visualization renderer | ❌ (Gradio is ad-hoc) | ✅ first-class chart/dashboard renderer |
| LLM/agent-friendly query API | ✅ JSON, no DSL to learn | Possible via Publisher MCP, but Malloy's expressiveness asks more of the agent |

---

## 11. When to pick which

### Pick **Malloy** when:

- Your audience is analysts/engineers who'll write queries by hand and care about expressiveness.
- You need **hierarchical / nested** result shapes for dashboards (this is the killer feature).
- You want **symmetric aggregates** to remove fanout as a class of bug without thinking about it.
- You're heavy on Trino/Presto.
- You want a **language-aware** VS Code editor with autocomplete and inline visualizations (OBSL has a Jupyter-notebook path in VS Code / Colab, which is a great Python-driven loop but not a language-aware editor for OBML itself).

### Pick **OBSL** when:

- Your consumers are **applications, agents, or LLMs**, not humans writing a DSL — a stable JSON Query API is a feature, not a limitation.
- You need first-class, *reusable* **cumulative** and **period-over-period** metric definitions (vs. embedding window logic in each query).
- You target **ClickHouse, Databricks, or Dremio**.
- You need **named alternative join paths** for ambiguous graphs.
- You want a **graph view of the model** (RDF/SPARQL) for governance/lineage tooling.
- You want a **fully self-hostable, embeddable** semantic engine with no DSL dependency on the consumer side.

### They could coexist

It's plausible to use both: Malloy as the analyst-facing modeling/exploration layer and OBSL as the API-facing metrics service for embedded/agent use cases. The models would be expressed twice, but neither is a strict superset of the other.

---

## 12. Gap analysis

### To match Malloy, OBSL would need:

1. **Nested query results** — return tree-shaped data structures from a single query. This is the biggest functional gap and would require new AST nodes (`NestedQuery`) and result shape changes.
2. **Symmetric aggregates** — as an alternative to (or in addition to) the current static fanout + CFL approach. Would let users join across fanout without thinking.
3. **Trino/Presto dialect** — straightforward to add given the existing dialect plugin system.
4. **Named views with refinements** — a way to register a parameterized query template and apply per-call overrides.
5. **A language-aware authoring experience** — a VS Code extension for `.obml` with autocomplete, inline diagnostics, and inline visualizations. The Jupyter notebook + Colab + Gradio playground combination covers the dev loop, but a first-class editor extension would close the polish gap.

### To match OBSL, Malloy would need:

1. **First-class cumulative & period-over-period metric types** — declarative versions of what's currently expressed per-query.
2. **Named secondary join paths** with per-query selection.
3. **More warehouse dialects** — ClickHouse, Databricks, Dremio.
4. **RDF/SPARQL graph surface** for governance/lineage.
5. **A consumer-friendly JSON Query API** — Publisher gets close, but the schema is Malloy-shaped, so consumers still benefit from understanding Malloy.

---

## References

- OBSL `MetricType` enum: `src/orionbelt/models/semantic.py`
- OBSL CFL planner: `src/orionbelt/compiler/cfl.py`
- OBSL fanout detection: `src/orionbelt/compiler/fanout.py`
- OBSL docs: [Model Format](../guide/model-format.md), [Period-over-Period Metrics](../guide/period-over-period.md), [Compilation Pipeline](../guide/compilation.md)
- Malloy: https://github.com/malloydata/malloy
- Malloy Publisher (REST + MCP server for Malloy models): https://github.com/malloydata/publisher
- Malloy docs: https://docs.malloydata.dev/
