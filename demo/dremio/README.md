# OrionBelt + Dremio: the semantic sidecar demo

A self-contained, one-command demo that shows OrionBelt acting as a **governed
semantic layer in front of Dremio**. Dremio talks to OrionBelt over the
Postgres wire protocol as if it were an ordinary database; OrionBelt compiles
the request into Dremio SQL and pushes it **back into Dremio** over Arrow
Flight, executing against the very same Parquet datasets you can query raw.

```
 Dremio SQL editor
    │  (1) one pgwire source  ──►  OrionBelt API
    ▼
 OrionBelt  ── compiles to Dremio dialect ──┐
    │                                       │ (Arrow Flight, ob_dremio)
    └──────────────── results ◄─────────────┘
                                     Dremio executes against its own
                                     S3 / MinIO Parquet datasets
                                                  ▲
                              (you also query that Parquet RAW, to compare)
```

Everything runs locally in four containers. No cloud, no credentials.

## What's in the box

| Service | Port | Role |
|---|---|---|
| `dremio` | http://localhost:19047 | Dremio OSS - the SQL engine and UI |
| `obsl` | http://localhost:18080 | OrionBelt API (single model), pgwire on `:15432` |
| `ui` | http://localhost:17860 | OrionBelt playground (model, ER diagram, queries) |
| `minio` | http://localhost:19001 | S3 object store holding the commerce Parquet |

The data is the bundled `orionbelt_1_commerce` dataset (15 tables) exported
from DuckDB to Parquet and served from MinIO. The OrionBelt model is the same
commerce semantic model, re-pointed at the Dremio S3 datasets
(`settings.defaultDialect: dremio`).

## Run it

```bash
demo/dremio/run-demo.sh
```

That builds the Parquet + model, starts the stack, waits for Dremio's cold
start (~30-60 s), and bootstraps Dremio (S3 source, dataset promotion, one
pgwire source, and the `governed` Space of curated views). It finishes by
printing a raw-vs-governed comparison.

Re-runs are fast and idempotent. Skip the image build with
`NO_BUILD=1 demo/dremio/run-demo.sh`. Tear everything down with
`DOWN=1 demo/dremio/run-demo.sh`.

Credentials (local demo only):
- Dremio: `obsl_admin` / `obsl_admin_pw_123!`
- MinIO: `minioadmin` / `minioadmin`

## The demo flow (in the Dremio SQL Runner)

### 1. Raw lakehouse SQL over the Parquet

Show that Dremio reads the Parquet directly from S3 (MinIO):

```sql
SELECT co.countryname, SUM(s.salesamount) AS total_sales
FROM lake.commerce.sales s
JOIN lake.commerce.clients   c  ON s.salesclient = c.clientid
JOIN lake.commerce.countries co ON c.clientcountryid = co.countryid
GROUP BY co.countryname
ORDER BY total_sales DESC
LIMIT 5;
```

### 2. The same answer, governed - federated through OrionBelt

```sql
SELECT "Country Name", "Total Sales"
FROM obsl.commerce.model
ORDER BY "Total Sales" DESC
LIMIT 5;
```

Identical numbers. But this query never spelled out a join or an aggregation:
it asked for a **business dimension and a business measure**. OrionBelt resolved
the join path, applied the measure definition, compiled Dremio SQL, and pushed
it back into Dremio via Flight. One governed definition of "Total Sales",
consumed by any Postgres-speaking tool.

### 3. The differentiator - cross-fact without the fan-trap

Ask for two measures from two different fact tables at once:

```sql
SELECT "Year Month", "Total Sales", "Total Shipments"
FROM obsl.commerce.model
ORDER BY "Year Month"
LIMIT 12;
```

A naive `sales JOIN shipments` in raw SQL **double-counts** (the classic
fan-trap). OrionBelt detects the independent facts and compiles a
**Composite Fact Layer** (`UNION ALL` with NULL padding) so each measure
aggregates against its own grain - correct by construction. Show the compiled
SQL in the OrionBelt playground (`Compile SQL`) to make the point.

### 4. A governed metric, defined once

```sql
SELECT "Channel Name", "Total Sales", "Average Sale"
FROM obsl.commerce.model
ORDER BY "Total Sales" DESC;
```

`Average Sale` is a metric (`Total Sales / Sales Order Count`) defined in the
model, not in the query. Single-fact metrics like this push down cleanly
through Dremio's federation.

### 5. Filters push down through Dremio

```sql
SELECT "Client Name", "Total Sales"
FROM obsl.commerce.model
WHERE "Country Name" = 'Singapore'
ORDER BY "Total Sales" DESC LIMIT 5;
```

Dremio's Postgres connector rewrites a `WHERE` into a derived-table wrapper
(`SELECT ... FROM (SELECT ... FROM model) WHERE ...`). OrionBelt detects that
trivial wrapper and flattens it back into a semantic query, so dimension
filters (`WHERE`) and measure filters (`HAVING`) both work through federation.

