---
description: "Query OrionBelt over Arrow Flight SQL from any ADBC client: enable the Flight surface, connect with the flightsql driver, and get Arrow back with no translation hop."
---

# Connecting via ADBC (Arrow Flight SQL)

OBSL's Arrow Flight SQL surface (`FLIGHT_ENABLED=true`) speaks the protocol
ADBC's `flightsql` driver talks, so any ADBC client can query a loaded model
and get Arrow back without a translation hop. Where the
[Postgres wire surface](postgres-wire-bi-tools.md) exists so BI tools can use
a connector they already ship, this one exists for programs that want columnar
data: a client that reads Arrow gets the server's Arrow, unconverted.

Everything on this page is asserted by `tests/integration/test_adbc_flightsql.py`,
which drives a real ADBC client against a real Flight server. Run it with
`uv run pytest -m adbc_flight`.

## Start the server

```bash
DB_VENDOR=duckdb \
DUCKDB_DATABASE=/path/to/warehouse.duckdb \
MODEL_FILES=/path/to/model.yaml \
FLIGHT_ENABLED=true \
FLIGHT_PORT=8815 \
uv run orionbelt-api
```

`FLIGHT_ENABLED` implies `QUERY_EXECUTE`. The gRPC URI is then
`grpc://<host>:8815`, or `grpc+tls://` with TLS configured — see below.

## TLS

Point the server at a certificate and key and it serves `grpc+tls` instead:

```bash
FLIGHT_ENABLED=true \
FLIGHT_TLS_CERT=/certs/server.crt \
FLIGHT_TLS_KEY=/certs/server.key \
uv run orionbelt-api
```

Both or neither. Setting one without the other refuses to start rather than
falling back to plaintext, because a deployment that reads as encrypted and is
not is worse than one that does not come up. The startup line says which
transport is live, so confirming it needs no packet capture.

Worth stating plainly: **the Flight surface authenticates before it encrypts.**
Without TLS, an API key sent by any of the mechanisms above crosses the wire in
clear text. Configuring auth without TLS logs a warning for that reason.

### From Docker

Certificates are files, so they arrive by mount:

```bash
docker run -p 8080:8080 -p 8815:8815 \
  -v /host/certs:/certs:ro \
  -e FLIGHT_ENABLED=true \
  -e FLIGHT_TLS_CERT=/certs/server.crt \
  -e FLIGHT_TLS_KEY=/certs/server.key \
  ralforion/orionbelt-semantic-layer-api:latest
```

**The image runs as a non-root user**, so a key mounted from the host as
`root:root 0600` is present, correctly named, and unreadable inside the
container. Make it readable by the container's user; in Kubernetes set
`defaultMode: 0444` on the secret volume, or an `fsGroup`. The server names
this case in its error rather than reporting a missing file.

### Trusting the certificate, client side

A self-signed or private-CA certificate has to be trusted explicitly, and a
client that will not check it is **refused rather than downgraded**. Both
options below are measured against a live server, not read from a driver's
README:

| Client | Trust a specific certificate | Skip verification |
|---|---|---|
| Python ADBC | `adbc.flight.sql.client_option.tls_root_certs` (PEM text) | `...tls_skip_verify` = `"true"` |
| DuckDB `adbc_scanner` | the same keys, in the `adbc_connect` MAP | as above |
| Flight SQL JDBC | `trustStore` **plus `useSystemTrustStore=false`** | `disableCertificateVerification=true` |

The JDBC row carries a trap worth reading twice: **`trustStore` on its own does
nothing.** Without `useSystemTrustStore=false` the driver keeps consulting the
system store, ignores the one you named, and fails with `unable to find valid
certification path` — the same error as passing no trust configuration at all,
so the setting looks unread rather than overridden. Measured against driver
19.0.0; setting `javax.net.ssl.trustStore` on the JVM does not work either,
because the driver shades its own TLS stack.

```
jdbc:arrow-flight-sql://obsl.example.com:8815
    ?useEncryption=true
    &useSystemTrustStore=false
    &trustStore=/path/to/truststore.jks
    &trustStorePassword=<password>
```

A JKS is built from the server's certificate (or its CA) with the JDK's own
tool:

```bash
keytool -importcert -alias obsl -file server.crt \
        -keystore truststore.jks -storepass changeit -noprompt
```

```python
conn = dbapi.connect(
    "grpc+tls://obsl.example.com:8815",
    db_kwargs={"adbc.flight.sql.client_option.tls_root_certs": open("server.crt").read()},
)
```

From DuckDB, the option key must be a **literal** in the MAP — only values can
be bound, and a parameterised key scrambles the pairs into an error about
failing to load the driver:

