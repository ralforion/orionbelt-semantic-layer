# OBSL vs dbt Semantic Layer (MetricFlow)

A feature comparison between **OrionBelt Semantic Layer (OBSL)** and the **dbt Semantic Layer** (powered by MetricFlow). Captured 2026-05-23.

---

## TL;DR

- **dbt SL wins on**: conversion metrics, the `metric_time` virtual dimension, ecosystem maturity, and governance/lineage tied to the dbt transformation pipeline.
- **OBSL wins on**: being a self-hostable, warehouse-agnostic, transformation-tool-agnostic engine; **richer modeling topologies** (multi-rooted DAG, named alternative join paths) where dbt assumes single-rooted star/snowflake; explicit multi-fact CFL planning; an RDF/SPARQL view of the model; a clean REST surface; a more ergonomic period-over-period metric type; **first-class window metrics** (rank/lag/lead/ntile) and **9 statistical aggregates** (CORR / COVAR_* / REGR_* / STDDEV_* / VAR_*) neither has any analog in dbt SL.
- **Different niches**: dbt SL is "metrics on top of your dbt project, served by dbt Cloud." OBSL is "drop-in semantic compiler you can embed anywhere, modeling-tool independent."

---

## 1. Modeling philosophy

| Aspect | OBSL (OBML) | dbt Semantic Layer |
|---|---|---|
| Source of truth | Standalone YAML (`OBML`) — independent of any transformation tool | YAML coupled to dbt models; semantic models reference dbt `ref()`s |
| Top-level objects | `dataObjects`, `dimensions`, `measures`, `metrics`, `filters` | `semantic_models` (entities, measures, dimensions) + `metrics` |
| Object scoping | Each `DataObject` has its own `columns:`; dimensions/measures/metrics live at model scope and reference `{[DataObject].[Column]}` | Dimensions/measures/entities are scoped *inside* each `semantic_model`; metrics reference measures |
| Identity for joins | Explicit `joins` between data objects with `columnsFrom`/`columnsTo`, `joinType`, `secondary`, `pathName` | Implicit: `entities` of type `primary`/`foreign`/`unique`/`natural`; MetricFlow auto-resolves joins by matching entity names |
| Deployment | Self-hosted FastAPI service, MCP server, Gradio UI; OSS | Definitions in dbt Core OSS; **query API gated behind dbt Cloud** |

---

## 2. Metric types

OBSL `MetricType` enum (`src/orionbelt/models/semantic.py`):

| OBSL | dbt SL | Notes |
|---|---|---|
| `Measure` (sum/avg/count/min/max + `any_value` / `median` / `mode` / `listagg` + 9 statistical aggs, `total: bool` for grand totals) | `simple` metric over a `measure` (no statistical aggs) | OBSL wins on aggregate surface — see §6 |
| `Metric` `type: derived` with `{[Measure A]}/{[Measure B]}` expression | `ratio`, `derived` | Both first-class |
| `Metric` `type: cumulative` (running total, rolling window, grain-to-date, **per-dimension partition**) | `cumulative` (running, period-to-date, rolling — no partition-by) | Both first-class — see §3 |
| `Metric` `type: period_over_period` with 4 comparison modes | Approximated via `offset_window` and `metric_time` | OBSL has a dedicated metric type — see §4 |
| `Metric` `type: window` (rank / dense_rank / row_number / ntile / lag / lead / first_value / last_value) | — | **Gap in dbt** — no equivalent surface; users would compose raw SQL |
| — | `conversion` | **Gap in OBSL** |

OBSL coverage: ~95% of dbt's metric expressivity (missing only `conversion`), plus three OBSL-only surfaces dbt SL has no native answer for: `MetricType.WINDOW` (rank/lag/lead/ntile), `partitionBy` on cumulative metrics, and the 9-function statistical-aggregate surface.

---

## 3. Cumulative metrics (parity)

Implementation: `src/orionbelt/compiler/cumulative_wrap.py` (~230 lines), pipeline phase placed after PoP wrap so it operates on already-compared data when needed.

Three patterns supported, all dbt-equivalent:

