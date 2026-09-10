---
description: Query the OrionBelt Semantic Layer from a plain DuckDB shell, over the Postgres wire surface or Arrow Flight SQL, and join governed measures to local Parquet and CSV.
---

# Using DuckDB as a client

DuckDB is one of the eight warehouses OBSL compiles *to*. It can also sit in
front of OBSL and query the model, which is a different and often more useful
thing: measures stay governed and resolved by the semantic layer, and the rows
that come back are an ordinary DuckDB relation you can join to a local Parquet
file, aggregate further, or persist.

There are two routes, and they differ in how you *address* the model rather
than in what you get back.

| | Postgres wire (`ATTACH`) | Arrow Flight SQL (`adbc_scan`) |
|---|---|---|
| Addressing | `FROM obsl.sales.model` - a real table | `adbc_scan(handle, 'SELECT ... FROM sales')` - OBSQL in a string |
| Extension | `postgres` (ships with DuckDB) | `adbc_scanner` (community) |
| Extra setup | one `SET`, one `ATTACH` | a filesystem path to the Flight SQL driver library |
| Transport | Postgres text protocol | Arrow, end to end |
| `SELECT *` | works - DuckDB expands the star from the catalog | rejected - the star reaches OBSQL verbatim |
| Server port | `PGWIRE_PORT` (5432) | `FLIGHT_PORT` (8815) |

Use `ATTACH` when you want the model to look like a table and compose with the
rest of your SQL. Use `adbc_scan` when the result is large enough that keeping
Arrow end to end is worth the extra setup.

Both are read-only. OBSL is a semantic layer: `INSERT` / `UPDATE` / `DELETE` and
DDL are refused on every surface.

## Route 1: `ATTACH` over the Postgres wire

