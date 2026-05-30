# OBSL vs AtScale

A feature comparison between **OrionBelt Semantic Layer (OBSL)** and **AtScale** — the enterprise "universal semantic layer" with deep OLAP heritage and native MDX/DAX support. Captured 2026-05-23.

---

## TL;DR

- **AtScale wins on**: enterprise OLAP capabilities (true hierarchies, parent-child, ragged, time-intelligence), **native MDX support** for Excel pivot tables and SSAS-protocol clients (no other peer in this comparison set has this), **DAX support** for Power BI live connections, **autonomous aggregates** (machine-learned auto-rollups similar to Cube pre-aggregations but more automated), enterprise governance (RLS, perspectives, lineage), and broad legacy/cloud warehouse coverage.
- **OBSL wins on**: being **open-source and self-hostable for production with no licensing tier** (AtScale offers a free Developer Community Edition for evaluation, but production multi-user deployments require an enterprise license), a **simple JSON Query API** consumable by any client, **first-class declarative metric types** for cumulative (with `partitionBy`), period-over-period, and **window** (rank / lag / lead / ntile / first_value / last_value), **9 statistical aggregates** (CORR / COVAR_* / REGR_* / STDDEV_* / VAR_*), an **RDF/SPARQL graph view** of the model, **named secondary join paths**, **multi-rooted DAG topology with the CFL planner**, an **interactive ontology-graph playground**, **OSI v0.2 ↔ OBML format conversion**, and a much smaller operational footprint.
- **Different niches**: AtScale is "the enterprise OLAP semantic layer for Excel/PowerBI/Tableau shops that need MDX." OBSL is "the open-source, embeddable semantic compiler for apps, agents, and modern BI tools that speak Arrow Flight SQL or REST."

AtScale is the only peer in this comparison set that natively speaks **MDX**, the OLAP wire protocol that Excel and SSAS clients use. That's a real moat for organizations standardized on Microsoft BI tooling. OBSL doesn't compete on that axis.

---

## 1. Modeling philosophy

| Aspect | OBSL (OBML) | AtScale |
|---|---|---|
| Format | Declarative YAML (`OBML`) | Visual model designer (Design Center) backed by JSON / XML model definitions; CLI/API for programmatic deployment |
| Source of truth | YAML model files | Models authored in AtScale Design Center; deployed to AtScale Engine |
| Top-level constructs | `dataObjects`, `dimensions`, `measures`, `metrics`, `filters` | `data models` (cubes), `dimensions` (with hierarchies, levels, attributes), `measures`, `calculated members`, `perspectives`, `aggregates`, `security` |
| OLAP primitives | Time grain on dimensions, no first-class hierarchies | First-class **hierarchies** (multiple per dimension), **levels**, **parent-child** dimensions, **ragged** hierarchies |
| Templating | None | Limited; some calculated-member expressions |
| Runtime | OSS, self-hosted (single Python service) | Proprietary AtScale Engine — on-prem, cloud-hosted, or AtScale-managed |

**Key cultural difference**: AtScale carries OLAP heritage from the SSAS/MDX world — it thinks in cubes, hierarchies, and calculated members. OBSL thinks in data objects, joins, and metrics. The model overlap is real but the conceptual frames differ.

---

## 2. Concept mapping

| OBSL | AtScale | Notes |
|---|---|---|
| `DataObject` | Dataset (mapped to a warehouse table or query) | |
| `Dimension` (model-scoped) | Dimension with one or more hierarchies + levels | AtScale has richer dimension structure |
| `Measure` | Measure on a fact dataset | |
| `Metric` `type: derived` | Calculated member (MDX expression) or calculated measure | AtScale uses MDX syntax for calculations |
| `Metric` `type: cumulative` | Time-intelligence functions in MDX (`PeriodsToDate`, `YTD`, etc.) — declarative via dimension hierarchies | AtScale's time intelligence is OLAP-native |
| `Metric` `type: period_over_period` | Calculated members using time-shift MDX expressions (`ParallelPeriod`, `Lag`) | Idiomatic in AtScale |
| `DataObjectJoin` | Relationships between dimensions and facts | |
| `secondary: true` + `pathName` | Multiple relationships / role-playing dimensions | |
| `QueryObject` JSON | Queries arrive via MDX, DAX, SQL, or REST | |
| `Filter` (named, reusable) | Perspectives (curated subsets of a model) + named sets | Conceptually similar |
| OBSL session-scoped REST | AtScale Engine + JDBC/ODBC/MDX/DAX endpoints | |