```sql
SELECT adbc_connect(MAP {
    'driver': '/path/to/libadbc_driver_flightsql.so',
    'uri':    'grpc+tls://obsl.example.com:8815',
    'adbc.flight.sql.client_option.tls_root_certs': '<PEM text>'
});
```

`tls_skip_verify` exists and works, and it verifies nothing: it accepts any
certificate, including one presented by whoever is between you and the server.
Reach for `tls_root_certs` unless you are debugging.

### Mutual TLS

`FLIGHT_TLS_CLIENT_CA=/certs/ca.crt` additionally requires each client to
present a certificate signed by that CA. It needs the server's own certificate
too — setting it alone is refused.

A client then supplies its own certificate and key alongside the trust
material. Trusting the server is no longer sufficient on its own: without a
client certificate the connection is dropped.

```python
conn = dbapi.connect(
    "grpc+tls://obsl.example.com:8815",
    db_kwargs={
        "adbc.flight.sql.client_option.tls_root_certs": open("ca.crt").read(),
        "adbc.flight.sql.client_option.mtls_cert_chain": open("client.crt").read(),
        "adbc.flight.sql.client_option.mtls_private_key": open("client.key").read(),
    },
)
```

## Connect

```python
from adbc_driver_flightsql import dbapi

with dbapi.connect("grpc://127.0.0.1:8815") as conn:
    with conn.cursor() as cur:
        cur.execute('SELECT "Customer Country", "Total Revenue" FROM sales')
        table = cur.fetch_arrow_table()
```

`fetch_arrow_table()` is the point: the rows never become Python objects on
the way past.

### Picking a model

A deployment can serve several models (`MODEL_FILES` loads each into its own
named session). Flight reads the model from the gRPC `database` /
`x-obsl-model` header, and ADBC sets arbitrary call headers:

```python
conn = dbapi.connect(
    "grpc://127.0.0.1:8815",
    db_kwargs={"adbc.flight.sql.rpc.call_header.x-obsl-model": "sales"},
)
```

### Authenticating

With `AUTH_MODE=api_key`, the Flight server validates the credential against the
same key store the REST surface uses. There is no account behind it — OBSL has
keys, not users — so whatever a client sends as a username is ignored.

Three ways to send the key, all equivalent:

| How the client sends it | `db_kwargs` |
|---|---|
| Basic auth | `{"username": "obsl", "password": "<key>"}` |
| `authorization` header | `{"adbc.flight.sql.authorization_header": "Bearer <key>"}` |
| `x-api-key` call header | `{"adbc.flight.sql.rpc.call_header.x-api-key": "<key>"}` |

```python
conn = dbapi.connect(
    "grpc://127.0.0.1:8815",
    db_kwargs={"username": "obsl", "password": "<your API key>"},
)
```

The first form is Flight SQL's `AuthenticateBasicToken`: the driver sends the key
once, the server answers with the bearer token to use from then on, and the
driver attaches it to every later call. The other two put the key on every call
directly, which is what a BI tool with a free-text header field can do.

A wrong or absent key fails the call with `UNAUTHENTICATED`, naming the header
to send — it never returns an empty result instead.

Flight's older `Handshake` — where the key travels on the stream rather than in
a header — keeps working alongside these, for clients that still speak it.

## From DuckDB

DuckDB can be the client. The `adbc_scanner` community extension makes it an
ADBC client, and the semantic layer is an ADBC server, so a plain DuckDB shell
queries governed measures and joins the result to local tables:

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
    'SELECT "Customer Country", "Total Revenue" FROM sales'
);
```

Two things it needs that the Python recipe does not: `adbc_scanner` is a
**community** extension rather than a bundled one, and `adbc_connect` wants a
filesystem path to the Flight SQL driver library. Any ADBC install has one -
`python -c "import adbc_driver_flightsql as d; print(d._driver_path())"` prints
it - but it is a path, not a package name.

`adbc_tables(handle)` lists the model and its metadata views, and
`adbc_schema(handle, 'model', schema := 'sales')` gives column types without
executing anything.

See **[Using DuckDB as a client](duckdb.md)** for the full guide, including the
`ATTACH ... (TYPE postgres)` route that addresses the model as a table rather
than a string in a table function, and which predicates reach the warehouse.

## What you can send

The Flight surface takes **OBSQL** — `SELECT <dimension|measure> FROM <model>`,
where the names are the model's, not the warehouse's. It is a semantic layer,
not a SQL proxy, so a statement is compiled against the model rather than
forwarded:

```sql
SELECT "Customer Country", "Total Revenue"
FROM sales
WHERE "Customer Country" = 'US'
```

Raw physical columns are addressable too, qualified by data object:

```sql
SELECT "Orders"."Amount" FROM sales
```

### What is refused, and why

| Statement | Result |
|---|---|
| `SELECT * FROM sales` | Refused. A model has no `*` — a star would have to invent a column list, and which columns a semantic layer returns is a governance decision. |
| `SELECT ... FROM nonexistent_relation` | Refused, as an error rather than an empty result. |
| Arbitrary warehouse SQL | Not forwarded. There is no escape hatch to the underlying database. |

A refusal arrives as an exception, not as zero rows — a client can tell
"nothing matched" from "that was not allowed".

## Prepared statements and parameters

Prepared statements bind over DoPut, and the client can discover what to bind:

```python
sql = (
    'SELECT "Customer Country", "Total Revenue" FROM sales '
    'WHERE "Customer Country" = ?'
)

