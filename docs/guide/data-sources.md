---
description: "Connect one OrionBelt server to several databases, including two of the same dialect, by naming them in the environment and letting each model pick its own with settings.defaultDataSource."
---

# Data Sources

A **data source** is a database the server can run queries on. Configured the
classic way, with `DB_VENDOR` and one set of variables per dialect
(`POSTGRES_HOST`, `SNOWFLAKE_ACCOUNT`, ...), a server reaches one database per
dialect. **Named data sources** lift that limit: declare as many as you need in
the environment, two Postgres databases included, and let each model say which
one its queries run on.

Nothing changes for a deployment that declares no named sources: every query
runs on the dialect's variables as before, and result-cache keys stay the same.

## Declaring data sources

List the names in `OBSL_DATABASES` and give each its dialect and connection
settings behind a `DB_<NAME>_` prefix:

```bash
OBSL_DATABASES=crm,shop-eu,shop-us

DB_CRM_TYPE=postgres
DB_CRM_DESCRIPTION=CRM: customers, returns
DB_CRM_POSTGRES_HOST=crm.internal
DB_CRM_POSTGRES_DBNAME=crm

DB_SHOP_EU_TYPE=mysql
DB_SHOP_EU_DESCRIPTION=EU web shop
DB_SHOP_EU_MYSQL_HOST=eu.shop.internal
DB_SHOP_US_TYPE=mysql
DB_SHOP_US_MYSQL_HOST=us.shop.internal

# Unprefixed values are defaults every named source of that dialect shares
MYSQL_USER=reporting
MYSQL_PASSWORD=...
MYSQL_DATABASE=shop
```