---

## 3. The headline AtScale features

### 3.1 Native MDX (the unique moat)

AtScale speaks **MDX** (Multidimensional Expressions) natively over the **XMLA** protocol — the same protocol Excel pivot tables, SSAS, and tools like Tableau (when using the MDX driver) use. Excel users connect to AtScale as if it were a SQL Server Analysis Services cube and get full pivot-table experience including drill-down, hierarchies, and time intelligence.

**This is unique in this comparison set.** No other tool here (OBSL, dbt SL, Malloy, LookML, Cube) speaks MDX. For organizations whose business users live in Excel and demand pivot-table workflows on warehouse data, AtScale is the only practical choice.

**OBSL** does not speak MDX. Excel connectivity would require an ODBC bridge to the Arrow Flight SQL endpoint or REST.

### 3.2 DAX support (Power BI live connections)

AtScale also speaks **DAX**, enabling Power BI live (non-import) connections that hit AtScale instead of Power BI's own Tabular engine. This means a Power BI dashboard can use the centrally-governed AtScale model rather than a per-user imported dataset.

**OBSL** does not speak DAX. Power BI integration is via Arrow Flight SQL ODBC.

### 3.3 Autonomous aggregates

AtScale's "Autonomous Data Engineering" automatically creates and maintains aggregate tables in the warehouse based on observed query patterns. Similar in spirit to Cube pre-aggregations but more automated — AtScale picks what to aggregate, builds it, and routes queries transparently.

**OBSL** has no aggregate / materialization story.

### 3.4 OLAP hierarchies and time intelligence

AtScale natively models multi-level hierarchies (e.g. `Year > Quarter > Month > Day`) within a single dimension, including:
- **Multiple hierarchies** per dimension (calendar vs. fiscal year)
- **Parent-child** hierarchies (org charts, account trees)
- **Ragged** hierarchies (geography with optional state/region levels)
- **Time intelligence** functions: YTD, MTD, parallel period, period-to-date

**OBSL** has time grain on dimensions but no first-class multi-level hierarchies or parent-child structures.

### 3.5 Enterprise governance

AtScale ships first-class enterprise primitives:
- **Row-level security** based on user attributes / Active Directory groups
- **Dimension-level / measure-level security** (hide measures from certain roles)
- **Perspectives** — curated subsets of a model exposed to specific user groups
- **Lineage and impact analysis** baked into the platform
- **SAML, OAuth, AD integration**

**OBSL** has session-scoped models (TTL, max-age, rate limits) but no first-class user/auth model.

---

## 4. Metric types

### 4.1 Aggregation surface on `Measure`

| Family | OBSL | AtScale |
|---|---|---|
| Standard | `sum`, `count`, `count_distinct`, `avg`, `min`, `max` | `sum`, `count`, `distinct count`, `avg`, `min`, `max` |
| Shape | `any_value`, `median`, `mode`, `listagg` | Semi-additive measures (last non-empty, first non-empty) — OLAP-native |
| Statistical | `stddev`, `stddev_pop`, `variance`, `var_pop` | Via MDX calculated members (`Stdev`, `Var`) — not first-class measure types |
| Association / regression | `corr`, `covar_pop`, `covar_samp`, `regr_slope`, `regr_intercept` | Via MDX calculated members — not first-class measure types |
| Grand totals | `total: bool` on the measure | Native to OLAP — every grain rolls up by construction |

