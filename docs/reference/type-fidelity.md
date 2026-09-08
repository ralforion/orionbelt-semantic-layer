---
description: "Which Arrow type each engine's driver returns for a declared OBML type, so a client knows what to expect across the eight supported dialects."
---

# Arrow type fidelity

What Arrow type each engine's driver returns for a declared OBML type. A
semantic layer's promise is that a measure declared `decimal(18, 2)` arrives as
an exact fixed-point number; whether it does is a property of the driver and its
cast rendering, not of the SQL.

Measured **2026-09-06** with `scripts/probe_types.py`, against all eight engines
live. Each case is rendered through the engine's own `cast_to_obml_type`, so
this is what OBSL emits rather than a hand-spelled approximation.

```bash
uv run python scripts/probe_types.py all          # human-readable
uv run python scripts/probe_types.py --json all   # regenerate the data below
```

## Verdicts

| Verdict | Meaning |
|---|---|
| `EXACT` | The declared type came back unchanged. |
| `WIDENED` | Still fixed-point, at a wider precision or scale. An engine widening a `SUM` is doing the right thing; a widened *cast* is the engine's own decimal rules, recorded rather than judged. |
| `FAMILY` | Right family, different width - an `int64` where the model said `integer`. |
| `ZONED` | A `timestamp` came back carrying a timezone. OBML's `timestamp` is a wall clock. |
| `TEXT` | A number carried as a string with every digit intact - ADBC does this for Postgres NUMERIC, whose precision Arrow's decimal cannot hold. `db_executor` parses the cells back to `Decimal`, so nothing is lost. |
| `LOSSY` | The fixed-point type became a float, or a string that does not carry the digits. This is the one that costs data. |

## The matrix

| Declared | DuckDB | Postgres | MySQL | ClickHouse | Snowflake | BigQuery | Databricks | Dremio |
|---|---|---|---|---|---|---|---|---|
| `decimal(18,2)` | EXACT | TEXT | WIDENED | EXACT | WIDENED | WIDENED | EXACT | EXACT |
| `decimal(38,9)` | EXACT | TEXT | WIDENED | EXACT | EXACT | EXACT | EXACT | EXACT |
| `decimal(19,2) big` | EXACT | TEXT | WIDENED | EXACT | WIDENED | WIDENED | EXACT | EXACT |
| `SUM decimal(18,2)` | WIDENED | TEXT | WIDENED | WIDENED | WIDENED | WIDENED | WIDENED | WIDENED |
| `integer` | EXACT | EXACT | FAMILY | EXACT | FAMILY | FAMILY | EXACT | EXACT |
| `bigint` | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT |
| `double` | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT |
| `string` | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT |
| `boolean` | EXACT | EXACT | LOSSY | EXACT | EXACT | EXACT | EXACT | EXACT |
| `date` | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | EXACT | FAMILY |
| `timestamp` | EXACT | EXACT | EXACT | ZONED | EXACT | EXACT | EXACT | EXACT |

### Arrow types returned

