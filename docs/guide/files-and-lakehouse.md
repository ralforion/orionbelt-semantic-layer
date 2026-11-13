---
description: "Run a semantic model on Parquet, CSV and JSON files and on Apache Iceberg tables with no warehouse: a DuckDB data source reads them in place, within a memory limit you set."
---

# Files and Lakehouse Tables

A DuckDB [data source](data-sources.md) can read files and Iceberg tables where
they are: Parquet, CSV and JSON files on disk or in object storage, and Apache
Iceberg tables behind a REST catalog. No warehouse is needed. The DuckDB inside
the OrionBelt process runs the queries, so the model's dialect is `duckdb`.

| What you read | How a data object names it | Setup statements |
|---|---|---|
| Parquet, CSV or JSON files on disk | `code` is the path or a glob, `schema: ""` | none |
| Files in S3-compatible object storage | `code` is the `s3://` path or glob, `schema: ""` | a `CREATE SECRET` for the bucket |
| Iceberg tables in a REST catalog | `code` is the table, `schema` its namespace | `ATTACH` the catalog, `SET search_path` |
| An Iceberg table without a catalog | a view over `iceberg_scan(...)` | `CREATE OR REPLACE VIEW` |

## A source without a database file

Declare a DuckDB source with `:memory:` as its database:

```bash
OBSL_DATABASES=lake
DB_LAKE_TYPE=duckdb
DB_LAKE_DUCKDB_DATABASE=:memory:
```

Such a source keeps one DuckDB instance for as long as the server runs, and
each query runs on a connection of its own to it. The queries running at once
share the instance's memory limit, and what its setup statements attach stays
attached.

Set `:memory:` explicitly where `DUCKDB_DATABASE` is set for the whole server,
as the Docker image does: a named source falls back to the unprefixed variable.

## Files by path

Put the path in the data object's `code`, and leave `schema` empty:

```yaml
dataObjects:
  Orders:
    code: "/data/orders/*.parquet"
    schema: ""
    columns:
      Order Id:
        code: id
        abstractType: int
      Amount:
        code: amount
        abstractType: float
```

- The empty `schema` keeps the path whole. The DuckDB dialect names a table
  `"schema"."code"`, and with no schema the path alone is the name.
- A glob reads every file it matches as one table.
- DuckDB picks the reader by the file's extension: `.parquet`, `.csv`, `.json`.

For files in S3-compatible storage, give DuckDB the bucket's keys in the
source's setup statements, and use the `s3://` path in `code`:

```bash
DB_LAKE_DUCKDB_INIT_SQL=CREATE OR REPLACE SECRET lake_s3 (TYPE s3, KEY_ID 'AKIA...', SECRET '...', REGION 'eu-west-1')
```

DuckDB's [secrets](https://duckdb.org/docs/stable/configuration/secrets_manager)
cover the other ways to sign in, and Google Cloud Storage and Azure.

## Setup statements

`DUCKDB_INIT_SQL` holds statements DuckDB runs on every new connection of the
source, before the connection's query: on REST, the Postgres wire and Flight SQL
alike. Use them to sign in to object storage, attach a catalog, define views,
or set the search path.

| Rule | Why |
|---|---|
| They run on **every** connection | `SET search_path`, `USE` and `TEMP` views last only for the connection that ran them |
| Write them to run again: `ATTACH IF NOT EXISTS`, `CREATE OR REPLACE SECRET`, `CREATE OR REPLACE VIEW` | A source without a database file keeps its instance, so an `ATTACH` without `IF NOT EXISTS` fails on the second query |
| They are never shown | They can hold keys. They are not listed with the source, and an error names the variable rather than repeating the statements |
| Changing them is a different source | They decide what the source's tables are, so cached results of the old statements are not served for the new ones |

A statement that fails reports which variable holds it and what DuckDB said,
for example: `The setup statements of data source 'lake'
(DB_LAKE_DUCKDB_INIT_SQL) failed: Catalog Error: ...`.

## Iceberg tables in a REST catalog

Attach the catalog in the setup statements, and point the search path at one of
its namespaces:

```bash
OBSL_DATABASES=lake
DB_LAKE_TYPE=duckdb
DB_LAKE_DUCKDB_DATABASE=:memory:
DB_LAKE_DUCKDB_INIT_SQL="CREATE OR REPLACE SECRET lake_s3 (TYPE s3, KEY_ID 'admin', SECRET 'password', REGION 'us-east-1', ENDPOINT 'minio:9000', URL_STYLE 'path', USE_SSL false); ATTACH IF NOT EXISTS 'warehouse' AS lake (TYPE iceberg, ENDPOINT 'http://iceberg-rest:8181', AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none'); SET search_path = 'lake.sales'"
```

A data object's `schema` is the namespace and its `code` the table:

```yaml
dataObjects:
  Orders:
    code: orders
    schema: sales
    # ...
  Budgets:
    code: budget
    schema: finance
    # ...
```