**The honest difference**: AtScale's OLAP heritage gives it stronger *semi-additive* primitives (last-value, opening / closing balances) that OBSL doesn't model. OBSL's statistical / regression aggregations are first-class declarative measure types with arity validation and dialect gating; AtScale reaches the same functions through MDX calculated members. Different mental models — both can compute the same SQL.

### 4.2 Metric types

| OBSL | AtScale | Notes |
|---|---|---|
| `Metric` `type: derived` | Calculated members (MDX expressions) | Both first-class |
| `Metric` `type: cumulative` (running, rolling, grain-to-date, **per-dimension `partitionBy`**) | Time-intelligence MDX: <pre><code>Aggregate(<br> YTD([Date].CurrentMember),<br> [Sales]<br>)</code></pre> — idiomatic OLAP | Both expressive; OBSL is YAML-declarative, AtScale is MDX-declarative |
| `Metric` `type: period_over_period` (4 comparison modes) | Calculated members using `ParallelPeriod` / `Lag` MDX functions | Both expressive; AtScale is more flexible (full MDX), OBSL is more turnkey |
| `Metric` `type: window` — <br>`rank`, `dense_rank`, `row_number`, `ntile`,<br>`lag`, `lead`, `first_value`, `last_value` | MDX `Rank()`, `Lag()`, `Lead()` in calculated members | OBSL exposes these as declarative metric types; AtScale expresses them via MDX |
| Reusable filter context / filtered measures | Named sets, perspectives | Comparable |
| Hierarchical aggregation | Limited (use grains) | First-class via hierarchies and `Aggregate()` |

For complex OLAP-style metrics (MDX is genuinely more expressive than any YAML format), AtScale wins on flexibility. For straightforward cumulative/PoP/window/statistical metrics that you want declared once and reused everywhere, OBSL is more turnkey. See [Trend Analysis](../guide/trend-analysis.md) for the full v2.6 metric / aggregation surface.

---

## 5. Joins / relationships

| | OBSL | AtScale |
|---|---|---|
| Definition site | `joins:` array on `DataObject` | Relationships in Design Center between dimensions and facts |
| Cardinality | `joinType`: `many-to-one`, `one-to-one`, `many-to-many` | Relationship cardinality + role-playing dimensions |
| Multiple paths | First-class via `secondary: true` + `pathName` + per-query `usePathNames` | **Role-playing dimensions** (a dimension joined multiple ways with different roles) — the OLAP-native way |
| Cycle / multi-path validation | Built into resolver | Engine-level checks |

AtScale's role-playing dimensions are conceptually similar to OBSL's named secondary paths — both are first-class ways to handle "same dim joined two different ways." Different terminology, similar capability.

---

## 6. Data modeling topology (a major differentiator)

AtScale models are typically **single-cube-rooted**: each data model has a single fact base (or a small number of fact tables joined via shared dimensions). Multi-fact reporting is achieved either by (a) having multiple data models, (b) carefully designing one model that joins facts via conformed dimensions, or (c) using AtScale's cube-of-cubes patterns. Multi-path joins are handled via role-playing dimensions, which is elegant but distinct from OBSL's `pathName` primitive.

OBSL is built on a **directed join graph (DAG)** with explicit support for richer topologies:

| Topology | Star (single fact + dims) | Snowflake (chained dims) | Multi-rooted (multiple facts) | Multi-path (alt. joins between same pair) | Cycles |
|---|---|---|---|---|---|
| **OBSL** | ✅ | ✅ | ✅ via CFL `UNION ALL` legs with per-leg common root | ✅ first-class via `secondary: true` + `pathName` + per-query `usePathNames` | Detected and rejected |
| **AtScale** | ✅ | ✅ | Conformed-dimension patterns or multiple data models | ✅ via role-playing dimensions (different mechanism, similar capability) | Implicit |

**Why this matters**: AtScale's OLAP heritage assumes you've designed a clean cube up front. OBSL's CFL planner lets you query across multiple unrelated facts in one go without pre-designing the cube boundary. For ad-hoc embedded analytics or AI/agent queries that don't know in advance which facts they'll touch, OBSL's flexibility is a real advantage.