```yaml
metrics:
 # 1. Running total (unbounded cumulative sum)
 - name: revenue_running_total
 type: cumulative
 measure: revenue
 timeDimension: order_date
 cumulativeType: sum

 # 2. Rolling window (e.g. last 7 days)
 - name: revenue_7d_avg
 type: cumulative
 measure: revenue
 timeDimension: order_date
 cumulativeType: avg
 cumulativeWindow: 7

 # 3. Grain-to-date (e.g. month-to-date, resets each month)
 - name: revenue_mtd
 type: cumulative
 measure: revenue
 timeDimension: order_date
 cumulativeType: sum
 grainToDate: month
```

Window functions used:

| Pattern | SQL produced |
|---|---|
| Running total | `SUM(x) OVER (ORDER BY time)` |
| Rolling window | `SUM(x) OVER (ORDER BY time ROWS BETWEEN N-1 PRECEDING AND CURRENT ROW)` |
| Grain-to-date | `SUM(x) OVER (PARTITION BY DATE_TRUNC(grain, time) ORDER BY time)` |

Aggregations: `sum`, `avg`, `min`, `max`, `count` (`CumulativeAggType`).
Grains for grain-to-date: `year`, `quarter`, `month`, `week` (`GrainToDate`).

**`partitionBy` extension (OBSL-only):** add a per-dimension partition to any rolling window:

```yaml
- name: revenue_ma12_by_country
 type: cumulative
 measure: revenue
 timeDimension: order_month
 cumulativeType: avg
 window: 12
 partitionBy: [country] # 12-month MA computed independently per country
```

dbt SL has no native partition-by surface on cumulative metrics; users would step outside the metric DSL and write window-function SQL by hand.

---

## 3a. Window metrics (OBSL-only)

OBSL ships `MetricType.WINDOW` for the single-row window-function family — `RANK`, `DENSE_RANK`, `ROW_NUMBER`, `NTILE`, `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`. dbt SL has no equivalent; the closest workaround is a derived metric over hand-rolled SQL, which means dropping out of the semantic-model layer entirely.

```yaml
metrics:
 # Rank revenue within each quarter
 - name: revenue_rank_by_quarter
 type: window
 windowFunction: dense_rank
 measure: revenue
 orderDirection: desc
 partitionBy: [quarter]

 # Prior-month revenue side by side with the current row
 - name: revenue_prior_month
 type: window
 windowFunction: lag
 measure: revenue
 offset: 1
 timeDimension: order_date
 partitionBy: [country]
```

Window metrics compose freely with derived metrics — a `{[Revenue]} - {[Revenue Prior Month]}` MoM delta needs no new SQL. Implementation: `src/orionbelt/compiler/window_wrap.py`, runs after `cumulative_wrap` in the pipeline so window functions can rank cumulative outputs.

See [Trend Analysis](../guide/trend-analysis.md) for the full feature surface.

---

## 4. Period-over-Period (OBSL advantage)

Implementation: `src/orionbelt/compiler/pop_wrap.py` (~510 lines).

OBSL exposes PoP as a **first-class metric type** with a comparison-mode enum, where dbt requires composing `offset_window` + `metric_time` + a derived metric.

```yaml
metrics:
 - name: revenue_yoy
 type: period_over_period
 measure: revenue
 periodOverPeriod:
 grain: month # bucket grain
 offset: -1 # compare vs. previous period
 offsetGrain: year # one year earlier
 comparison: percentChange # ratio | difference | previousValue | percentChange
```

Comparison modes (`PeriodOverPeriodComparison`):

- `ratio` — current / prior
- `difference` — current − prior
- `previousValue` — just the prior period's value
- `percentChange` — (current − prior) / prior

Internally builds a synthetic date range and joins current vs. comparison period.

**vs. dbt**: dbt typically requires:
1. Define a `simple` metric.
2. Reference it in a `derived` metric using `metric_time` and `offset_window` parameters.
3. Compose ratio/percent-change manually in `expr`.

OBSL's single-declaration approach is more ergonomic for the common case.

---

## 5. Multi-fact / fanout

| | OBSL | dbt SL |
|---|---|---|
| Fanout detection | Explicit `compiler/fanout.py` raises `FanoutError` | Avoided implicitly via entity types |
| Multi-fact strategy | Dedicated **CFL (Composite Fact Layer)** planner — `compiler/cfl.py` — emits `UNION ALL` legs with per-leg common-root resolution via `JoinGraph.find_common_root()` | MetricFlow join planner traverses entity graph; strategy is internal/opaque |
| User control | Explicit star-vs-CFL switch surfaces in compilation | Not exposed |
| Snowflake optimization | Uses `UNION ALL BY NAME` | n/a |