- **Any namespace of the catalog.** The search path names one namespace, and
  DuckDB finds `finance.budget` in the same catalog too.
- **The newest snapshot.** Each query reads a table's newest snapshot: a commit
  made after the source attached the catalog is read by the next query.
- **Storage keys.** With `ACCESS_DELEGATION_MODE 'none'` DuckDB reads the data
  files with your own secret, rather than with credentials the catalog hands
  out.
- **Signing in to the catalog.** The example needs none. For a catalog that
  needs a token, see DuckDB's
  [Iceberg REST catalog](https://duckdb.org/docs/stable/core_extensions/iceberg/iceberg_rest_catalogs)
  documentation.

This setup was tested with Apache Iceberg's REST catalog and MinIO.

## An Iceberg table without a catalog

Define a view over `iceberg_scan` in the setup statements:

```sql
CREATE SCHEMA IF NOT EXISTS sales;
CREATE OR REPLACE VIEW sales.orders AS
  SELECT * FROM iceberg_scan('/warehouse/sales/orders', version = '?');
```

- `version = '?'` reads the newest metadata file DuckDB finds in the table's
  folder.
- A path to one `metadata.json` file reads that snapshot, and keeps reading it
  after later commits.

## Memory and spilling

| Variable | Default | What it sets |
|---|---|---|
| `DUCKDB_MEMORY_LIMIT` | 80% of the memory DuckDB sees | DuckDB's `memory_limit`, such as `500MB` or `50%` |
| `DUCKDB_THREADS` | every CPU DuckDB sees | DuckDB's `threads` |
| `DUCKDB_SPILL_DIR` | see below | Where DuckDB writes joins, aggregates and sorts that outgrow the limit; `off` turns spilling off |
| `DUCKDB_SPILL_MAX_MB` | DuckDB's, 90% of the free disk | The most DuckDB may spill |

Each is read as `DB_<NAME>_<VARIABLE>` for a named source, then as the bare
variable. In a container, DuckDB sees the container's memory and CPU limits.

Unset, the spill directory is:

- **for a source without a database file**, a new directory under the system's
  temp directory, removed when the server stops; no spilling where that
  directory is in memory (a tmpfs or ramfs, or Cloud Run's filesystem), as a
  file there takes the memory it was meant to free;
- **for a database file**, DuckDB's own default: `<file>.tmp` beside it.

### How much memory to give DuckDB

DuckDB's limit covers its own work, not the server around it. Measured in a
1 GB container with 2 CPUs, four queries at once, each grouping 24 million
Parquet rows by 7.6 million keys:

| Setting | Result |
|---|---|
| Defaults | The container ran out of memory after 11 s: the server was killed, and all four queries were lost |
| `DB_LAKE_DUCKDB_MEMORY_LIMIT=500MB` | All four answered in about 83 s, spilling to disk; the server stayed up |

Set the limit to about half the container's memory, shared by:

- each source without a database file: one limit for all its queries;
- each database file: queries that overlap share one DuckDB instance and its
  limit; a query that runs alone opens the file anew;
- each [federated query's](federation.md#running-a-federated-query) combine
  step: `FEDERATION_COMBINE_MEMORY_LIMIT`, up to `FEDERATION_MAX_CONCURRENT` at
  once.

Instances belong to sources, not to models: every model on one source shares
its instance. `DB_<NAME>_MAX_CONNECTIONS` caps how many of the source's queries
run at once.

Where spilling is off, a query past the limit fails with DuckDB's
`Out of Memory Error` rather than the server being killed.

## In Docker

DuckDB loads an extension the first time a query needs it: `httpfs` for object
storage, `iceberg` and `avro` for Iceberg. The image lets it download them into
`/app/.duckdb`. Measured: the first query of an Iceberg source took 26 s while
DuckDB downloaded them, the next one 32 ms.

For a host that cannot reach `extensions.duckdb.org`, build them into the image
(about 105 MB):

```bash
docker build --build-arg DUCKDB_EXTENSIONS="httpfs iceberg avro" -t orionbelt-api .
```

To spill to a volume rather than the container's writable layer:

```bash
docker run -v obsl-spill:/app/spill -e DB_LAKE_DUCKDB_SPILL_DIR=/app/spill ...
```

## Limitations

| Limitation | Why | Workaround |
|---|---|---|
| One Iceberg catalog per source | The DuckDB dialect writes `"schema"."table"`, without the catalog's name | Declare a second source for the second catalog. A query reading both is a [federated query](federation.md) |
| Sources that read one database file at the same time must agree on `DUCKDB_MEMORY_LIMIT`, `DUCKDB_THREADS` and `DUCKDB_SPILL_DIR` | DuckDB opens one instance per file in a process, with one set of settings | Give them the same settings. A mismatch is refused, naming the settings |
| An Iceberg folder cannot be named in `code` | DuckDB reads a bare path only for files such as Parquet | A view over `iceberg_scan`, or a catalog |