---

## 7. Dialects / data sources

AtScale connects to a broad list of warehouses via its query engine.

| Dialect | OBSL | AtScale |
|---|---|---|
| BigQuery | ✅ | ✅ |
| Snowflake | ✅ | ✅ |
| Postgres | ✅ | ✅ |
| MySQL | ✅ | Partial (less common) |
| DuckDB | ✅ | ❌ |
| Databricks | ✅ | ✅ |
| ClickHouse | ✅ | Partial |
| Dremio | ✅ | ❌ |
| Redshift | ❌ | ✅ |
| Synapse / SQL Server | ❌ | ✅ |
| Hive / Hadoop | ❌ | ✅ (legacy strength) |
| Iceberg / Open table formats | ❌ | ✅ |

AtScale wins on legacy and enterprise (Synapse, Hive, Iceberg lakehouse). OBSL is competitive on modern cloud warehouses and uniquely supports DuckDB and Dremio.

---

## 8. APIs / interfaces

| | OBSL | AtScale |
|---|---|---|
| REST API | Yes — first-party FastAPI service | Yes — REST API for model management and queries |
| MCP | Yes — first-party server | Limited (via AI-Link integrations) |
| GraphQL | No | No |
| Native SQL endpoint | Apache Arrow Flight SQL (gRPC, columnar) | JDBC / ODBC SQL endpoint |
| MDX (XMLA) | ❌ | ✅ — primary protocol for Excel/SSAS clients |
| DAX | ❌ | ✅ — for Power BI live connections |
| JDBC | ✅ via Arrow Flight SQL JDBC driver | ✅ native |
| ODBC | ✅ via Arrow Flight SQL ODBC driver | ✅ native |
| DB-API 2.0 drivers | ✅ 8 drivers shipped | ❌ |
| Python SDK | Via FastAPI client | AI-Link Python SDK |
| UI / IDE | Interactive Gradio playground (SQL Compiler, Mermaid ER, RDF ontology graph, OSI import/export) | AtScale Design Center (visual model designer, polished, proprietary) |
| RDF graph + SPARQL | Yes (`/graph`, `/sparql`) | No |
| Format conversion | OSI ↔ OBML (`/convert/*`) | OSI support (founding contributor — direction of travel) |

AtScale's MDX/DAX support is unique and a genuine advantage for Microsoft-stack BI environments. OBSL's RDF/SPARQL surface and DB-API drivers are unique in the other direction.

---

## 9. Open-source vs. proprietary

| | OBSL | AtScale |
|---|---|---|
| License | Source-available (BSL) | Proprietary; **free Developer Community Edition** for non-production / individual use |
| Self-hostable | ✅ (one Python service) | ✅ — Developer Edition is free to install; production deployment requires enterprise license |
| Pricing | OSS is free for self-hosted production; commercial tiers available for embedded analytics, managed cloud, and enterprise features | **Developer Edition: free** (with feature/scale limits). Enterprise: typical six-figure annual licensing for production |
| Commercial offering | Embedded analytics license · commercial cloud offering · enterprise features · consulting + support | Enterprise license — production deployments, autonomous aggregates at scale, enterprise governance, vendor support |
| Vendor lock-in | None (plain YAML, OSI-portable) | High — model lives in AtScale's proprietary format (mitigated for OSI-aware exports) |
| Air-gapped deploy | ✅ supported | ✅ supported (enterprise) |
| Self-host parity | OSS has full parity on the shipped v2.6 surface; enterprise tier adds enterprise-specific capabilities on top | Full features in licensed enterprise tier; Developer Edition has feature/scale restrictions |

The free **Developer Community Edition** lowers AtScale's barrier for evaluation, prototyping, and individual learning — you can model, run MDX queries from Excel, and explore AtScale without a contract. For production multi-user deployments, autonomous aggregates at scale, or enterprise governance/support, the licensed AtScale tier is required. For OBSL the OSS tier is fully production-grade for self-hosted use; commercial offerings (embedded analytics license, managed cloud, enterprise features, consulting + support) are available alongside if you want them, not as an upgrade gate for OSS capabilities.