| Declared | DuckDB | Postgres | MySQL | ClickHouse | Snowflake | BigQuery | Databricks | Dremio |
|---|---|---|---|---|---|---|---|---|
| `decimal(18,2)` | `decimal128(18, 2)` | `extension<arrow.opaque[storage_type=string, type_name=numeric, vendor_name=PostgreSQL]>` | `decimal256(76, 2)` | `decimal128(18, 2)` | `decimal128(38, 2)` | `decimal128(38, 9)` | `decimal128(18, 2)` | `decimal128(18, 2)` |
| `decimal(38,9)` | `decimal128(38, 9)` | `extension<arrow.opaque[storage_type=string, type_name=numeric, vendor_name=PostgreSQL]>` | `decimal256(76, 9)` | `decimal128(38, 9)` | `decimal128(38, 9)` | `decimal128(38, 9)` | `decimal128(38, 9)` | `decimal128(38, 9)` |
| `decimal(19,2) big` | `decimal128(19, 2)` | `extension<arrow.opaque[storage_type=string, type_name=numeric, vendor_name=PostgreSQL]>` | `decimal256(76, 2)` | `decimal128(19, 2)` | `decimal128(38, 2)` | `decimal128(38, 9)` | `decimal128(19, 2)` | `decimal128(19, 2)` |
| `SUM decimal(18,2)` | `decimal128(38, 2)` | `extension<arrow.opaque[storage_type=string, type_name=numeric, vendor_name=PostgreSQL]>` | `decimal256(76, 2)` | `decimal128(38, 2)` | `decimal128(38, 2)` | `decimal128(38, 9)` | `decimal128(28, 2)` | `decimal128(38, 2)` |
| `integer` | `int32` | `int32` | `int64` | `int32` | `int64` | `int64` | `int32` | `int32` |
| `bigint` | `int64` | `int64` | `int64` | `int64` | `int64` | `int64` | `int64` | `int64` |
| `double` | `double` | `double` | `double` | `double` | `double` | `double` | `double` | `double` |
| `string` | `string` | `string` | `string` | `string` | `string` | `string` | `string` | `string` |
| `boolean` | `bool` | `bool` | `int64` | `bool` | `bool` | `bool` | `bool` | `bool` |
| `date` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date64[ms]` |
| `timestamp` | `timestamp[us]` | `timestamp[us]` | `timestamp[us]` | `timestamp[ms, tz=Europe/Berlin]` | `timestamp[ns]` | `timestamp[us]` | `timestamp[us]` | `timestamp[ms]` |

## What the table measures, and what OBSL delivers

Every row above is a measurement of a **driver**. That is not the same as what
a caller receives, because OBSL reconciles a result against the type the model
*declared* before anyone sees it. Four rows read alarming and are not.

**Postgres `TEXT` on every decimal costs nothing.** ADBC represents `NUMERIC` as
`arrow.opaque[storage_type=string, type_name=numeric]` because Postgres NUMERIC
is arbitrary precision with NaN and Infinity, Arrow's `decimal128` caps at 38
digits and needs the scale up front, and typmod is `-1` for a computed
expression. Preserving the exact digits as text is the faithful choice - it is
what lets a NUMERIC wider than Arrow's 38 digits survive at all - and
`db_executor` parses those cells back to `Decimal` before any caller sees them.
Measured: `12345678901234567.89::numeric(19,2)` comes back
`Decimal('12345678901234567.89')`.

This row read `LOSSY` until 2026-09-06, which contradicted this page's own
definition of the verdict: nothing became a float, and the string carries every
digit. It has its own verdict now.

**ClickHouse `ZONED` on `timestamp` is intrinsic.** ClickHouse has no naive
`DateTime` - the type is an instant rendered against the server timezone. Every
OBSL surface reconciles it against the declared wall clock; see the Flight
alignment in `ob_flight/server_execution.py`.

**MySQL `LOSSY` on `boolean` and Dremio `FAMILY` on `date` are reconciled.**
MySQL has no boolean type, so a declared one arrives as `int64` `1`; Dremio
returns `date64[ms]` where the other seven give `date32[day]`. Both are still
true of the drivers, and neither reaches a caller: `service/result_schema.py`
casts them to the declared type on every surface.

```
MySQL, a dimension declared boolean
  driver returns      int64      1, 0
  REST returns        boolean    true, false
  pgwire advertises   OID 16     t, f
```

Reconciliation is an allowlist of exactly those two cases, not a general cast.
A `resultType` names a *family*, so an engine answering more precisely than the
declaration - a `decimal(38, 2)` behind a measure declared `float` - is not
drift and is left alone. Timestamps are excluded too: ClickHouse's zoned-for-
naive case needs the wall clock preserved value by value rather than converted.

## When reconciliation is refused

A declared boolean holding something other than `0`, `1` or `NULL` is not cast:
Arrow maps every nonzero to `true`, and asserting that a column holding `7` is
`true` invents content the model never claimed. The column keeps the engine's
type, the response *reports* that type rather than the declared one, and the
declaration is carried as a warning:

```json
{
  "code": "DECLARED_TYPE_NOT_APPLIED",
  "message": "Column 'Tier' was not reconciled to its declared type: declared boolean
              but the column holds values other than 0 and 1; left as int64"
}
```

So `type` always describes the bytes beside it, and `warnings` tells you where
the model wanted otherwise.

## What CI asserts

Every type defect OBSL has actually shipped lived between the driver and the
caller, not in the driver - the cache codec typing a column from the values it
happened to hold (#410), the executor never importing pyarrow so no result
carried a schema at all (#412), the same reconciliation missing from one read
path at a time (#414) - and a driver-level probe is blind to all of them.

So `tests/integration/test_type_fidelity.py` asserts through
`db_executor.execute_sql`, the path REST, pgwire and the CLI share, on DuckDB -
the one engine needing no credentials, and therefore the one every CI machine
can reach. The other seven stay in this table rather than in CI: a suite that
skips six of eight rows reports green for a matrix nobody measured.

Two structural guards cover what those defects had in common, both of which
were one rule duplicated across surfaces: the Arrow-to-hint mapping is a single
function rather than one per surface (asserted by identity, in the Flight
driver's suite), and any code rebuilding a cached result must reconcile the
table and hand its findings to the response
(`tests/architecture/test_cached_hits_reconcile.py`).
