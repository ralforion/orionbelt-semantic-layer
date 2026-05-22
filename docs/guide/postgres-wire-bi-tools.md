# Connecting BI tools via the Postgres wire surface

OrionBelt's Postgres wire surface (`PGWIRE_ENABLED=true`) lets any
Postgres-compatible client query a loaded OBSL model — without
installing a custom JDBC driver, ODBC bridge, or Arrow Flight client.
Most BI tools ship a built-in Postgres connector that "just works".

This page is a step-by-step manual checklist for the four tools
covered by Step 5 of the
[Postgres wire plan](https://github.com/ralfbecher/orionbelt-semantic-layer/blob/main/design/PLAN_postgres_wire.md):
DBeaver, Tableau Desktop, Power BI Desktop, and Metabase.

## The Semantic Loop — Dremio + OrionBelt

![Dremio + OrionBelt Semantic Layer — the Full Circle: Dremio (Query Engine) → Postgres wire → OBSL → Dremio (Execution Engine)](../assets/dremio-orionbelt-full-circle.png)

Dremio is the flagship pgwire consumer in v2.5: register OBSL as a
Postgres source, and Dremio's federation engine pushes queries down
over the wire. With a Dremio-backed OBSL model (OBML
``settings.defaultDialect: dremio``) the loop closes — Dremio
queries OBSL via Postgres, OBSL compiles to Dremio's SQL dialect,
ob-dremio's Arrow Flight driver streams the result back through
Dremio's own execution engine. Governed semantics, no extra hop.

## 1. Common configuration

Start the OBSL server with the wire surface enabled:

```bash
DB_VENDOR=duckdb \
DUCKDB_DATABASE=examples/orionbelt_1_commerce.duckdb \
QUERY_EXECUTE=true \
PGWIRE_ENABLED=true \
PGWIRE_PORT=5432 \
MODEL_FILES=examples/orionbelt_1_commerce.yaml \
uv run orionbelt-api
```

Every client uses the same connection details:

| Setting | Value |
|---|---|
| Host | the machine running `orionbelt-api` (`localhost` for local dev) |
| Port | `PGWIRE_PORT` (default `5432`) |
| Database | the model addressing name — the OBML `name:` field or, lacking that, the file stem (e.g. `orionbelt_1_commerce`) |
| Username | any non-empty string (`obsl`, the tool's default — ignored in `trust` mode) |
| Password | leave empty in `trust` mode (auth lands in Step 6) |
| TLS / SSL | disable; the server has no built-in TLS today |

To list available model names, query the REST `GET /v1/models`
endpoint or check the server startup log.

## 2. DBeaver

DBeaver Community speaks pgwire natively via its bundled PostgreSQL
JDBC driver.

1. **New Connection → PostgreSQL.** Fill in host, port, database
   (the model name), username `obsl`, no password.
2. **Driver properties → `sslmode = disable`.** OBSL doesn't ship TLS;
   the default `sslmode=prefer` will silently fall back.
3. **Test connection.** DBeaver issues a flurry of pg_catalog probes
   immediately. Expect success.
4. **Schema browser → "main" schema → Tables.** You should see one
   entry per loaded model. Right-click → "View data" returns rows.
5. **SQL editor.** Try:

    ```sql
    SELECT "Sales Year", "Total Sales"
    FROM orionbelt_1_commerce
    LIMIT 10;
    ```

| Behaviour | Expected |
|---|---|
| Schema tree populates | ✅ |
| Column types displayed correctly | ✅ (via `information_schema.columns`) |
| Bind-parameterised query (DBeaver "Generate SELECT * LIMIT 100") | ✅ (extended-query protocol, Step 4) |
| `\d`-style metadata dialog | ⚠ partial — DBeaver may fall back to information_schema (works) |

## 3. Tableau Desktop

Tableau's PostgreSQL connector lives under **Connect → To a Server →
PostgreSQL**.

1. Enter the connection details from §1.
2. Tableau may probe `pg_catalog.pg_class` and `information_schema.tables`
   at connect time — these are answered by the catalog emulator.
3. Once connected, drag the model table from the left panel onto the
   canvas to use it as a data source.
4. Drag a dimension (e.g. `Sales Year`) to *Rows* and a measure
   (e.g. `Total Sales`) to *Columns*. A bar chart renders.

| Behaviour | Expected |
|---|---|
| Connect succeeds | ✅ |
| Table list populates | ✅ |
| Live (non-extract) data refresh | ✅ |
| Custom SQL with parameters | ⚠ depends on the SQL — only `SELECT dim, measure FROM model` shapes round-trip |

## 4. Power BI Desktop (Windows only)

1. **Get Data → PostgreSQL database.** Server: `host:port`. Database:
   the model name.
2. Authentication: **Database** tab, leave password empty.
3. After connecting, the Navigator shows the model table. Tick it and
   load.
4. Build a visual — drag the dimension and measure onto a chart.

Power BI defaults to *Import* mode. *DirectQuery* will run live
queries through pgwire on every interaction — works, but every chart
becomes a network round-trip.

## 5. Metabase

Self-hosted Metabase ships a Postgres connector.

1. **Admin → Databases → Add database → PostgreSQL.**
2. Fill in the connection details from §1. Leave SSL disabled.
3. Metabase scans `information_schema` at connect time to populate the
   "Data Reference" panel.
4. Build a Question via the GUI — the table appears under the
   database; pick a dimension + measure aggregation. Metabase
   generates `SELECT "Dimension", AGG("Measure") FROM table GROUP BY 1`,
   which OBSL routes through OBSQL.

| Behaviour | Expected |
|---|---|
| Database scan succeeds | ✅ |
| Question builder lists columns | ✅ |
| Aggregation queries return rows | ✅ |
| Custom-SQL questions with raw SQL | ⚠ only OBSL-shaped SQL works |

## Known limitations

These constraints are documented in
[design/PLAN_postgres_wire.md §10](https://github.com/ralfbecher/orionbelt-semantic-layer/blob/main/design/PLAN_postgres_wire.md):

| Limitation | Reason | Workaround |
|---|---|---|
| `psql \d <table>` partially works (psql 16 RLS-policy probe hits DuckDB's correlated-UNNEST limit) | DuckDB engine, not the wire protocol | Use BI tools (they query `information_schema`) or `\dt` |
| Binary-format Bind parameters rejected | Step 4 ships text format only | Force text format if a driver supports it; binary lands in Step 7 |
| No authentication | `trust` mode only until Step 6 lands | Run behind a network boundary or skip pgwire on public deploys |
| No TLS | Native TLS comes in a later step | Front with nginx / Cloud Run TLS termination |
| Write operations (`INSERT` / `UPDATE` / `DELETE` / DDL) | Read-only semantic layer | Use the REST API for model management; data writes go to the warehouse, not OBSL |

## Reporting a tool that fails

Catalog probes that OBSL doesn't understand log a single warning per
unique SQL shape:

```
WARNING ... PGWIRE_CATALOG_PROBE_UNHANDLED dialect=duckdb error=...
```

If a BI tool fails to connect, scrape that line out of the server log
and open an issue against
[orionbelt-semantic-layer](https://github.com/ralfbecher/orionbelt-semantic-layer/issues)
with the captured SQL. We extend the catalog rewriter (see
`src/orionbelt/pgwire/catalog.py`) per-tool until it lights up.