with conn.cursor() as cur:
    parameters = cur.adbc_prepare(sql)
    print(parameters)                        # $1: string

    cur.execute(sql, parameters=("US",))
    us = cur.fetch_arrow_table()

    cur.execute(sql, parameters=("DE",))     # the same handle, bound again
    de = cur.fetch_arrow_table()
```

The parameter schema is **the model's**, not an inference: the column a
placeholder is compared against has a declared type, so a `decimal(18, 2)`
measure binds at that width and a count binds as an integer. A raw-mode
parameter takes the type its data object declares.

A bound value is substituted as a parsed literal, never spliced into text, so
it cannot change the shape of the statement it lands in — and what executes has
been through the same governance an inline literal gets.

| | |
|---|---|
| Rebinding a handle | Supported — that is what preparing is for. |
| Parameter *sets* (one execution per row) | Refused by name. Bind and execute once per row. |
| Wrong parameter count | Refused. Binding too few silently shifts every later value onto the wrong column. |
| Executing without binding | Refused, naming how many parameters are missing. |

## Catalog and discovery

`GetCatalogs`, `GetDbSchemas`, `GetTables`, `GetColumns` and `GetTableTypes`
are answered from the model — no warehouse hop. So are the SQL forms BI tools
send: `SHOW TABLES`, `DESCRIBE <model>`, and selects against
`information_schema.tables` / `.columns` and their `pg_catalog` equivalents.

These honour the statement they are given:

```sql
SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'
SELECT column_name FROM information_schema.columns WHERE ordinal_position > ?
```

Supported predicates are `=`, `<>`, `<`, `<=`, `>`, `>=`, `LIKE`, `ILIKE`,
`IN`, `IS NULL`, and `AND`/`OR`/`NOT` over them, with the column on either
side. `ORDER BY` applies too - by column, by select-list alias, or by ordinal
position, ascending or descending:

```sql
SELECT table_name FROM information_schema.tables
WHERE table_type = 'VIEW'
ORDER BY table_name DESC
```

A sort key may name a column the SELECT list drops, because ordering runs
before the projection. `LIMIT` and `OFFSET` apply after the sort, so which
rows survive is decided by the order you asked for. A predicate outside that set leaves the result unfiltered rather than
failing, since clients probe a long tail of system tables — but a *parameter*
cannot be bound into one, because a bound value that is then ignored is worse
than a refusal.

## Types

A column's type is a property of the model, not of the engine behind it. Where
a warehouse cannot express a declared type, OBSL reconciles it: MySQL has no
boolean, so a declared one arrives as `true`/`false` rather than `1`/`0`, and
Dremio's `date64[ms]` narrows to the `date32[day]` every other engine returns.

Reconciliation is deliberately narrow — an engine answering *more* precisely
than the declaration is not drift, so a `decimal(38, 2)` behind a measure
declared `float` is left alone. See
[Arrow type fidelity](../reference/type-fidelity.md) for the per-engine
measurements and what is refused.

## Known limitations

| | |
|---|---|
| Parameter sets | Not implemented; refused rather than truncated to the first row. |
| Catalog clauses | `WHERE`, the SELECT list, `ORDER BY`, `LIMIT` and `OFFSET` apply. `GROUP BY`, joins and subqueries do not - a catalog view is a list, not a query surface. |
| `CommandGetXdbcTypeInfo` | Streams a single `info: utf8` column rather than the spec shape. ADBC accepts it because the advertised schema matches the stream. |
| Statistics (`GetStatistics`) | Unreachable, not merely absent. Flight SQL carries no statistics command, so the `flightsql` driver refuses `adbc_get_statistics` and `adbc_get_statistic_names` in the client, before a request is put on the wire - nothing a server implements can answer them. A client planner that wants cardinalities has to get them from the warehouse. |
| Transactions | Flight SQL exposes no transaction to begin — OBSL is read-only. ADBC warns that autocommit cannot be disabled; the warning is expected. |
