"""OBSQL reference text — served via the REST API reference endpoint.

The reference is a single, agent-friendly markdown string covering the
OBSQL grammar in enough detail for an LLM / MCP client / BI tool to
construct valid queries without scraping ``/docs`` or visiting the
website. Keep it in sync with ``docs/guide/semantic-ql.md`` — that file
is the user-facing version; this one is the compact API-served version.
"""

from __future__ import annotations

OBSQL_REFERENCE = """\
# OrionBelt Semantic QL (OBSQL) Reference

OBSQL is OrionBelt's natural SQL surface. BI-style SQL written against a
per-model **virtual table**, translated to a ``QueryObject`` and compiled
through the standard pipeline.

* Brand short form: **OBSQL** (OrionBelt Semantic Query Language).
* Available on REST (``POST /v1/query/semantic-ql``) and Arrow Flight SQL.
* OBML is the modeling language; OBSQL is the query language.

## 1. Virtual table shape

Every loaded model exposes **one** virtual table named ``<model_id>``.
Columns are the union of dimensions, measures, and metrics. There is no
``JOIN`` syntax — the model defines all joins; the compiler emits them.

## 2. Aggregate-mode grammar

```
SELECT <ident_list>
FROM   <model_id>
[WHERE <predicates>]
[HAVING <predicates>]
[GROUP BY <ignored>]
[ORDER BY <alias | position> [ASC | DESC]]
[LIMIT <int>]
[WITH ROLLUP | WITH CUBE]
```

### SELECT items

Three accepted forms, all compile to the same vendor SQL:

1. **Bare label**: ``"Customer Country"``, ``"Total Sales"``
2. **MEASURE marker**: ``MEASURE("Total Sales")`` — matches Snowflake
   ``SEMANTIC_VIEW`` and Databricks metric-view syntax.
3. **Matching aggregate wrapper**: ``SUM("Total Sales")`` accepted **iff**
   ``"Total Sales"`` is a ``sum`` measure; ``COUNT(DISTINCT "X")``
   accepted iff ``"X"`` is a ``count_distinct`` measure. Mismatched
   wraps reject with the declared aggregation named in the error.

Metrics reject any aggregate wrapper — they're derived expressions
already at the query grain.

### WHERE / HAVING

* AND-chained ``<column> <op> <literal>`` predicates.
* Operators: ``=``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, ``IN (...)``,
  ``IS NULL``, ``IS NOT NULL``, ``LIKE``, ``ILIKE``.
* References to **measures or metrics** in WHERE auto-route to HAVING.
* Top-level OR rejects with ``UNSUPPORTED_SQL_FEATURE`` — use ``IN (...)``
  or split into two queries.

### GROUP BY

Silently accepted but ignored — implicit from the dimensions in SELECT.

### ORDER BY

* By alias (must match a SELECT item)
* By 1-based position (``ORDER BY 2 DESC``)

### LIMIT

Integer literal.

### WITH ROLLUP / WITH CUBE

Trailing modifier or ``GROUP BY ROLLUP(...)`` / ``CUBE(...)`` function
form, all map to ``query.grouping``. Adds ``GROUPING(dim) AS _g_<dim>``
flag columns so callers can distinguish subtotal/grand-total rows from
detail rows with NULL dims.

```sql
SELECT "Region", "Country", "Total Sales"
FROM   sales_model
WHERE  "Year" = 2025
WITH ROLLUP
```

ClickHouse uses the trailing form ``GROUP BY ... WITH ROLLUP``; every
other dialect emits ``GROUP BY ROLLUP(...)``.

## 3. Raw mode — qualified columns

If **every** SELECT item is a qualified ``"DataObject"."column"``
reference, OBSQL emits ``QuerySelect.fields=[...]`` for un-aggregated
detail rows:

```sql
SELECT "Customers"."Customer Name", "Customers"."Country"
FROM   sales_model
WHERE  "Customers"."Country" = 'US'
ORDER  BY "Customers"."Country"
LIMIT  100
```

Rules:

* WHERE predicates target qualified columns the same way.
* HAVING, GROUP BY, WITH ROLLUP — all reject in raw mode.
* DISTINCT honoured (``SELECT DISTINCT "Customers"."Country" FROM ...``).
* Mixing a qualified raw column with a bare dim/measure label →
  ``MIXED_RAW_AND_AGGREGATE_MODE``.

## 4. Catalog mode — discovery, never touches the warehouse

The following are answered from the **model** server-side:

* ``SHOW TABLES`` / ``SHOW COLUMNS`` / ``DESCRIBE <table>``
* ``SELECT * FROM information_schema.tables``
* ``SELECT * FROM information_schema.columns``
* ``SELECT * FROM pg_catalog.pg_class`` / ``.pg_attribute``
* ``SELECT * FROM _dimensions`` / ``_measures`` / ``_metrics``
* ``SELECT 1``, ``SELECT version()``, ``SELECT current_schema()``
* ``USE`` / ``SET`` accepted as no-ops

Unknown catalog probes return empty result sets — tools adapt.

## 5. Hard rejections (no flag to bypass)

| Code | Trigger |
|---|---|
| ``UNKNOWN_SELECT_ITEM`` | SELECT label not a dim, measure, or metric |
| ``UNKNOWN_FILTER_FIELD`` | WHERE / HAVING field not a known column |
| ``UNKNOWN_ORDER_BY_FIELD`` | ORDER BY identifier not in SELECT |
| ``INVALID_ORDER_BY_POSITION`` | Numeric position outside 1..n |
| ``UNSUPPORTED_SQL_FEATURE`` | JOIN, CTE, subquery, UNION, window, ``SELECT *``, mismatched aggregate wrap, metric wrapped in aggregate, top-level OR |
| ``MIXED_RAW_AND_AGGREGATE_MODE`` | Raw qualified columns mixed with bare aggregate labels |
| ``RAW_SQL_REJECTED`` | FROM target is neither the virtual table nor a catalog source |
| ``WRITE_OPERATION_REJECTED`` | DDL / DML / TCL — OBSL is read-only |

## 6. Multi-model addressing (v2.4.0+)

When the server is started with ``MODEL_FILES=sales.yaml,returns.yaml``
each model is exposed as its own Flight SQL catalog. BI tools and
clients pick which model to query:

| Protocol | How to select |
|---|---|
| **Arrow Flight SQL** | gRPC ``database`` metadata header (set by JDBC ``Connection.setCatalog()`` or the URL path). DBeaver / Tableau / Power BI: type the model name in the "Database" field. |
| **Pyarrow Flight** | ``flight.FlightCallOptions(headers=[(b'database', b'sales')])`` |
| **pgwire** (v2.5.0+) | ``postgresql://obsl:KEY@host:5432/sales`` — the URL ``database=`` slot |
| **REST** | ``POST /v1/sessions/<model_name>/query/semantic-ql`` — session id == model name |

Resolution order on the server:

1. Explicit selector from header / URL → that model
2. Legacy ``__default__`` (from ``MODEL_FILE``)
3. Auto-resolve when exactly one model is loaded — no selector required
4. Otherwise: rich error listing available model names and how to set
   the selector for each client type

### Model name rules

* Defined by the OBML top-level ``name:`` field, falling back to the
  filename stem.
* Normalized: lowercase, runs of ``[whitespace | . | -]`` → underscore,
  collapse underscore runs, strip leading/trailing underscores, strip a
  trailing ``_obml`` suffix.
* Validated against ``^[a-z][a-z0-9_]{0,62}$``.
* Reserved names (rejected at startup): ``obsl, obml, obsql, model,
  default, public, information_schema, pg_catalog, sqlite_master,
  mysql, sys, performance_schema, admin, root``.

### Discovery

``GET /v1/models`` returns the live catalog of loaded models with their
descriptions and counts of declared dims / measures / metrics / data
objects. Stable across the server's lifetime — BI tool configs can
hard-code names without worrying about drift.

## 7. Endpoints

| Method | Path | Behaviour |
|---|---|---|
| POST | ``/v1/query/semantic-ql`` | Translate + compile + execute (shortcut, auto-resolves session/model) |
| POST | ``/v1/query/semantic-ql/compile`` | Translate + compile only, returns SQL + translated ``QueryObject`` JSON |
| POST | ``/v1/sessions/{id}/query/semantic-ql`` | Session-scoped execute |
| POST | ``/v1/sessions/{id}/query/semantic-ql/compile`` | Session-scoped compile |
| Flight | gRPC port 8815 | Send OBSQL as a regular SQL statement; same translation path |

## 8. Worked examples

### Aggregate
```sql
SELECT "Region Name", "Total Sales"
FROM   sales_model
WHERE  "Sales Date" >= '2025-01-01'
ORDER  BY "Total Sales" DESC
LIMIT  20
```

### Rollup with flag columns
```sql
SELECT "Region", "Country", "Total Sales"
FROM   sales_model
WITH ROLLUP
```
Result schema gains ``_g_Region`` and ``_g_Country`` (0 = detail value,
1 = rolled up).

### Raw mode
```sql
SELECT "Customers"."Customer Name", "Customers"."Email"
FROM   sales_model
WHERE  "Customers"."Country" = 'US'
ORDER  BY "Customers"."Customer Name"
LIMIT  500
```

### Catalog
```sql
SHOW TABLES;
DESCRIBE sales_model;
SELECT * FROM information_schema.columns WHERE table_name = 'sales_model';
```
"""