OBSL's CFL is more transparent and inspectable; dbt's approach is more declarative but harder to reason about for complex join graphs.

## 6. Data modeling topology (a major differentiator)

dbt SL is fundamentally **single-rooted, tree-shaped** in practice: each query resolves a base measure to a primary entity and traverses outward via matching entity names. Multi-fact queries work, but the topology is implicit and ambiguous multi-paths are a smell rather than a feature.

OBSL is built on a **directed join graph (DAG)** with explicit support for richer topologies:

| Topology | Star (single fact + dims) | Snowflake (chained dims) | Multi-rooted (multiple facts) | Multi-path (alt. joins between same pair) | Cycles |
|---|---|---|---|---|---|
| **OBSL** | ✅ | ✅ | ✅ via CFL `UNION ALL` legs with per-leg common root | ✅ first-class via `secondary: true` + `pathName` + per-query `usePathNames` | Detected and rejected |
| **dbt SL** | ✅ | ✅ | Partial — works if entities line up, but no explicit multi-fact planner | Workaround: define alternate entities and pick by relationship | Implicit |

**Why this matters**: Real-world warehouses are messy. You routinely need a customer→order→order_item path *and* a customer→returns path queryable in one model, or to choose between "ship_address_id" and "billing_address_id" joins to the same address dimension on a per-query basis. dbt expects you to flatten these into well-shaped entities upstream; OBSL lets you model them as-is and resolve at query time.

---

## 6a. Statistical aggregates (OBSL-only)

OBSL `Measure.aggregation` ships 9 statistical functions that dbt SL doesn't expose as a first-class surface — users would either step out to raw SQL or write a custom derived metric that depends on the warehouse's native functions:

| OBML aggregation | SQL | Inputs |
|---|---|:---:|
| `stddev`, `stddev_pop` | `STDDEV_SAMP(x)`, `STDDEV_POP(x)` | 1 |
| `variance`, `var_pop` | `VAR_SAMP(x)`, `VAR_POP(x)` | 1 |
| `corr` | `CORR(x, y)` | 2 |
| `covar_pop`, `covar_samp` | `COVAR_POP(x, y)`, `COVAR_SAMP(x, y)` | 2 |
| `regr_slope`, `regr_intercept` | `REGR_SLOPE(y, x)`, `REGR_INTERCEPT(y, x)` | 2 |