Start the server with the wire surface enabled (see
[Postgres Wire (BI Tools)](postgres-wire-bi-tools.md#1-common-configuration)),
then, in any DuckDB shell:

```sql
INSTALL postgres;
LOAD postgres;

SET pg_use_text_protocol = true;   -- required, see below

ATTACH 'host=127.0.0.1 port=5432 dbname=commerce user=obsl'
    AS obsl (TYPE postgres, READ_ONLY);
```

`dbname` is the model's addressing name: the OBML `name:` field, or the file
stem if the model does not declare one. Each model is a schema under the
attached catalog, with the model itself at `<schema>.model`:

```sql
SELECT database, schema, name FROM (SHOW ALL TABLES) WHERE database = 'obsl';
```

```text
obsl   commerce   model                   -- the model: dimensions + measures + metrics
obsl   commerce   dimensions              -- metadata views, one row per artefact
obsl   commerce   measures
obsl   commerce   metrics
obsl   commerce   _dimensions_metadata    -- the same, with the full column detail
obsl   commerce   _measures_metadata
obsl   commerce   _metrics_metadata
```

Then query it as a table. Columns are the model's dimensions, measures and
metrics, typed as the model declares them:

```sql
SELECT "Country Name", "Total Sales"
FROM   obsl.commerce.model
WHERE  "Country Name" = 'Germany';
```

### `pg_use_text_protocol` is not optional

Left at its default, the extension reads data with
`COPY ... TO STDOUT (FORMAT binary)`. OBSL answers queries, not binary bulk
exports, so the read fails with a parse error naming `COPY`. The catalog
browses fine either way, which makes it look like a broken query rather than a
protocol OBSL does not speak. Set the flag once per session, before the first
read.

Two neighbouring settings need no change: `pg_use_ctid_scan` and
`pg_experimental_filter_pushdown` both work as-is, because a model has neither
physical row ids nor a plan the extension can push into.

### TLS and authentication

The extension is libpq underneath, so the whole connection string reaches it
and `sslmode`, `sslrootcert` and `password` behave exactly as they do for
`psql`. Against a listener started with `PGWIRE_TLS_CERT` / `PGWIRE_TLS_KEY`:

```sql
ATTACH 'host=obsl.internal port=5432 dbname=commerce user=obsl
        sslmode=verify-full sslrootcert=/etc/ssl/certs/obsl-ca.crt'
    AS obsl (TYPE postgres, READ_ONLY);
```

All five `sslmode` values negotiate (`disable`, `prefer`, `require`,
`verify-ca`, `verify-full`), and `verify-ca` against the wrong trust anchor is
refused, which is what makes the others mean anything.

With `AUTH_MODE=api_key`, the API key **is** the password:

```sql
ATTACH 'host=obsl.internal port=5432 dbname=commerce user=obsl
        password=obsl_pat_...  sslmode=require'
    AS obsl (TYPE postgres, READ_ONLY);
```

The mechanism is SCRAM-SHA-256 by default, which libpq performs on DuckDB's
behalf; `PGWIRE_AUTH_MODE=password` drops to cleartext for clients that lack
SCRAM, and DuckDB is not one of them. Send the key over TLS either way: with
SCRAM the key never crosses the wire, but the results do.

## Route 2: `adbc_scan` over Arrow Flight SQL

`adbc_scanner` is a **community** extension, and `adbc_connect` wants a
filesystem path to the Flight SQL driver library rather than a package name.
Any ADBC install has one:

```bash
python -c "import adbc_driver_flightsql as d; print(d._driver_path())"
```

```sql
INSTALL adbc_scanner FROM community;
LOAD adbc_scanner;

CREATE OR REPLACE TABLE h AS
SELECT adbc_connect(MAP {
    'driver': '/path/to/libadbc_driver_flightsql.so',
    'uri':    'grpc://127.0.0.1:8815'
}) AS handle;

SELECT * FROM adbc_scan(
    (SELECT handle FROM h),
    'SELECT "Country Name", "Total Sales" FROM commerce'
);
```

The string is [OBSQL](semantic-ql.md), and the model is addressed by name
(`commerce`), not as `<schema>.model` - that shape belongs to the wire surface.

What else the handle gives you:

| | |
|---|---|
| `adbc_tables(handle)` | Lists `model` and the `dimensions` / `measures` / `metrics` views |
| `adbc_schema(handle, 'model', schema := 'commerce')` | Column names and types without executing anything |
| `adbc_columns(handle)` | The same, across every table |

`adbc_scan_table(handle, '<table>')` does **not** work against OBSL. It builds
`SELECT * FROM <table>`, and OBSQL rejects `SELECT *`: a semantic query names
the dimensions and measures it wants, because "everything" is not a grain. Use
`adbc_scan` with an explicit column list.

## Where the work happens

The two routes differ here, and the `ATTACH` route is the more forgiving one.

**Over `ATTACH`**, DuckDB pushes simple predicates down into the query it sends,
and it does so through trivial wrappers too. Both of these arrive at the
semantic layer carrying the filter, so the warehouse does the work and only the
matching rows travel:

```sql
SELECT * FROM obsl.commerce.model WHERE "Country Name" = 'Germany';
SELECT * FROM (SELECT * FROM obsl.commerce.model) WHERE "Country Name" = 'Germany';
```

What does *not* get pushed is anything DuckDB cannot express as a scan filter:
a join to a local table, a window function, a predicate over a computed
expression. Those run in DuckDB on rows that have already arrived.

**Over `adbc_scan`**, there is no pushdown at all: the OBSQL string is sent
verbatim, and anything outside it is DuckDB's own work on rows that have
already arrived. Put selective predicates inside the string.

## Composing with local data

The point of either route. A governed measure joins to a local file:

```sql
SELECT m."Country Name",
       m."Total Sales",
       m."Total Sales" > t.target AS beat_target
FROM   obsl.commerce.model m
JOIN   read_csv('targets.csv') t ON m."Country Name" = t.country;
```

And lands in a local table:

```sql
CREATE TABLE snapshot AS SELECT * FROM obsl.commerce.model;
```

`SELECT count(*)` over the model returns exactly the number of rows
`SELECT *` returns. Note that this is not the same as the number of distinct
dimension combinations: asking for measures anchors the query to the facts, so
a dimension value with no facts behind it (a customer who has never ordered) is
listed by `SELECT "Customer Name"` but is not a row of the model.

## Known limitations

| Limitation | Route | Note |
|---|---|---|
| `SET pg_use_text_protocol = true` required | `ATTACH` | The default reads with binary `COPY`, which OBSL does not implement |
| `adbc_scan_table()` unsupported | `adbc_scan` | It generates `SELECT *`, which OBSQL rejects; name the columns |
| `SELECT *` unsupported inside an OBSQL string | `adbc_scan` | The `ATTACH` route is unaffected: DuckDB expands the star itself |
| Read-only | both | OBSL is a semantic layer; writes go to the warehouse, not through it |

## See also

- [Postgres Wire (BI Tools)](postgres-wire-bi-tools.md) - the same surface from Tableau, DBeaver, Power BI, Metabase and Dremio
- [ADBC / Arrow Flight SQL](adbc.md) - the Flight surface from Python, JDBC and ODBC
- [OBSQL](semantic-ql.md) - the query language both routes speak
