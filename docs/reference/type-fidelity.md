# Arrow type fidelity

What Arrow type each engine's driver returns for a declared OBML type. A
semantic layer's promise is that a measure declared `decimal(18, 2)` arrives as
an exact fixed-point number; whether it does is a property of the driver and its
cast rendering, not of the SQL.

Measured **2026-09-04** with `scripts/probe_types.py`, against all eight engines
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
| `LOSSY` | The fixed-point type became a float or a string. |

## The matrix

| Declared | DuckDB | Postgres | MySQL | ClickHouse | Snowflake | BigQuery | Databricks | Dremio |
|---|---|---|---|---|---|---|---|---|
| `decimal(18,2)` | EXACT | LOSSY | WIDENED | EXACT | WIDENED | WIDENED | EXACT | EXACT |
| `decimal(38,9)` | EXACT | LOSSY | WIDENED | EXACT | EXACT | EXACT | EXACT | EXACT |
| `decimal(18,2) big` | EXACT | LOSSY | WIDENED | EXACT | WIDENED | WIDENED | EXACT | EXACT |
| `SUM decimal(18,2)` | WIDENED | LOSSY | WIDENED | WIDENED | WIDENED | WIDENED | WIDENED | WIDENED |
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
| `decimal(18,2) big` | `decimal128(19, 2)` | `extension<arrow.opaque[storage_type=string, type_name=numeric, vendor_name=PostgreSQL]>` | `decimal256(76, 2)` | `decimal128(19, 2)` | `decimal128(38, 2)` | `decimal128(38, 9)` | `decimal128(19, 2)` | `decimal128(19, 2)` |
| `SUM decimal(18,2)` | `decimal128(38, 2)` | `extension<arrow.opaque[storage_type=string, type_name=numeric, vendor_name=PostgreSQL]>` | `decimal256(76, 2)` | `decimal128(38, 2)` | `decimal128(38, 2)` | `decimal128(38, 9)` | `decimal128(28, 2)` | `decimal128(38, 2)` |
| `integer` | `int32` | `int32` | `int64` | `int32` | `int64` | `int64` | `int32` | `int32` |
| `bigint` | `int64` | `int64` | `int64` | `int64` | `int64` | `int64` | `int64` | `int64` |
| `double` | `double` | `double` | `double` | `double` | `double` | `double` | `double` | `double` |
| `string` | `string` | `string` | `string` | `string` | `string` | `string` | `string` | `string` |
| `boolean` | `bool` | `bool` | `int64` | `bool` | `bool` | `bool` | `bool` | `bool` |
| `date` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date32[day]` | `date64[ms]` |
| `timestamp` | `timestamp[us]` | `timestamp[us]` | `timestamp[us]` | `timestamp[ms, tz=Europe/Berlin]` | `timestamp[ns]` | `timestamp[us]` | `timestamp[us]` | `timestamp[ms]` |

## Reading the two rows that look alarming

**Postgres `LOSSY string` on every decimal is correct**, and is the clearest
reason this table is not the whole story. ADBC represents `NUMERIC` as
`arrow.opaque[storage_type=string, type_name=numeric]` because Postgres NUMERIC
is arbitrary precision with NaN and Infinity, Arrow's `decimal128` caps at 38
digits and needs the scale up front, and typmod is `-1` for a computed
expression. Preserving the exact digits as text is the faithful choice, and
`db_executor` parses those cells back to `Decimal` before any caller sees them.
The driver is lossy here; **OBSL is not**.

**ClickHouse `ZONED` on `timestamp` is intrinsic.** ClickHouse has no naive
`DateTime` - the type is an instant rendered against the server timezone. Every
OBSL surface reconciles it against the declared wall clock; see the Flight
alignment in `ob_flight/server_execution.py`.

## Open gaps

| Engine | Gap |
|---|---|
| MySQL | `boolean` is **lossy**: MySQL has no boolean type, so a declared one arrives as `int64` `1`. |
| Dremio | `date` returns `date64[ms]` where the other seven give `date32[day]`. |

## What CI asserts

This table measures **drivers**. Every type defect OBSL has actually shipped
lived between the driver and the caller instead - the cache codec typing a
column from the values it happened to hold (#410), the executor never importing
pyarrow so no result carried a schema at all (#412) - and a driver-level probe
is blind to all of them.

So `tests/integration/test_type_fidelity.py` asserts through
`db_executor.execute_sql`, the path REST, pgwire and the CLI share, on DuckDB -
the one engine needing no credentials, and therefore the one every CI machine
can reach. The other seven stay in this table rather than in CI: a suite that
skips six of eight rows reports green for a matrix nobody measured.
