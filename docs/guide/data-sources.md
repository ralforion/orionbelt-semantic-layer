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

A DuckDB source can also read Parquet, CSV and JSON files and Iceberg tables in
place, with no database file: see [Files and Lakehouse
Tables](files-and-lakehouse.md) for its setup statements and memory settings.

| Rule | Behavior |
|---|---|
| `OBSL_DATABASES` | Comma-separated names. Their order is the order sources are listed in |
| Prefix | `DB_` + the name upper-cased, every run of other characters turned into `_`: `shop-eu` reads `DB_SHOP_EU_*` |
| `DB_<NAME>_TYPE` | Required. One of `bigquery`, `clickhouse`, `databricks`, `dremio`, `duckdb`, `mysql`, `postgres`, `snowflake` (`postgresql` is accepted for `postgres`) |
| `DB_<NAME>_DESCRIPTION` | Optional. What the source holds, shown wherever sources are listed |
| `DB_<NAME>_POOL_SIZE` | Optional. Idle connections kept for the source; falls back to `DB_POOL_SIZE`, then 5 |
| `DB_<NAME>_MAX_CONNECTIONS` | Optional. Connections of the source in use at once, by REST, pgwire, Flight SQL and federated legs together; falls back to `DB_MAX_CONNECTIONS`, then 10. `0` means no limit. A query past it waits its turn, in order of arrival |
| `DB_<NAME>_ACQUIRE_TIMEOUT_SECONDS` | Optional. How long a query waits for one of those connections; falls back to `DB_ACQUIRE_TIMEOUT_SECONDS`, then 30. A query still waiting then is refused with `DATA_SOURCE_BUSY` (HTTP 503), naming the source and the setting to raise |
| `DB_<NAME>_KEY_INJECTION_MAX_KEYS` | Optional. The most keys a [federated query](federation.md#key-injection) lists or [maps](federation.md#key-maps) in a leg on this source; falls back to `DB_KEY_INJECTION_MAX_KEYS`, then the engine's measured limit. `0` turns lists and maps off |
| `DB_<NAME>_TIMEZONE` | Optional. The IANA zone the source's sessions run in, which its timestamps without a zone are in; falls back to `DB_TIMEZONE`, then the zone [detected](model-format.md#resolution-order) from the session. Declare it where none is detected (Databricks, Dremio) or the timestamps were written in another. Results are labeled with it, and a [federated query](federation.md#timestamps-without-a-time-zone) converts from it |
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

A query reading data objects of several sources is a *federated query*: it is
compiled into one query per source, a *leg*, each written in its source's
dialect, and a DuckDB query combining their results. Every surface that
executes queries runs it the same way, through the result cache they share.
The answer is the one a single database holding all the tables would return,
or the query is refused with a named `FEDERATION_*` error.

A query on such a model that reads only one source is not split: it runs on
that source, written in its dialect.

[Federated Queries](federation.md) explains how a query is split, where each
filter runs, how each aggregation is combined, the run time settings, and every
limitation.

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
the source the model names. A model whose data objects are read from several
sources has each data object probed on its own source, in that source's
dialect, as its queries read it, and a data object read through `nestedIn` on
its parent's source; a source that cannot be reached is reported
where it is named, and the others are probed all the same. An explicit
`dialect` cannot point the probe at another source: one that differs from a
source's dialect is reported as `DATASOURCE_UNAVAILABLE`, naming both dialects.

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
the content of the key file counts, not only its path. For DuckDB the
[setup and connection statements, memory, thread, spill and path settings](files-and-lakehouse.md)
count too: a pool hands out connections opened with one set of them, and the
paths a source may read decide what its queries can return.

Each pool also bounds the source's connections in use at once
(`MAX_CONNECTIONS`, default 10), whether a query runs through REST, pgwire,
Flight SQL or as a leg of a federated query, and whether its connection is
pooled or opened for it alone. Two names sharing a pool share that bound, set
by the first name a query used. Idle connections kept for reuse do not count
against it; at most `POOL_SIZE` of them are kept, and never more than the
bound. Waiting queries are served in order of arrival, so a steady stream of
new ones cannot starve one that came first.

## Logging in as the signed-in user

By default a data source logs in with one account for everyone: the
credentials in its variables. With `DB_<NAME>_AUTH=user` it logs in as the
**user who called OrionBelt** instead, so the database applies that user's own
grants, row-level security and masking, and its audit log names them.

```bash
OBSL_DATABASES=lakehouse
DB_LAKEHOUSE_TYPE=databricks
DB_LAKEHOUSE_AUTH=user
DB_LAKEHOUSE_DATABRICKS_SERVER_HOSTNAME=dbc-1234.cloud.databricks.com
DB_LAKEHOUSE_DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/abc123
```

The caller signs in with your identity provider and sends its access token
(`AUTH_MODE=oidc`, see [OpenID Connect](authentication.md#openid-connect)). For
each query OrionBelt exchanges that token for one issued **for the database**,
for the same user, and connects with it. It never sends the caller's own token
to the database.

| Dialect | How the user's token is exchanged | Set up at the vendor |
|---------|-----------------------------------|----------------------|
| Databricks | At the workspace's own token service (OAuth token federation). | An account federation policy that accepts your identity provider's issuer and OrionBelt's audience (`OIDC_AUDIENCE`), and names the claim holding the Databricks user name. A service-principal policy also needs `DB_<NAME>_OAUTH_CLIENT_ID`. |
| Snowflake | Your identity provider issues it, asked by OrionBelt's own client there: Entra ID through its on-behalf-of flow ([Snowflake through Entra ID](#snowflake-through-entra-id)), Okta, Keycloak and others through the standard token exchange ([Snowflake through Okta, Keycloak and others](#snowflake-through-okta-keycloak-and-others)). | An External OAuth security integration that trusts the identity provider; there, permission for OrionBelt's client to ask for Snowflake tokens. |

BigQuery follows. Other dialects refuse `DB_<NAME>_AUTH=user`.

What changes for such a source:

- **No shared connections.** Each query opens a connection for its caller and
  closes it after; `MAX_CONNECTIONS` still bounds them.
- **Results are cached per user.** OrionBelt cannot see a user's grants, so
  one user's cached rows are never served to another. The same query asked by
  ten users is ten cache entries.
- **Cached at most five minutes** (`DB_<NAME>_CACHE_MAX_TTL_SECONDS`, default
  300; 0 turns caching off for the source). OrionBelt is not told when a
  user's grants change, so a revoked user may still get **their own** cached
  rows until then, never anyone else's. A query across data sources is cached
  no longer than the strictest source it reads allows. Responses show
  `ttl_source: user_login_capped` when the cap applied.
- **Every caller must be a signed-in user.** An API key, or no
  authentication, is refused with 401 `SIGN_IN_REQUIRED`, before any cache
  lookup. When the database's token service refuses the user, the answer is
  403 `DATA_SOURCE_SIGN_IN_REFUSED` with its reason; when it cannot be
  reached, 503 `DATA_SOURCE_SIGN_IN_UNAVAILABLE`. When the identity provider
  wants the user at its sign-in page first (multi-factor authentication,
  conditional access, consent), 401 `DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED`.
  Flight SQL and pgwire answer with their own equivalents.
- OrionBelt refuses to start when a source logs in as the user but
  `AUTH_MODE` is not `oidc`, and when a Snowflake source does and OrionBelt
  has no client at the identity provider or does not know its token endpoint.

The exchanged token is kept in memory per data source and user, and exchanged
again a minute before it expires; it is never written to disk or logged.

### Snowflake through Entra ID

Snowflake takes a token that your identity provider issues for it (External
OAuth), so OrionBelt asks Entra ID for one, on the user's behalf, as a client
of its own:

```bash
# OrionBelt's app registration: the one OIDC_AUDIENCE names, with a secret.
OIDC_CLIENT_ID=<OrionBelt's application (client) ID>
OIDC_CLIENT_SECRET_FILE=/run/secrets/obsl-client-secret

OBSL_DATABASES=warehouse
DB_WAREHOUSE_TYPE=snowflake
DB_WAREHOUSE_AUTH=user
DB_WAREHOUSE_OAUTH_EXCHANGE=entra_obo
DB_WAREHOUSE_OAUTH_SCOPE=api://<Snowflake app ID>/session:role-any
DB_WAREHOUSE_SNOWFLAKE_ACCOUNT=xy12345.eu-west-1
DB_WAREHOUSE_SNOWFLAKE_WAREHOUSE=BI_WH
DB_WAREHOUSE_SNOWFLAKE_DATABASE=SALES
```

The on-behalf-of flow only accepts a user's token issued for the client that
asks, so the token users call OrionBelt with must be issued for OrionBelt's
app registration: `OIDC_AUDIENCE` names it, and `OIDC_CLIENT_ID` is its
client ID. OrionBelt asks Entra at `OIDC_TOKEN_ENDPOINT`, or at the token
endpoint the issuer's discovery document names.

**In Entra ID:** an app registration for Snowflake that exposes the scopes
users may get (Snowflake's guide names them `session:scope:<role>`, plus
`session:role-any`), and, on OrionBelt's app registration, a client secret and
the delegated permission to those scopes.

**In Snowflake:** a security integration that trusts Entra's tokens and maps
a claim to the Snowflake user. Take the issuer and keys URL for your tenant
from [Snowflake's guide for Entra ID](https://docs.snowflake.com/en/user-guide/oauth-azure):

```sql
CREATE SECURITY INTEGRATION entra_orionbelt
  TYPE = EXTERNAL_OAUTH
  ENABLED = TRUE
  EXTERNAL_OAUTH_TYPE = AZURE
  EXTERNAL_OAUTH_ISSUER = 'https://sts.windows.net/<tenant ID>/'
  EXTERNAL_OAUTH_JWS_KEYS_URL = 'https://login.microsoftonline.com/<tenant ID>/discovery/v2.0/keys'
  EXTERNAL_OAUTH_AUDIENCE_LIST = ('api://<Snowflake app ID>')
  EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'upn'
  EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'login_name'
  EXTERNAL_OAUTH_ANY_ROLE_MODE = 'ENABLE';
```

**The role** comes from the scope. A role scope fixes it. With
`session:role-any` (which needs `EXTERNAL_OAUTH_ANY_ROLE_MODE = 'ENABLE'`),
`DB_<NAME>_SNOWFLAKE_ROLE` picks the role, or else the user's default role
applies. The scope is part of the source's identity: two sources that differ
only in their scope share no token and no cached result.

A `SNOWFLAKE_USER` or `SNOWFLAKE_PASSWORD` set for the source is not sent: the
token says who connects.

**When Entra wants the user to sign in again**, for multi-factor
authentication, a conditional access policy or consent, the answer is 401
`DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED`. That covers Entra's
`interaction_required`, `login_required` and `consent_required`, and an
`invalid_grant` whose `suberror` says signing in again resolves it, as
Microsoft's client libraries read them; any other `invalid_grant` is 403
`DATA_SOURCE_SIGN_IN_REFUSED`. Over REST the 401's `WWW-Authenticate` header
says where to sign in (`authorization_uri`: `OIDC_AUTHORIZATION_ENDPOINT`, or
the one the discovery document names) and carries Entra's claims challenge
when it sent one (`error="insufficient_claims"`, `claims` base64-encoded),
which the client presents when it signs the user in again.

### Snowflake through Okta, Keycloak and others

Identity providers that implement the standard token exchange (RFC 8693),
such as Okta and Keycloak, issue the Snowflake token the same way, asked by
OrionBelt as its own client:

```bash
OIDC_CLIENT_ID=obsl
OIDC_CLIENT_SECRET_FILE=/run/secrets/obsl-client-secret

OBSL_DATABASES=warehouse
DB_WAREHOUSE_TYPE=snowflake
DB_WAREHOUSE_AUTH=user
DB_WAREHOUSE_OAUTH_EXCHANGE=token_exchange
DB_WAREHOUSE_OAUTH_SCOPE=session:role:ANALYST
DB_WAREHOUSE_OAUTH_AUDIENCE=snowflake
DB_WAREHOUSE_SNOWFLAKE_ACCOUNT=xy12345.eu-west-1
DB_WAREHOUSE_SNOWFLAKE_WAREHOUSE=BI_WH
```

OrionBelt sends the user's token as `subject_token` (an access token) with
`DB_<NAME>_OAUTH_SCOPE` and, when set, `DB_<NAME>_OAUTH_AUDIENCE`, to
`OIDC_TOKEN_ENDPOINT` or the discovered token endpoint. Its client
authenticates with HTTP Basic. As with Entra, the token users call OrionBelt
with must be issued for that client.

| Identity provider | What to set up there | `DB_<NAME>_OAUTH_AUDIENCE` |
|---|---|---|
| Okta | An API Services app with the Token Exchange grant, for OrionBelt; on the custom authorization server whose tokens Snowflake trusts, scopes named after the roles (`session:role:ANALYST`) and an access policy that lets OrionBelt's app ask for them. | That authorization server's audience |
| Keycloak (26.2 and later) | A confidential client for OrionBelt with **Standard token exchange** on, which the users' tokens name in `aud`; per role, an optional client scope on it that adds Snowflake as an audience and the `scp` claim Snowflake reads the role from. | The client ID that stands for Snowflake |

In Snowflake, the security integration trusts the identity provider: Okta's
type is `OKTA`; any other is `CUSTOM`, which takes the provider's keys URL or
its public key. For Keycloak, with the realm's RSA public key and the user's
email mapped to the Snowflake login name:

```sql
CREATE SECURITY INTEGRATION keycloak_orionbelt
  TYPE = EXTERNAL_OAUTH
  ENABLED = TRUE
  EXTERNAL_OAUTH_TYPE = CUSTOM
  EXTERNAL_OAUTH_ISSUER = 'https://keycloak.example.com/realms/<realm>'
  EXTERNAL_OAUTH_RSA_PUBLIC_KEY = '<the realm public key, base64>'
  EXTERNAL_OAUTH_AUDIENCE_LIST = ('snowflake')
  EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'email'
  EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME';
```

The scope and the audience are part of the source's identity: two sources
that differ in either share no token and no cached result.

## How OBSL names itself to your database

Each connection OBSL opens to a data source carries the name
`orionbelt-semantic-layer/<version>`, for example
`orionbelt-semantic-layer/3.1.1`. Your database shows it next to the queries
OBSL runs, so you can tell them apart from other tools' queries.

| Database | Sent as | Where you see it |
| --- | --- | --- |
| BigQuery | The start of the User-Agent | Cloud Audit Logs: `protoPayload.requestMetadata.callerSuppliedUserAgent` |
| ClickHouse | The start of the User-Agent | `http_user_agent` in `system.query_log` and `system.processes` |
| Databricks | Inside the User-Agent | The Source column of Query History, and `user_agent` in `system.access.audit` |
| DuckDB, MotherDuck | DuckDB's `custom_user_agent` | MotherDuck: `USER_AGENT` in `MD_INFORMATION_SCHEMA.QUERY_HISTORY` (Business plan). Files read over HTTP: the User-Agent of each request |
| MySQL | The `program_name` connection attribute | `performance_schema.session_connect_attrs` |
| Postgres | `application_name` | `pg_stat_activity` |
| Snowflake | Nothing | Snowflake's `application` setting is for its registered partners |
| Dremio | Nothing | |

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