Each source reads its dialect's usual variables (see
[Drivers](../drivers.md#db_vendor-and-credentials)) with the prefix in front. A
variable the source does not set falls back to the unprefixed one, so the two
shops above share a user, password and database name and differ only in host.

| Rule | Behavior |
|---|---|
| `OBSL_DATABASES` | Comma-separated names. Their order is the order sources are listed in |
| Prefix | `DB_` + the name upper-cased, every run of other characters turned into `_`: `shop-eu` reads `DB_SHOP_EU_*` |
| `DB_<NAME>_TYPE` | Required. One of `bigquery`, `clickhouse`, `databricks`, `dremio`, `duckdb`, `mysql`, `postgres`, `snowflake` (`postgresql` is accepted for `postgres`) |
| `DB_<NAME>_DESCRIPTION` | Optional. What the source holds, shown wherever sources are listed |
| `DB_<NAME>_POOL_SIZE` | Optional. Idle connections kept for the source; falls back to `DB_POOL_SIZE`, then 5 |
| `DB_<NAME>_MAX_CONNECTIONS` | Optional. Connections of the source in use at once, by REST, pgwire, Flight SQL and federated legs together; falls back to `DB_MAX_CONNECTIONS`, then 10. `0` means no limit. A query past it waits its turn, in order of arrival |
| `DB_<NAME>_ACQUIRE_TIMEOUT_SECONDS` | Optional. How long a query waits for one of those connections; falls back to `DB_ACQUIRE_TIMEOUT_SECONDS`, then 30. A query still waiting then is refused with `DATA_SOURCE_BUSY` (HTTP 503), naming the source and the setting to raise |
| Variable lookup | `DB_<NAME>_<VAR>`, then the unprefixed `<VAR>`, each under its own name before its [alternative spellings](#orionbelt-analytics-variable-names). A source's own value always wins over a shared default |
| Names | Compared ignoring case, spaces, dashes and underscores, so `Shop EU` means `shop-eu` |

The server refuses a declaration it cannot use, and says why: a name listed
twice, a name with no letters or digits, two names that would read the same
`DB_<NAME>_` variables (`shop.eu` and `shop/eu`), or a missing or unknown
`TYPE`.

### Dialects configured without a name

A dialect configured the classic way, with its unprefixed variables
(`POSTGRES_HOST`, `DUCKDB_DATABASE`, `SNOWFLAKE_ACCOUNT`, ...), is still a
source, **named after the dialect**: `postgres`, `duckdb`, ... That is how every
deployment worked before named sources, and how it keeps working. Once a
dialect has a named source, its unprefixed variables are only the shared
defaults of its named sources and no longer a source of their own.

## Choosing a model's data source

A model names the source its queries run on in its settings:

```yaml
settings:
  defaultDialect: mysql
  defaultDataSource: shop-eu
```

It is a name only, never credentials: the environment says where `shop-eu` is
and how to sign in, so the same model file runs unchanged against a test and a
production database. For every query the server picks the source in this order:

1. the source the model names in `settings.defaultDataSource`;
2. otherwise the only named source of the query's dialect;
3. otherwise, when the dialect has no named source, the dialect's own variables.

Two or more named sources of the dialect and no name in the model is an error,
not a guess: the query is refused with the candidates, for example
*"Model 'sales' runs on mysql, and 2 mysql data sources are configured
(shop-eu, shop-us). Set settings.defaultDataSource, or load the model with a
datasource."*

The source must be of the model's dialect. Where queries execute
(`QUERY_EXECUTE=true` or Flight), a model naming a source that is not
configured, or one of another dialect, fails to load with
`DATA_SOURCE_UNAVAILABLE`. For `MODEL_FILES` that is at startup, so a typo in a
name stops the deployment instead of failing its first query. The error's `context.reason`
says which case it is (`unknownSource`, `dialectMismatch`, `ambiguousSource`,
`invalidConfiguration`, `extensionOutdated`), and a misspelt name is answered with
the configured names it is close to: *"Did you mean 'shop-eu'?"*

### A data object's own source

A data object can name its source in `dataSource`, which wins over the model's
`settings.defaultDataSource`:

```yaml
dataObjects:
  Orders:
    code: orders
    dataSource: shop-eu
```

A data object read through `nestedIn` comes from its parent's table and takes
the parent's source. Load-time checks cover these names too: where queries
execute, an unknown one fails the load at `dataObjects.<name>.dataSource`.

### Queries across data sources

A query reading data objects of several sources is compiled into one query per
source, a *leg*, and a DuckDB query combining them. Every surface that executes
queries runs it the same way: REST `query/execute`, OBSQL (REST
`query/semantic-ql`, Flight SQL and pgwire) and `obsl execute`. Each leg runs on
its own source, all at once, then the combine query in an in-memory DuckDB
inside OBSL, through the result cache the surfaces share, so a query cached by
one is a hit on the others. `query/sql` and `obsl compile` show the plan, the
REST execute response lists the legs in `federation`, and lineage names each
table's data source. The one-shot batch (`oneshot/batch`) and the database
`EXPLAIN` of `query/plan` run on one source and refuse such a model with
`FEDERATION_NOT_ENABLED`.

A query on such a model that reads only one source is not split: it runs on
that source, written in its dialect.

How a query is split:

- Groups of joined data objects on one source form islands. The island
  holding the measures is the fact island; every other island is joined to it
  by a many-to-one join.
- The fact leg groups by the query's dimensions on its source and by the join
  keys, and pre-aggregates the measures. Each other leg returns the distinct
  join keys with the dimensions the query reads there. Each leg is written in
  its source's dialect; a source not configured where the query is compiled
  gets the dialect the query is compiled for.
- The combine query joins the legs on their keys, re-aggregates each measure
  (`SUM` of sums and counts, `MIN` / `MAX` of minimums and maximums), evaluates
  derived metrics, and applies HAVING, ORDER BY and LIMIT. An order whose key
  has no match keeps its values under an empty group, as in one database.
- A filter on the fact island's dimensions runs in its leg. Any other filter
  runs in the combine query, after the join. So does a static model filter on
  another island, on a column that island's leg returns: it drops the rows it
  excludes and keeps an unmatched row when it holds for NULL, as it does in one
  database, and an `enforce: always` filter applies even when the query reads
  nothing else of that island. A filter on a dimension the query selects at a
  time grain cannot run after the join and is refused.
- Measures are combined from partial results that are neither rounded nor
  bounded: a leg leaves its sums in the engine's own type, which keeps the
  input's scale and the engine's range, and the measure's own type and
  `defaultValue` apply once, to the combined value. A count over no rows is 0,
  whatever default it declares, as a SQL `COUNT` is.

What does not split exactly is refused by name rather than answered with a
different number: measures from more than one source (`FEDERATION_MULTI_FACT`),
`avg`, `count_distinct` and other aggregations that cannot be combined from
partial results (`FEDERATION_UNSUPPORTED_AGGREGATION`), a measure reading
another source (`FEDERATION_MEASURE_ACROSS_SOURCES`), cumulative,
period-over-period, window and reaggregate metrics (`FEDERATION_UNSUPPORTED_METRIC`),
joins between sources other than many-to-one from the measures' side
(`FEDERATION_UNSUPPORTED_JOIN`), relative date and time-grain filters on
another source (`FEDERATION_UNSUPPORTED_FILTER`), and queries without measures,
with `exists`, role-playing dimensions, `fill`, `asOf`, `usePathNames` or raw
mode.

#### Running a federated query

| What | How |
|---|---|
| Caching | The result is cached like any query's. Its key covers the combine query and every leg's SQL with the data source it runs on, its TTL is the shortest of all the tables the legs read, and a change to any of them invalidates it; a hit runs nothing. A result computed from legs served from the cache expires no later than the first of them, so it is never older than its legs. Each leg is also cached on its own, keyed by its data source and SQL, so a query that changes only what the combine step does (a filter on another source, HAVING, ORDER BY, LIMIT) reuses the legs an earlier query fetched |
| Legs | Run at once, each on its own source |
| Types | Each leg's columns are made exact for DuckDB before combining: Postgres `numeric`, which arrives as text, becomes a decimal at the scale its values carry, and a decimal wider than 38 digits narrows when its values fit. A value no decimal holds (`NaN`, more than 38 digits) is refused with `FEDERATION_LEG_TYPE`, never rounded |
| Errors | A leg that fails fails the query, and the error names the leg's data source and dialect (HTTP 502) |
| Leg size | A leg may return at most `FEDERATION_MAX_LEG_ROWS` rows (default 1,000,000). Each leg's SQL asks for one more row, so a larger leg is refused with `FEDERATION_LEG_TOO_LARGE` rather than combined in part |
| Concurrency | At most `FEDERATION_MAX_CONCURRENT` federated queries run at once per process (default 4). The others wait in order of arrival for up to `FEDERATION_QUEUE_TIMEOUT_SECONDS` (default 30), then get `FEDERATION_BUSY` (HTTP 503). Legs run on a pool of `FEDERATION_LEG_THREADS` threads (default 16) |
| Combine | A fresh in-memory DuckDB per query, with `FEDERATION_COMBINE_THREADS` threads (default 2) and `FEDERATION_COMBINE_MEMORY_LIMIT` (default `512MB`) instead of DuckDB's defaults of every core and 80% of RAM per instance |
| Spill to disk | Off unless `FEDERATION_SPILL_DIR` names a directory. A leg is then read batch by batch and held in memory up to `FEDERATION_SPILL_THRESHOLD_MB` (default 64); past that, it goes to an Arrow IPC file that the combine step reads memory-mapped, so the operating system can page it out, and DuckDB spills its own join and sort state next to it. Each query writes into its own directory, readable by the server alone and removed when the query ends, also when it fails. One query may spill at most `FEDERATION_SPILL_MAX_MB` (default 4096, else `FEDERATION_SPILL_TOO_LARGE`, HTTP 502), and the queries running at once at most `FEDERATION_SPILL_BUDGET_MB` together (default 16384, else `FEDERATION_SPILL_FULL`, HTTP 503). DuckDB's own spill in the combine step is capped at `FEDERATION_SPILL_MAX_MB` per query as well. A spilled leg is not stored in the result cache; the combined result is |

Spilling needs real disk. On an in-memory filesystem, such as Cloud Run's
default one, a spilled file takes the same memory it was meant to free; leave
`FEDERATION_SPILL_DIR` unset there, or point it at a disk-backed volume.
[Docker](../getting-started/docker.md#spilling-federated-queries-to-disk)
shows the volume to mount, how large it needs to be, and the same for
Kubernetes. A leg is read batch by batch from every data source, so a leg
that spills is never held whole in memory; it is still bounded by
`FEDERATION_MAX_LEG_ROWS`.

The legs read their sources at slightly different moments: there is no
snapshot across databases. Naive timestamps are combined as each source
returns them, so sources whose sessions run in different time zones can
disagree; and strings compare by DuckDB's binary collation in the combine
step, where MySQL's default compares case-insensitively.

### One model on several databases

[Model load](../api/endpoints.md#post-v1sessionssession_idmodels) takes a
`datasource` that replaces the model's `settings.defaultDataSource`:

```bash
curl -X POST "$OBSL/v1/sessions/$SESSION/models" -H 'Content-Type: application/json' \
  -d "{\"model_yaml\": $(jq -Rs . < sales.yaml), \"datasource\": \"shop-us\"}"
```

The same YAML loaded with another source is another model, with its own
`model_id` and its own result-cache entries. The override replaces the model's
default only: a data object naming its own `dataSource` keeps it. With `MODEL_FILES`, keep one file
per source, each with its own `name:` and `defaultDataSource`, so BI tools pick
`sales_eu` or `sales_us` by their database field.

From the command line, `--datasource` does the same for a local run:

```bash
obsl execute sales.yaml -q query.json --datasource shop-us
```

## Listing the data sources

```bash
obsl datasources                      # local configuration
obsl datasources --server https://your-host
curl "$OBSL/v1/datasources"
```

```json
{
  "datasources": [
    {"name": "crm", "dialect": "postgres", "description": "CRM: customers, returns",
     "target": {"dbname": "crm"}},
    {"name": "shop-eu", "dialect": "mysql", "description": "EU web shop",
     "target": {"database": "shop"}}
  ]
}
```

A listing shows the name, dialect, description and **target**, the database it
reads, and never a host, user, password or token. What may be shown is a fixed
list per dialect, so a credential variable added to a driver later stays
hidden:

| Dialect | Target shown |
|---|---|
| BigQuery | project |
| ClickHouse | database |
| Databricks | catalog, schema |
| MySQL | database |
| Postgres | dbname, schema |
| Snowflake | database, schema |
| DuckDB, Dremio | nothing (a MotherDuck database string can carry its token) |

The model's own setting shows in `model_settings.defaultDataSource` of
[`GET /v1/settings`](../api/endpoints.md#get-v1settings).

## Where it applies

Every surface that runs a model's SQL picks the source the same way, so the
same model reaches the same database however it is queried: REST
`query/execute`, `query/semantic-ql`, rule evaluation, the one-shot batch, value
search, the database `EXPLAIN` of `query/plan`, online validation, Arrow Flight
SQL, PostgreSQL wire and the CLI.

Online validation (`validate --online`, `POST .../validate?online=true`) probes
the source the model names. An explicit `dialect` cannot point the probe at
another source: one that differs from the named source's dialect is reported as
`DATASOURCE_UNAVAILABLE`, naming both dialects.

A Flight query runs on the source it was planned with: the ticket is fetched by
a later call, which need not name the model again.

## Connection pools and the result cache

Connections are pooled per source, and the [result cache](result-cache.md) is
scoped to it. Both are keyed by a fingerprint of the source's dialect and
connection settings, secrets included, so:

- two names for the same database with the same credentials share a pool and
  cache entries, since they return the same rows;
- the same database reached with another password is another pool and another
  cache scope;
- a dialect configured without a name keeps the cache keys it always had.

The fingerprint is a hash and shows nothing of what went into it. For BigQuery
the content of the key file counts, not only its path.

Each pool also bounds the source's connections in use at once
(`MAX_CONNECTIONS`, default 10), whether a query runs through REST, pgwire,
Flight SQL or as a leg of a federated query, and whether its connection is
pooled or opened for it alone. Two names sharing a pool share that bound, set
by the first name a query used. Idle connections kept for reuse do not count
against it; at most `POOL_SIZE` of them are kept, and never more than the
bound. Waiting queries are served in order of arrival, so a steady stream of
new ones cannot starve one that came first.

## OrionBelt Analytics variable names

The `DB_<NAME>_` prefix and naming rules match OrionBelt Analytics, so one
`.env` can declare the sources of both. OBSL keeps its own list variable
(`OBSL_DATABASES`, where Analytics reads `OBA_DATABASES`), as the two products
rarely serve the same set. Analytics' variable spellings are accepted when
OBSL's own are unset:

| OBSL | Also accepted |
|---|---|
| `POSTGRES_DBNAME` | `POSTGRES_DATABASE` |
| `POSTGRES_USER` | `POSTGRES_USERNAME` |
| `MYSQL_USER` | `MYSQL_USERNAME` |
| `SNOWFLAKE_USER` | `SNOWFLAKE_USERNAME` |
| `BIGQUERY_PROJECT` | `BIGQUERY_PROJECT_ID` |
| `BIGQUERY_CREDENTIALS_FILE` | `BIGQUERY_CREDENTIALS_PATH` |
| `DUCKDB_DATABASE` | `DUCKDB_DATABASE_PATH` |
| `DATABRICKS_ACCESS_TOKEN` | `DATABRICKS_TOKEN` |
| `MOTHERDUCK_ACCESS_TOKEN` | `MOTHERDUCK_TOKEN`, `motherduck_token` |

## Deployment

Data sources are plain environment variables, so they deploy like any other
setting.

**Docker:** put them in the env file:

```bash
docker run -p 8080:8080 -p 8815:8815 --env-file .env ralforion/orionbelt-semantic-layer-flight
```

**Cloud Run:** pass the settings as environment variables and the secrets from
Secret Manager. `OBSL_DATABASES` holds commas, so give `--set-env-vars` another
delimiter (`^;^` makes `;` separate the variables).

Pick an image with the drivers your sources need. The published API image
(`orionbelt-semantic-layer-api`) carries the DuckDB driver only; the Flight
image (`orionbelt-semantic-layer-flight`) carries all eight, and so does an API
image built with `docker build --build-arg OB_EXTRA=flight .`:

```bash
gcloud run deploy orionbelt-api --image ralforion/orionbelt-semantic-layer-flight \
  --set-env-vars "^;^QUERY_EXECUTE=true;OBSL_DATABASES=crm,shop-eu;DB_CRM_TYPE=postgres;DB_CRM_POSTGRES_HOST=10.0.0.5;DB_CRM_POSTGRES_DBNAME=crm;DB_SHOP_EU_TYPE=mysql;DB_SHOP_EU_MYSQL_HOST=10.0.0.6" \
  --set-secrets "DB_CRM_POSTGRES_PASSWORD=crm-postgres-password:latest,MYSQL_PASSWORD=shop-mysql-password:latest"
```

A changed source takes a restart (on Cloud Run, a new revision), like any
other setting. `/health` never connects to a database, so a slow or
unreachable warehouse cannot fail it.

There is no per-request choice of source. A model that names one, in
`settings.defaultDataSource` or with `datasource` at load, is bound to it: a
request cannot point it at another database, and a request for another dialect
is refused. A model that names none is not bound: each query runs on the source
of its dialect, the model's `defaultDialect` or a dialect the request asks for.
Name the source to bind a governed model.
