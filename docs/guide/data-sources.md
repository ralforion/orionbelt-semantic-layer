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
name stops the deployment instead of failing its first query.

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

### Queries across data sources (compile only)

A query reading data objects of several sources is **compiled** into one query
per source, a *leg*, and a DuckDB query combining them. It is **not executed
yet**: running it is refused with `FEDERATION_NOT_ENABLED`, as is any query on a
model whose objects name different sources. `query/sql` and `obsl compile` show
the plan:

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
- Measures are combined from partial results that are not rounded: a sum is
  kept at `DECIMAL(38, s)` (at least nine decimal places) until it is combined,
  and the measure's own type and `defaultValue` apply to the combined value. A
  count over no rows is 0.

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
