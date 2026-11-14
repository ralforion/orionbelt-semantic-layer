---
description: "Run a semantic model on Parquet, CSV and JSON files and on Apache Iceberg tables with no warehouse: a DuckDB data source reads them in place, within a memory limit you set."
---

# Files and Lakehouse Tables

A DuckDB [data source](data-sources.md) can read files and Iceberg tables where
they are: Parquet, CSV and JSON files on disk or in object storage, and Apache
Iceberg tables behind a REST catalog. No warehouse is needed. The DuckDB inside
the OrionBelt process runs the queries, so the model's dialect is `duckdb`.

| What you read | How a data object names it | Setup statements | `DUCKDB_ALLOWED_PATHS` |
|---|---|---|---|
| Parquet, CSV or JSON files on disk | `code` is the path or a glob, `schema: ""` | none | the files' directory |
| Files in S3-compatible object storage | `code` is the `s3://` path or glob, `schema: ""` | a `CREATE SECRET` for the bucket | the bucket, `s3://bucket/` |
| Iceberg tables in a REST catalog | `code` is the table, `schema` its namespace | `ATTACH` the catalog, `SET search_path` | the catalog's URL and the bucket |
| An Iceberg table without a catalog | a view over `iceberg_scan(...)` | `CREATE OR REPLACE VIEW` | the table's folder |

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
attached. The instance is the source's own, a named `:memory:name` included:
sources with the same settings share one, and nothing else reaches it by its
name.

Set `:memory:` explicitly where `DUCKDB_DATABASE` is set for the whole server,
as the Docker image does: a named source falls back to the unprefixed variable.

## What a source may read

A data object's `code` can name a file, so a model could name any file the
server can read: its configuration, a key file. A DuckDB source therefore reads
only its own database, unless `DUCKDB_ALLOWED_PATHS` lists more:

```bash
DB_LAKE_DUCKDB_ALLOWED_PATHS=/data/orders/,s3://my-bucket/,https://catalog.example.com/
```

| Entry | Allows |
|---|---|
| A directory | Every file in it and below it, and nothing beside it: `/data/orders/` does not open `/data/orders_private/`, and `..` cannot climb out |
| A file | That file alone |
| A URL prefix | Everything under it: an S3 bucket, an Iceberg catalog's REST endpoint |
| `*` | Everything the server can read, as before this setting existed |
| Unset | The source's database alone. A MotherDuck (`md:`) database is not limited unless the setting is given: its data is remote |

- **What it covers.** Every query of the source, on REST, the Postgres wire and
  Flight SQL. A path the list leaves out is refused with DuckDB's `Permission
  Error`, and the error names `DUCKDB_ALLOWED_PATHS`.
- **The setup statements run first.** The limit is set after them, so they can
  still load extensions and attach a catalog. What the queries then read, the
  files behind a view or the catalog's tables, has to be listed.
- **Extensions.** Once the limit is set, DuckDB loads no further extension. One
  that the setup statements do not load (with `LOAD`, or by using it, as
  `CREATE SECRET (TYPE s3, ...)` loads `httpfs`) cannot be loaded later.
- **It cannot be lifted while the server runs.** DuckDB refuses to turn it off
  again. A changed list takes effect after a restart.
- **One list per database file.** The first source to open a file decides its
  paths until the server restarts; another source of the file with other paths
  is refused.

## Files by path

Put the path in the data object's `code`, leave `schema` empty, and list the
files' directory in the source's `DUCKDB_ALLOWED_PATHS` (here
`DB_LAKE_DUCKDB_ALLOWED_PATHS=/data/orders/`):

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
DB_LAKE_DUCKDB_ALLOWED_PATHS=s3://my-bucket/
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
| They run for one connection of an instance at a time | Run at once, two connections' `CREATE OR REPLACE VIEW`s conflict in DuckDB's catalog (`Catalog write-write conflict`) |
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
DB_LAKE_DUCKDB_ALLOWED_PATHS=http://iceberg-rest:8181/,s3://warehouse/
```

Each query asks the catalog for the table, so the catalog's URL is listed as
well as the bucket that holds the data files.

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

- List the table's folder in `DUCKDB_ALLOWED_PATHS`: the view reads it at query
  time.