OBSL validates column arity at model-load time and gates dialect support at compile time (MySQL rejects correlation/covariance/regression; BigQuery + ClickHouse reject linear regression — all with a hard `UnsupportedAggregationError`, no silent fallback). See [Trend Analysis](../guide/trend-analysis.md#3-statistical-aggregates-on-measure) for the full coverage matrix.

---

## 7. Joins

| | OBSL | dbt SL |
|---|---|---|
| Definition | Directed `join` declarations with `columnsFrom`/`columnsTo`, `joinType`, `secondary`, `pathName` | Inferred by matching `entity` names across semantic models |
| Multiple paths between same objects | First-class via `secondary: true` + named `pathName`, selected per-query via `usePathNames: [{source, target, pathName}]` | Express via additional entities — no path naming primitive |
| Cycle / multi-path validation | Built into resolver; `pathName` required for secondary | n/a (graph traversal handles) |

---

## 8. Dialects / execution

| | OBSL | dbt SL |
|---|---|---|
| Dialect coverage | 8: BigQuery, ClickHouse, Databricks, Dremio, DuckDB, MySQL, Postgres, Snowflake | All warehouses dbt supports (broader) |
| SQL generation | Full custom AST → SQL with per-dialect codegen (`dialect/*.py`) | MetricFlow → SQL |
| Execution surface | Self-hosted, runs anywhere | Query API (JDBC/GraphQL/Python SDK/MCP) **dbt Cloud only** |
| dbt Core query API | n/a | Definitions only — no built-in runtime serving |

---

## 9. APIs / interfaces

| | OBSL | dbt SL |
|---|---|---|
| Natural SQL surface | **OrionBelt Semantic QL (OBSQL)** — write `SELECT "Region", "Total Sales" FROM sales` (or without FROM); `MEASURE()` marker; aggregate-wrap matching against declared aggregation; `WITH ROLLUP` / `WITH CUBE` first-class | No — dbt SL has no SQL surface; you ask for metrics via GraphQL or the `dbt sl query` CLI, both DSL-specific |
| Catalog discovery from BI tools | `SHOW TABLES`, `DESCRIBE <model>`, `information_schema.*`, `pg_catalog.*` answered from the model in-process — never touches the warehouse | n/a |
| Governance | **Closed by design** — raw warehouse SQL and DDL/DML always reject with `RAW_SQL_REJECTED` / `WRITE_OPERATION_REJECTED`. No env flag to bypass | n/a |
| REST API | Yes — full session lifecycle, validate/compile/execute, ER diagram, `find`, lineage `explain`, OSI conversion | No (REST not offered) |
| Arrow Flight SQL | Yes — gRPC server on port 8815; BI tools (DBeaver, Tableau JDBC, Power BI ODBC) connect natively. Multi-model addressing via the `database` gRPC header. | No |
| DB-API 2.0 drivers | Yes — 8 drivers (`ob-bigquery`, `ob-snowflake`, `ob-postgres`, `ob-mysql`, `ob-duckdb`, `ob-clickhouse`, `ob-databricks`, `ob-dremio`) | No |
| GraphQL | No | Yes (dbt Cloud) |
| JDBC | Via Arrow Flight SQL JDBC driver | Yes (dbt Cloud) |
| MCP | Yes (in-tree thin client + standalone repo `orionbelt-semantic-layer-mcp`) | Yes (`dbt-mcp`) |
| Python SDK | Via FastAPI client | Yes |
| UI / Playground | Yes — interactive Gradio playground: SQL Compiler, Query Results table, auto-generated Mermaid ER diagrams, interactive RDF/OBSL ontology graph (vis-network), OSI import/export, settings panel | dbt Cloud Studio (paid) |
| RDF graph + SPARQL | Yes (`/graph`, `/sparql`) | No |
| Format conversion | OSI ↔ OBML round-trip (`/convert/*`) | n/a |

---

## 10. Other distinctives

| Feature | OBSL | dbt SL |
|---|---|---|
| Sessions / multi-tenant runtime | TTL, max-age, rate limits, 410/429 | Cloud-managed |
| Caching | Result cache based on freshness inheritance (file backend, off by default): TTL derived from per-`dataObject` `refresh:` contract; ETL `POST /v1/heartbeat` invalidates dependent entries by physical table | dbt Cloud query cache |
| Versioned governance, lineage to upstream models | No (model is standalone) | Strong — inherits dbt's lineage, tests, docs, exposures |
| Filter ergonomics | `MeasureFilter`, `FilterContext`, `GrainOverride`, query-level `where`/`having` | Per-metric `filter:`, `metric_time` |
| Vendor-agnostic | Yes — pure OSS | Practical lock-in: production query APIs require dbt Cloud |

---

## 11. Gap analysis (OBSL → dbt parity)

To match dbt SL feature-for-feature, OBSL would need:

1. **`conversion` metric type** — funnel-style metric: count of base events that converted to a target event within a window.
2. **`metric_time` virtual dimension** — a unified time axis across heterogeneous fact tables, abstracting per-table date columns. (Partially achievable today via dimensions on each data object, but not as a single canonical handle.)
3. **(Nice to have) GraphQL or JDBC surface** — for BI tool integration parity.

Conversely, dbt SL would need to add to match OBSL's strengths:

1. **Self-hostable query runtime** (currently dbt Cloud only).
2. **RDF/SPARQL graph view** of the model.
3. **Named secondary join paths** for non-trivial multi-path scenarios.
4. **Explicit fanout detection / CFL planner exposure**.

---

## References

- OBSL `MetricType` enum: `src/orionbelt/models/semantic.py`
- OBSL cumulative wrap: `src/orionbelt/compiler/cumulative_wrap.py`
- OBSL PoP wrap: `src/orionbelt/compiler/pop_wrap.py`
- OBSL CFL planner: `src/orionbelt/compiler/cfl.py`
- OBSL docs: [Model Format](../guide/model-format.md), [Period-over-Period Metrics](../guide/period-over-period.md), [Compilation Pipeline](../guide/compilation.md)
- dbt SL: https://docs.getdbt.com/docs/build/about-metricflow
- dbt SL GraphQL API: https://docs.getdbt.com/docs/dbt-cloud-apis/sl-graphql
