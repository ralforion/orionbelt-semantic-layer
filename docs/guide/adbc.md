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
`grpc://<host>:8815` — or `grpc+tls://` behind TLS.

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

With `AUTH_MODE=api_key`, the Flight server validates the handshake credential
against the same key store the REST surface uses. The key goes in as the
Basic-auth password:

```python
conn = dbapi.connect(
    "grpc://127.0.0.1:8815",
    db_kwargs={"username": "obsl", "password": "<your API key>"},
)
```

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
| Statistics (`GetStatistics`) | Not implemented. |
| Transactions | Flight SQL exposes no transaction to begin — OBSL is read-only. ADBC warns that autocommit cannot be disabled; the warning is expected. |