- `version = '?'` reads the newest metadata file DuckDB finds in the table's
  folder.
- A path to one `metadata.json` file reads that snapshot, and keeps reading it
  after later commits.

## Memory and spilling

| Variable | Default | What it sets |
|---|---|---|
| `DUCKDB_MEMORY_LIMIT` | `50%` of the memory DuckDB sees | DuckDB's `memory_limit`, such as `500MB` or `50%`. DuckDB's own default, 80%, is left out on purpose: see below |
| `DUCKDB_THREADS` | every CPU DuckDB sees | DuckDB's `threads` |
| `DUCKDB_SPILL_DIR` | see below | Where DuckDB writes joins, aggregates and sorts that outgrow the limit; `off` turns spilling off |
| `DUCKDB_SPILL_MAX_MB` | DuckDB's, 90% of the free disk | The most DuckDB may spill, wherever it spills |

Each is read as `DB_<NAME>_<VARIABLE>` for a named source, then as the bare
variable. In a container, DuckDB sees the container's memory and CPU limits.

Unset, the spill directory is:

- **for a source without a database file**, a new directory under the system's
  temp directory, removed when the server stops; no spilling where that
  directory is in memory (a tmpfs or ramfs, or Cloud Run's filesystem), as a
  file there takes the memory it was meant to free;
- **for a database file**, DuckDB's own default: `<file>.tmp` beside it.

### How much memory to give DuckDB

DuckDB's limit covers its own work, not the server around it, and DuckDB
spills to disk only once it reaches the limit. Measured in a 1 GB container
with 2 CPUs, four queries at once, each grouping 24 million Parquet rows by 7.6
million keys:

| Memory limit | Result |
|---|---|
| DuckDB's default, 80% (819 MiB) | The container ran out of memory after 11 s: the server was killed, and all four queries were lost. With the server's own memory on top, 80% leaves no room |
| Unset: 50% (512 MiB) | All four answered in about 68 s, spilling to disk; the server stayed up |
| 500 MB | All four answered in about 83 s, spilling to disk; the server stayed up |
| 150 MB per query | Each query failed with DuckDB's `Out of Memory Error`: too little memory even to spill. The server stayed up |

That is why a source's limit is half the memory unless you set it. Spilling
lets a query past the limit finish, more slowly, rather than fail: one of these
queries alone took 7.6 s with the memory it needed.

Each limit is one instance's. Two DuckDB sources busy at once can, at half the
memory each, still take all of it; where several are, set their limits so that
together they stay near half the container's memory. The limits that add up:

- each source without a database file: one limit for all its queries;
- each database file: queries that overlap share one DuckDB instance and its
  limit; a query that runs alone opens the file anew;
- each [federated query's](federation.md#running-a-federated-query) combine
  step: `FEDERATION_COMBINE_MEMORY_LIMIT`, up to `FEDERATION_MAX_CONCURRENT` at
  once.

Instances belong to sources, not to models: every model on one source shares
its instance. `DB_<NAME>_MAX_CONNECTIONS` caps how many of the source's queries
run at once.

Where spilling is off (`DUCKDB_SPILL_DIR=off`, or a temp directory in memory),
a query past the limit fails with DuckDB's `Out of Memory Error`.

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
| Sources of one database file must agree on `DUCKDB_ALLOWED_PATHS` | DuckDB shares one instance of a file between the queries reading it at once, and its limit, once set, cannot change; setting it would also cut off a source reading without one | The first source to open a file decides its paths until the server restarts, however the path is spelled. A source of the file with other paths is refused, naming its setting: give them the same list |
| One Iceberg catalog per source | The DuckDB dialect writes `"schema"."table"`, without the catalog's name | Declare a second source for the second catalog. A query reading both is a [federated query](federation.md) |
| Sources that read one database file at the same time must agree on `DUCKDB_MEMORY_LIMIT`, `DUCKDB_THREADS` and `DUCKDB_SPILL_DIR` | DuckDB opens one instance per file in a process, with one set of settings | Give them the same settings. A mismatch is refused, naming the settings |
| An Iceberg folder cannot be named in `code` | DuckDB reads a bare path only for files such as Parquet | A view over `iceberg_scan`, or a catalog |