### 6. Period-over-period, through federation

```sql
SELECT "Sales Month", "Total Sales", "Sales MoM Change"
FROM obsl.commerce.model
ORDER BY "Sales Month" LIMIT 12;
```

A window metric: OrionBelt builds a date spine and a self-join under the hood;
the consumer just asks for `Sales MoM Change`. You can combine offsets too -
`Sales MoM Change` and `Sales YoY Growth` in one query each get their own
prior-period join (they just have to share the time dimension and base grain).

### 7. Cross-fact derived metrics, through federation

```sql
SELECT "Product Category", "Total Sales", "Return Rate", "Gross Margin"
FROM obsl.commerce.model
ORDER BY "Total Sales" DESC LIMIT 5;
```

`Return Rate` (Returns / Sales) and `Gross Margin` (Sales - Cost) each combine
measures from different fact tables. OrionBelt computes the components inside a
Composite Fact Layer and projects only the requested columns - one governed
definition, no hand-written multi-fact SQL.

## Saved as Dremio views

The bootstrap also saves each curated query as a **Dremio view** in a Space
called `governed`, so you can browse and query them by name instead of pasting
SQL. A note on *where* they live: Dremio forbids creating a view inside a
source (`CREATE VIEW obsl.commerce.…` fails with *"Cannot create view in …"*) -
views are virtual datasets and belong in a **Space** or a user's home. So the
demo puts them in the `governed` Space, each referencing `obsl.commerce.model`.

When you `SELECT` from a governed view, Dremio wraps the view body in a
derived table and pushes it down to OrionBelt (including any inner
`WHERE` / `ORDER BY` / `LIMIT`); OrionBelt flattens that wrapper back into a
flat semantic query before compiling.

| View (`governed.…`) | Maps to | Shows |
|---|---|---|
| `raw_top_countries` | A1 | raw lakehouse SQL over `lake` (no OrionBelt) |
| `top_countries_by_sales` | A2 | same answer, governed |
| `clients_in_singapore` | A3a | dimension filter -> `WHERE` |
| `countries_over_1m` | A3b | measure filter -> `HAVING` |
| `sales_vs_shipments` | A4 | cross-fact, Composite Fact Layer |
| `avg_sale_by_channel` | A5 | governed metric |
| `sales_period_over_period` | A6 | MoM + YoY window metrics |
| `category_margin` | A7 | cross-fact derived metrics |

(Eight views for seven curated queries: A3 keeps both filter variants - `WHERE`
on a dimension and `HAVING` on a measure.) Try one:

```sql
SELECT * FROM governed.category_margin;
```

## The OrionBelt playground (http://localhost:17860)

Because OrionBelt runs in single-model mode here, the playground shows the
**loaded model read-only** and still compiles and executes queries against it,
alongside the Mermaid ER diagram and the RDF/ontology graph. Good for showing
the model itself next to the Dremio federation story.

See `demo-queries.sql` for the full curated, run-ordered list.

## How it's wired

- `build_assets.py` - exports the DuckDB seed to `parquet/commerce/<table>/`
  and generates `model/commerce_dremio.yaml` (the commerce model re-pointed at
  `"lake"."commerce".<table>` with `defaultDialect: dremio`).
- `docker-compose.yml` - the four services; an `mc-init` sidecar uploads the
  Parquet into MinIO via the S3 API (the erasure-coded backend won't serve
  files dropped straight onto the drive).
- `bootstrap.py` - registers the MinIO S3 source, promotes each Parquet folder
  as a dataset, registers the single pgwire source, creates the `governed` Space
  with the curated views, and runs the comparison.

OrionBelt reaches Dremio via Flight using the `DREMIO_HOST` / `DREMIO_PORT` /
`DREMIO_USERNAME` / `DREMIO_PASSWORD` env vars on the `obsl` service.

## What this demonstrates

- **OrionBelt is a thin governance layer, not another copy of the data.** The
  data stays in the lakehouse; OrionBelt pushes compute back to Dremio over
  Arrow Flight.
- **Any Postgres client gets the semantic layer for free** - no special driver.
  Here Dremio is both the consumer (federation) and the executor (Flight).
- **Metrics are defined once and stay consistent across tools.** "Total Sales"
  lives in the model; join-path selection, fan-trap handling, and dialect
  translation are the engine's job.
- **One model, eight dialects.** The same model runs on Snowflake, BigQuery,
  Databricks, Postgres, etc. - here it's Dremio.
- **Freshness-driven result cache.** Enabled via `CACHE_BACKEND=file`. Repeated
  queries are served from cache over both the REST/playground path and the
  pgwire surface (run a query twice, then `curl localhost:18080/v1/cache/stats`).
  Arrow Flight has a separate streaming execution path and is not yet cached.