---

## 10. Other distinctives

| Feature | OBSL | AtScale |
|---|---|---|
| Native MDX (XMLA / SSAS protocol) | ❌ | ✅ unique strength |
| Native DAX (Power BI live) | ❌ | ✅ |
| Autonomous aggregates | ❌ | ✅ flagship enterprise feature |
| Multi-level hierarchies, parent-child, ragged | ❌ (grain only) | ✅ first-class OLAP |
| Time intelligence (YTD, MTD, parallel period) | Via `cumulative` and `period_over_period` metric types | First-class MDX functions |
| Role-playing dimensions | Via `secondary: true` + `pathName` (different mechanism) | First-class OLAP idiom |
| Enterprise RLS / governance | ❌ | ✅ |
| Perspectives (model subsets) | Via separate models | ✅ |
| Lineage / impact analysis | ❌ | ✅ |
| Multi-rooted DAG modeling | ✅ via CFL | ❌ (cube-rooted) |
| Named secondary join paths (per-query) | ✅ | Role-playing dimensions are model-time, not query-time |
| First-class declarative PoP metric type | ✅ | Via MDX (more flexible but less turnkey) |
| First-class declarative cumulative metric type | ✅ (running, rolling, grain-to-date, `partitionBy`) | Via MDX time intelligence |
| First-class declarative window metric type (rank / lag / lead / ntile / first_value / last_value) | ✅ | Via MDX calculated members |
| First-class statistical / regression aggregates (`stddev`, `variance`, `corr`, `covar_*`, `regr_*`) as measure types | ✅ 9 declarative aggregations | Via MDX calculated members — not first-class measure types |
| Semi-additive measures (last/first non-empty) | ❌ | ✅ first-class OLAP |
| Apache Arrow Flight SQL | ✅ | ❌ |
| DB-API 2.0 drivers | ✅ 8 drivers | ❌ |
| RDF/SPARQL graph view | ✅ | ❌ |
| Interactive ontology-graph playground | ✅ Gradio | ❌ |
| OSI interoperability | ✅ converter shipped | ✅ founding contributor |
| MCP server (LLM/agent) | ✅ first-party | Limited |
| Built-in caching | ✅ file cache based on freshness inheritance (TTL derived from per-`dataObject` `refresh:` contracts; ETL heartbeat invalidation) | ✅ result cache + autonomous aggregates (warehouse-side) |
| Visual model designer | ❌ | ✅ Design Center (polished) |
| Open source / self-host without license | ✅ | ❌ |

---

## 11. When to pick which

### Pick **AtScale** when:

- Your business users live in **Excel pivot tables** and you need a real OLAP/MDX experience (this is the killer feature — no peer here matches it).
- You're running **Power BI** with live (DirectQuery / non-import) connections to a centrally-governed model via DAX.
- You need **autonomous aggregates** maintained by the platform without DIY rollup engineering.
- You need first-class **OLAP hierarchies**, parent-child structures, ragged hierarchies, and time intelligence as model primitives.
- You need **enterprise governance**: RLS, perspectives, lineage, AD integration, SAML/OAuth, audit trails.
- You're already buying enterprise BI tools (Tableau, Power BI, Excel, MicroStrategy) and the per-seat math works.
- Your warehouse is **Synapse, Hive, or Iceberg** (AtScale's lakehouse story is mature).
- You're **evaluating** an OLAP/MDX semantic layer — the free Developer Community Edition lets you prototype before committing.

### Pick **OBSL** when:

- You need an **open-source, self-hostable, embeddable** semantic layer with **no production licensing tier** — OBML is plain YAML, the runtime is one Python service, and the same OSS bits run in production as in development. AtScale's free Community Edition is great for evaluation, but production multi-user deployments still need an enterprise contract.
- Your consumers are **applications, agents, or LLMs** — a stable JSON/Arrow Flight SQL API beats requiring callers to know MDX/DAX.
- You need **multi-rooted DAG modeling** for messy real-world warehouses with multiple fact tables and ambiguous join paths.
- You want **first-class declarative cumulative and period-over-period metric types** rather than expressing them as MDX calculated members.
- You target **DuckDB, Dremio, or ClickHouse** with full driver coverage.
- You want **OSI interoperability** for portability between semantic layers.
- You want a **graph view of the model** (RDF/SPARQL) for governance/lineage tooling without buying enterprise software.
- Your operational appetite is small — **one Python service**, no separate model designer or aggregate engine to operate.

### They could coexist

In a Microsoft-stack enterprise: AtScale serves the human BI audience (Excel, Power BI, Tableau via MDX) and OBSL serves the embedded / API / agent audience (REST, Arrow Flight SQL, MCP). With OSI as a common interchange format the two models could in principle stay in sync — though in practice you'll author one as the source of truth and synchronize toward the other.

---

## 12. Gap analysis

### To match AtScale, OBSL would need:

1. **MDX / XMLA support** — speak the OLAP wire protocol that Excel and SSAS clients use. This is the biggest single gap and a major engineering investment (MDX is a sizable language to implement).
2. **DAX support** for Power BI live connections — separate but related effort.
3. **Multi-level hierarchies, parent-child, ragged** — first-class OLAP dimension structures beyond simple grains.
4. **Autonomous aggregates / pre-aggregations** — automated rollup creation and query routing (also a Cube gap).
5. **Enterprise governance primitives** — RLS, perspectives, dimension/measure-level security, AD integration.
6. **Lineage and impact analysis** — first-class governance surface.
7. **Time intelligence library** richer than the current `cumulative` + `period_over_period` types — full MDX-equivalent functions like `ParallelPeriod`, `OpeningPeriod`, `ClosingPeriod`.
8. **Visual model designer** for non-developer authors.
9. **More dialects** — Synapse, Redshift, Iceberg lakehouse format.

### To match OBSL, AtScale would need:

1. **Open-source / self-hostable runtime in production** — the free Developer Community Edition is welcome for evaluation, but production deployments still require an enterprise license. A truly OSS production tier would be the structural unlock.
2. **Multi-rooted DAG modeling with explicit CFL multi-fact planning** — the ability to query across genuinely independent fact tables in one go without pre-designing a cube.
3. **First-class declarative cumulative & period-over-period metric types** — turnkey alternatives to writing MDX calculated members.
4. **RDF/SPARQL graph surface** for governance/lineage tooling outside the proprietary platform.
5. **A modern OSS SQL wire protocol surface for BI tools** — OBSL ships both **PostgreSQL wire** (Tableau / DBeaver / Superset / Power BI / `psql` / Dremio's Postgres-source connector, v2.5.0+) and **Apache Arrow Flight SQL** (gRPC, columnar, JDBC/ODBC via Flight SQL drivers) side-by-side; AtScale's BI-tool surface is JDBC/ODBC + MDX/DAX through the enterprise gateway.
6. **First-class DB-API 2.0 drivers** for direct programmatic access from Python.
7. **MCP server (first-party)** for LLM/agent integration.
8. **Plain-text portable model format** — OBML is a static YAML file that diffs in Git; AtScale models are richer but harder to version-control as plain text.

---

## References

- OBSL `MetricType` enum: `src/orionbelt/models/semantic.py`
- OBSL CFL planner: `src/orionbelt/compiler/cfl.py`
- OBSL fanout detection: `src/orionbelt/compiler/fanout.py`
- OBSL docs: [Model Format](../guide/model-format.md), [Period-over-Period Metrics](../guide/period-over-period.md), [Compilation Pipeline](../guide/compilation.md)
- AtScale: https://www.atscale.com/
- AtScale docs: https://documentation.atscale.com/
- AtScale and OSI: https://open-semantic-interchange.org/
