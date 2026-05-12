# OrionBelt Semantic QL (OBSQL)

**Semantic QL** is OrionBelt's natural SQL surface — BI-style SQL written
against a **per-model virtual table** and translated to a `QueryObject` that
flows through the standard compilation pipeline. The short brand form is
**OBSQL**; both terms refer to the same language. It's the third member of
the OrionBelt trio:

| Short form | Full name | What it is |
|---|---|---|
| **OBSL** | OrionBelt **Semantic Layer** | The system |
| **OBML** | OrionBelt **Modeling Language** | YAML format for defining models |
| **OBSQL** | OrionBelt **Semantic QL** | SQL surface for querying them |

Semantic QL is the same SQL dialect on two transports:

- **REST**: `POST /v1/sessions/{id}/query/semantic-ql` (and the
  top-level shortcut `POST /v1/query/semantic-ql` in single-model mode)
- **Arrow Flight SQL**: any plain SQL the Flight server receives is
  classified by FROM target. Targets that match the model's virtual table
  are translated through the same path. See the Flight server section
  below.

## Why a virtual table?

BI tools (Tableau, Power BI, Metabase, Superset, DBeaver) compose SQL
from a catalog tree and a column picker. They cannot author OBML YAML.
Exposing the semantic model as **one virtual table per model**, with
columns = dimensions + measures + metrics, gives those tools exactly the
SQL surface they expect — without leaking warehouse joins to the user.

Semantic QL is OBSL's take on a well-trodden pattern — Cube SQL API, dbt
Semantic Layer JDBC, AtScale, and Snowflake `SEMANTIC_VIEW(...)` all
expose semantic models as virtual SQL surfaces. The differentiator:
Semantic QL ships with **explicit measure markers** (`MEASURE("X")`),
**aggregate-wrap matching** (`SUM(sum_measure)` validated against the
declared aggregation), and **first-class hierarchical subtotals**
(`WITH ROLLUP` / `WITH CUBE`) — see the corresponding sections below.

## Accepted shape

```sql
SELECT <dimension or measure labels>
FROM   <model_name>
[WHERE <predicates>]
[HAVING <predicates>]
[ORDER BY <label or position> [ASC | DESC]]
[LIMIT <n>]
[WITH ROLLUP | WITH CUBE]
```

| Clause     | Rules |
|------------|-------|
| `SELECT`   | Bare identifiers, `MEASURE("<label>")`, or an aggregate wrapper that **matches the measure's declared aggregation** (`SUM("X")` on a SUM-measure, `COUNT(DISTINCT "X")` on a count_distinct-measure, etc.). Mismatched wraps (`MIN` on a SUM-measure) and **any** wrap on a metric reject with a message naming the declared aggregation. `SELECT *` and free-form expressions are rejected. |
| `FROM`     | The model's virtual table. Any other target is rejected unless `FLIGHT_ALLOW_RAW_SQL` / `FLIGHT_ALLOW_DATA_OBJECT_SQL` is on (Flight only — REST is always semantic mode). |
| `WHERE`    | `column op literal` atoms joined by `AND`. Measure / metric references are auto-routed to `HAVING`. Top-level `OR` is rejected. |
| `HAVING`   | Same shape as `WHERE`; passes through unchanged. |
| `GROUP BY` | Silently ignored — implicit from the dimensions in `SELECT`. BI tools auto-emit it; we tolerate it for compatibility. |
| `ORDER BY` | Identifier (must be a SELECT alias) or 1-based position. |
| `LIMIT`    | Integer literal. |
| `WITH ROLLUP` / `WITH CUBE` | Trailing modifier — see below. |

## Selecting measures: three accepted forms

The translator accepts three syntactically distinct ways to reference a
measure or metric in `SELECT`:

```sql
-- 1. Bare label — terse, recommended for hand-written SQL
SELECT "Region", "Total Sales" FROM sales_model

-- 2. MEASURE() marker — Snowflake SEMANTIC_VIEW / Databricks metric-view syntax
SELECT "Region", MEASURE("Total Sales") FROM sales_model

-- 3. Aggregate wrapper that matches the measure's declared aggregation
SELECT "Region", SUM("Total Sales") FROM sales_model
```

All three compile to identical vendor SQL — the wrapper is stripped at
translation time.

### Rule for aggregate wrappers

For **measures**, the wrapping aggregate must equal the measure's
declared `aggregation`. Mismatches reject with a clear error:

| Measure declares | Accepted wrap | Rejected example |
|---|---|---|
| `sum` | `SUM("X")` | `MIN("X")`, `AVG("X")` |
| `count` | `COUNT("X")` | `SUM("X")` |
| `count_distinct` | `COUNT(DISTINCT "X")` | `COUNT("X")` |
| `avg` | `AVG("X")` | other |
| `min` / `max` | matching `MIN("X")` / `MAX("X")` | crossing them |

Error message names the declared aggregation so the caller can fix it:

> `[UNSUPPORTED_SQL_FEATURE]` Measure `Order Count` is declared as `COUNT` —
> applying `SUM` would change its math. Use `COUNT("Order Count")`, bare
> `"Order Count"`, or `MEASURE("Order Count")`.

### Wrappers on metrics

**Metrics reject every aggregate wrapper** — a derived expression like
`Revenue per Order = SUM(amount) / COUNT(order_id)` is already evaluated
at the query's grain, and no outer aggregate is mathematically correct.
Use bare `"Revenue per Order"` or `MEASURE("Revenue per Order")`.

> `[UNSUPPORTED_SQL_FEATURE]` Metric `Revenue per Order` is a derived
> expression already evaluated at the query's grain — applying `SUM(...)`
> would change its math. Use bare `"Revenue per Order"` or
> `MEASURE("Revenue per Order")`.

### Why this matters

BI tools (Tableau, Power BI, Metabase, Superset) emit `SUM(measure_col)`
or `COUNT(measure_col)` reflexively when you drop a measure on a viz.
For SUM-typed measures this now works seamlessly. For metrics, the user
sees a concrete error pointing at the right syntax instead of silently
getting wrong numbers (e.g., the classic "sum of per-region ratios"
trap). Honesty over convenience — the semantic layer exists to make the
math right.

## Raw mode — detail rows via qualified columns

Sometimes you need un-aggregated rows from a data object — the OBML
"raw mode" shape. Trigger it by writing **qualified** `"DataObject"."column"`
references in SELECT:

```sql
SELECT "Customers"."Customer Name", "Customers"."Country"
FROM   sales_model
WHERE  "Customers"."Country" = 'US'
ORDER  BY "Customers"."Country"
LIMIT  100
```

Compiles to a `QueryObject` with `select.fields=[...]` (no aggregation,
no joins). Detection rule:

- Every SELECT item must be `"<DataObject>"."<column>"`
- WHERE predicates target qualified columns the same way
- `DISTINCT` is honoured (`SELECT DISTINCT "Customers"."Country" FROM ...`)
- `HAVING`, `GROUP BY`, and `WITH ROLLUP` are **rejected** in raw mode
- Mixing a qualified raw column with a bare dim/measure → `MIXED_RAW_AND_AGGREGATE_MODE`

Raw mode is the OBSQL equivalent of REST `/query/execute` with
`select.fields=[...]`. It bypasses the dim/measure abstraction but stays
inside the semantic layer — model-defined row-level filters still
apply, no joins, no warehouse-side ad-hoc SQL.

## Rejected SQL

The translator rejects shapes that don't fit the semantic model with
stable error codes:

| Code | Triggered by |
|------|--------------|
| `UNKNOWN_SELECT_ITEM` | SELECT item that's not a known dim / measure / metric. |
| `UNKNOWN_FILTER_FIELD` | WHERE / HAVING field that's not a known dim / measure / metric. |
| `UNKNOWN_ORDER_BY_FIELD` | ORDER BY identifier missing from SELECT. |
| `INVALID_ORDER_BY_POSITION` | Numeric position outside `[1, n]`. |
| `UNSUPPORTED_SQL_FEATURE` | JOIN, CTE, subquery, UNION, window function, `SELECT *`, aggregate call wrapped around a measure, top-level `OR`. |
| `RAW_SQL_REJECTED` | Flight: FROM target is not the virtual table and not a catalog source. Raw warehouse SQL is **never** accepted (no flag to bypass). |
| `WRITE_OPERATION_REJECTED` | Flight or REST: `INSERT` / `UPDATE` / `DELETE` / `DROP` / `CREATE` / `ALTER` / `TRUNCATE` / `MERGE` / `GRANT` / `REVOKE` / `COMMIT` / `ROLLBACK`. OBSL is read-only. |
| `MIXED_RAW_AND_AGGREGATE_MODE` | SELECT mixes qualified raw-mode columns (`"DataObject"."column"`) with bare dim/measure labels. Use one form consistently. |

## Hierarchical subtotals: `WITH ROLLUP` / `WITH CUBE`

Add a trailing modifier to compute hierarchical subtotals (`ROLLUP`) or
the full cross-tab (`CUBE`):

```sql
SELECT "Region", "Country", "Total Sales"
FROM   sales_model
WHERE  "Year" = 2025
WITH ROLLUP
```

The compiler emits the dialect-appropriate form:

| Dialect | Emitted SQL |
|---------|-------------|
| Postgres, Snowflake, Databricks, DuckDB, Dremio, BigQuery, MySQL | `GROUP BY ROLLUP(a, b)` / `GROUP BY CUBE(a, b)` |
| ClickHouse | `GROUP BY a, b WITH ROLLUP` / `GROUP BY a, b WITH CUBE` |

For every selected dimension, a `GROUPING(dim) AS _g_<dim>` column is
appended to the result schema. `0` means the row carries a real value for
that dimension; `1` means it was rolled up (NULL value in the dim column).
The flag columns are the only reliable way to tell a subtotal row from a
detail row whose dim is legitimately NULL.

```sql
-- Keep only the country-level subtotals
WHERE _g_Region = 0 AND _g_Country = 1
```

### Measure additivity under rollup

OBSL doesn't classify measures by additivity or rewrite their SQL. The
database recomputes each grouping level from base rows:

- **Additive measures** (`SUM`, `COUNT`): subtotals sum to higher levels and to the grand total.
- **Non-additive measures** (`COUNT(DISTINCT)`, `AVG`, percentiles, ratios defined as `AVG(x/y)`): each grouping level is individually correct, but subtotals **do not sum** to the grand total. Mathematically expected; not a bug.
- **Weighted ratios** (`SUM(x) / SUM(y)`): roll up correctly at every level.

### Restrictions

- `WITH ROLLUP` / `WITH CUBE` require at least one dimension in `SELECT`.
- The two are mutually exclusive.
- Combining rollup/cube with `total: true` measures, period-over-period
  metrics, or cumulative metrics emits an `INCOMPATIBLE_COMBINATION`
  warning. The query still runs but the `_g_*` flag columns may not
  appear in the final projection.

## REST examples

### Execute

```bash
curl -X POST http://localhost:8000/v1/query/semantic-ql \
  -H 'content-type: application/json' \
  -d '{
    "sql": "SELECT \"Region\", \"Total Sales\" FROM sales_model WITH ROLLUP",
    "dialect": "duckdb"
  }'
```

The response shape matches `/v1/query/execute`: `sql`, `dialect`,
`columns`, `rows`, `explain`, plus the freshness-cache metadata.

### Compile-only (debugging)

```bash
curl -X POST http://localhost:8000/v1/query/semantic-ql/compile \
  -H 'content-type: application/json' \
  -d '{ "sql": "SELECT \"Region\", \"Total Sales\" FROM sales_model" }'
```

The compile response also includes the **translated `QueryObject` JSON**
under `query`, so you can see exactly what your SQL became.

## Arrow Flight SQL

The Flight server classifies every incoming SQL by its first `FROM`
target:

| FROM target | Mode | Behavior |
|-------------|------|----------|
| `<model_name>` (virtual table) | `semantic` | Translated → compiled → executed |
| `SHOW TABLES`, `DESCRIBE`, `information_schema.*`, `pg_catalog.*`, `SELECT version()` / `current_schema()` / `SELECT 1` | `catalog` | Answered from the **model**; **never** touches the warehouse |
| **anything else** | `rejected` | `RAW_SQL_REJECTED` — no flag to bypass |
| DDL/DML (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, `TRUNCATE`, …) | `rejected` | `WRITE_OPERATION_REJECTED` — OBSL is read-only by design |

### Governance — closed by design

**There are no env flags to enable raw SQL or write operations.** OBSL is
a semantic layer, not a JDBC proxy. The only thing the warehouse ever
receives is SQL produced by the OBSL compiler:

| Source | What reaches the warehouse |
|---|---|
| Semantic QL via Flight | Compiled SQL from `CompilationPipeline` |
| `QueryObject` via REST `/query/execute` | Compiled SQL from `CompilationPipeline` |
| OBML YAML via Flight | Compiled SQL from `CompilationPipeline` |
| Catalog discovery (SHOW / DESCRIBE / information_schema / pg_catalog) | **Nothing** — answered from the model in-process |
| Anything else | **Nothing** — rejected at the door |

Operators cannot accidentally open a hole. There is no "raw SQL" mode,
no admin override, no escape hatch. If you need direct warehouse access,
use the warehouse's own clients — not OBSL.

The Flight catalog (`CommandGetTables`, `CommandGetColumns`,
`ListFlights`) lists the **semantic virtual table first** with its
dimension / measure / metric columns, plus the `_dimensions`,
`_measures`, and `_metrics` metadata views. Data-object physical
columns are not exposed.

### Schema probe shortcut

When a Flight client calls `GetFlightInfo` for a semantic-mode query, the
result schema is built directly from the model — no warehouse round-trip
is needed to learn the column types. Faster catalog navigation; no
spurious `EXPLAIN`-shaped queries hit the database.

## BI tool setup

The general recipe across BI tools that speak Flight SQL:

1. Install the Apache Arrow Flight SQL JDBC `.jar`.
2. Connect to `jdbc:arrow-flight-sql://<host>:8815?useEncryption=false`.
3. Browse the schema — you'll see one table per loaded model, with
   columns labelled by dimension / measure / metric.
4. In the SQL editor, write Semantic QL against that virtual table:

```sql
SELECT "Region", "Total Sales"
FROM   sales_model
WHERE  "Year" = 2025
ORDER  BY "Total Sales" DESC
LIMIT  100
```

The semantic layer takes care of every join, every aggregate, every row-
level rule. BI tools see exactly the columns they're allowed to combine.
