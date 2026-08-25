---
description: "The portable scalar-function catalog: 41 entries whose meaning OBSL pins across all eight dialects, with the engine disagreements each one settles."
---

# Portable functions

A model that uses a warehouse's own functions is a model that runs on one
warehouse. OBSL carries a **catalog of 41 scalar functions** whose meaning it
owns, and renders each one per dialect.

Most of that is about **spelling**. DuckDB counts characters with `length`,
ClickHouse with `lengthUTF8`; picking the right name is the whole job, and every
portability layer does it.

The harder half is that engines disagree about the **answer**. `ROUND(x)` exists
on every one of them. It runs on every one of them. And on a `DOUBLE PRECISION`
column holding 2.5 it returns `3` on DuckDB and `2` on ClickHouse, PostgreSQL
and MySQL. Neither is a bug — they are simply different numbers, and a catalog
that fixed only the name would hand that difference to your dashboard.

Worse, those three do not answer consistently *within* one engine. They round
ties **to even for their float type and away from zero for their decimal type**,
and they document both halves:

| | float `round(2.5)` | decimal `round(2.5)` |
|---|---|---|
| DuckDB, BigQuery, Snowflake, Databricks | 3 | 3 |
| ClickHouse, PostgreSQL, MySQL | **2** | 3 |

So the same column, widened from `NUMERIC` to `DOUBLE PRECISION` by a well-meant
migration, quietly changes the number in your report.

This is also why it is easy to miss. A bare `ROUND(2.5)` typed into a console
answers **3** on PostgreSQL and MySQL, because a decimal literal is `numeric`
there and `DECIMAL` on MySQL — the types those engines already round the way you
expect. Only ClickHouse, whose literal is a `Float64`, shows the difference
without a table in the query. The disagreement lives in your columns, not in
your literals.

The catalog states what a call *means* and bends the engine to it. `round` means
ties go away from zero, so `round(2.5)` is 3 everywhere. Each of those three
already rounds its *decimal* type that way, so the work is reaching that half
without disturbing the decimal on the way:

```sql
ROUND(x)                                              -- DuckDB, BigQuery, Snowflake, Databricks, Dremio
ROUND(CAST(x AS numeric))                             -- PostgreSQL
TRUNCATE(x + SIGN(x) * 0.5, 0)                        -- MySQL
truncate(x + SIGN(x) * toDecimal256('0.5', 1), 0)     -- ClickHouse
```

Two shapes, and which one an engine gets depends on whether it has a decimal
type that can hold anything.

**PostgreSQL does.** Its `numeric` is unbounded, so casting to it names no
width, loses nothing, and its own `ROUND` then rounds the way the catalog wants.
The cast earns its place twice over there: PostgreSQL has no
`round(double precision, integer)` at all, so `round(x, 2)` over a float column
used to raise rather than answer.

**MySQL and ClickHouse do not**, so nothing is cast. A cast has to name a width,
and on those two that width is a loss either way — MySQL's `DECIMAL` is 65
digits split between the two sides, so `CAST(1e50 AS DECIMAL(65, 18))` saturates
silently to `999…9`, and ClickHouse's conversion from `Float64` scales by a
power of ten *in floating point*, so `round(toDecimal256(1e19, 18))` gives
`9999999999999999539` where the answer is `1e19`. An infinity cannot be
converted at all.

So they add half of the last kept place and truncate, which is the same
operation and needs no conversion. The arithmetic runs in whatever type
arrived, and only the **half** is written as an exact decimal: `0.5` at zero
places, `0.005` at two, `50` at minus two. A bare `0.005` already *is* a
`DECIMAL` to MySQL; to ClickHouse it is a `Float64`, and `Decimal + Float64` is
a `Float64`, so there it is quoted and passed through `toDecimal256`.
ClickHouse's own promotion then does the rest — `Decimal + Decimal` stays a
`Decimal`, `Float64 + Decimal` stays a `Float64` — so one expression preserves
whichever type it is handed.

Two ends of the digit count are special, and they are not symmetric. Rounding to
**at least as many places as the decimal type carries** cannot change anything,
so the call is the identity there and no arithmetic is emitted. A **negative**
count is not identity at any size — `round(1e40, -41)` is 0 — and truncating
stops working once the count passes the value's own magnitude, so those divide
by the factor, round at zero places, and put the scale back. The factor has to
out-scale the *value* rather than the type: `9e64` is an ordinary `DECIMAL(65)`,
and `round(9e64, -5000)` is 0, not the `1e65` a type-sized factor would give.
Past the largest finite double no factor is coarse enough, and every
representable number rounds to zero there anyway.

One ClickHouse limit is worth stating, because no expression avoids it. The
half promotes a `Decimal256` to `Decimal(76, n+1)`, so a value carrying more
than `76 - (n+1)` integer digits wraps rather than raising. It is bounded by
arithmetic rather than by luck: 76 digits in total means that many integer
digits force the scale to `n` or less, and a value already at that scale is
unchanged by rounding to `n` places — so every value this can spoil is one it
had no work to do on. Wherever the rounding is real, the rewrite agrees with
ClickHouse's own `ROUND`, and a test pins that up to the width of the type.

One consequence is deliberate: on PostgreSQL `round` gives back `numeric`
rather than a float.

One of those places is a *type* rather than a function. ClickHouse pads a
`FixedString` to its declared width with NUL bytes, and they count as content:
a `FixedString(50)` holding `Books` answers **50** to `length`, comes back from
`upper` still carrying 45 of them, and makes `ends_with(x, 'ks')` **false**.
`replace` and `split_part` refuse it outright. That is not exotic — TPC-DS
types its `CHAR` columns that way, following ClickHouse's own published DDL. So
on ClickHouse the text arguments of a string function are read through
`toString`, which strips the padding, is the identity on a `String`, and carries
`Nullable` through unchanged. Numeric arguments and literals are left alone.

Five more places where the engines differ on the answer, not the name:

| expression | what engines do | what OBSL returns |
|---|---|---|
| `trunc(-1.9)` | Databricks has no numeric truncation at all | **-1**, everywhere |
| `greatest(1, NULL, 3)` | four engines skip the NULL and answer 3 | **NULL**, everywhere |
| `length('äbcd')` | ClickHouse and MySQL count bytes and answer 5 | **4** — characters |
| `position('cd', 'abcd')` | ClickHouse takes the haystack first and answers 0 | **3** — needle first |
| `json_value(doc, '$.a')` where `a` is an object | four engines return serialized JSON | **NULL** — the path did not reach a scalar |

Every entry carries examples that are **executed** against live engines and
asserted against the documented value, rather than derived from vendor
documentation. That distinction earned its place: several rows above are not
what the engine's own docs say.

A call the catalog does not carry still works — it is emitted verbatim, and the
model is pinned to whatever engines spell it that way. Setting
`settings.expressionMode: portable` turns that into an error instead, so an
engine dependency cannot be picked up by accident.

## The catalog

A function call inside an `expression` — a computed column, a measure expression,
a metric formula — is either **in the catalog**, in which case OBSL owns what it
means and renders it per dialect, or **outside it**, in which case the call is
emitted verbatim and the model is pinned to whatever engines happen to spell it
that way.

Canonical names are lowercase and snake_case; OBML is case-insensitive about
them, so `SUBSTRING(...)` and `substring(...)` are the same entry. `?` marks an
optional argument.

Every entry renders on all eight dialects. On Databricks `json_value` reads
through `try_variant_get` behind a `schema_of_variant` guard, available on
Databricks SQL and on Runtime 15.3 or
above; on Dremio through `TRY_CONVERT_FROM(x AS ROW(...))`, whose row type is
built from the literal path at compile time and whose member names are quoted,
since Dremio is the one dialect that puts them in identifier position.

| Signature | Result | Pinned meaning |
|---|---|---|
| `substring(x, start, len?)` | string | 1-based; omitting `len` runs to the end |
| `concat(a, b, ...)` | string | **NULL propagates** — any NULL argument makes the result NULL |
| `upper(x)` / `lower(x)` | string | Case mapping of non-ASCII follows the engine's collation |
| `trim(x)` / `ltrim(x)` / `rtrim(x)` | string | Whitespace only |
| `length(x)` | int | **Characters, not bytes** |
| `replace(x, from, to)` | string | All occurrences |
| `position(needle, haystack)` | int | 1-based, `0` when absent; needle first |
| `split_part(x, delim, n)` | string | 1-based; an `n` past the last field yields `''` |
| `lpad(x, len, fill)` / `rpad(x, len, fill)` | string | Longer input is truncated to `len` |
| `starts_with(x, prefix)` / `ends_with(x, suffix)` | boolean | Case-sensitive |
| `abs(x)`, `sign(x)`, `floor(x)`, `ceil(x)`, `sqrt(x)`, `ln(x)`, `exp(x)` | numeric | `sign` is -1, 0 or 1 |
| `power(base, exponent)` | float | |
| `round(x, n?)` | float | **Ties round away from zero** — 2.5 is 3, -2.5 is -3 |
| `trunc(x, n?)` | float | **Toward zero** — -1.9 is -1, where floor gives -2 |
| `mod(a, b)` | numeric | The result takes the sign of the dividend |
| `div(a, b)` | int | **Integer division, truncating toward zero** — the only way to ask for it |
| `log(base, x)` | float | **Base first**; use `ln(x)` for the natural logarithm |
| `coalesce(a, b, ...)` | argument | The first argument that is not NULL |
| `nullif(a, b)` | argument | NULL when `a` equals `b` |
| `greatest(a, b, ...)` / `least(a, b, ...)` | argument | **NULL propagates**, as for `concat` |
| `date_trunc(unit, x)` | timestamp | Start of the unit; a **week starts Monday** (ISO 8601) |
| `date_add(unit, n, x)` | timestamp | Negative `n` subtracts, so there is no `date_sub` |
| `date_diff(unit, start, end)` | int | **Boundaries crossed**, signed — not complete units elapsed |
| `extract(unit, x)` | int | **ISO week numbering**; an integer, not a numeric |
| `last_day(x)` | date | Last day of `x`'s month |
| `current_date()` | date | Today, per the database session |
| `json_value(x, path)` | string | Scalar at a **literal JSONPath**; NULL when absent or when the path resolves to an object or array |
| `cast(x, 'type')` | argument | To a **quoted OBML type**, `decimal(p, s)` or `double` only; a decimal target rounds ties away from zero |
| `to_number(x)` | float | The number `x` names, or **NULL** when it does not name one; surrounding whitespace ignored |

## Casting, and what it does not promise

`cast(x, 'decimal(18, 2)')` and `cast(x, 'double')`. The target is a quoted
**OBML** type, never a SQL one: naming `NUMERIC` or `Decimal64` is the vendor
leak the abstract type map exists to prevent, and the OBML name is what carries
BigQuery's `ROUND` wrap for a parameterized decimal and MySQL's widening to 38
digits.

A decimal target **rounds to its scale, ties away from zero**, the same rule
`round` pins. Seven engines already did; ClickHouse rounds a *float's* ties to
even, so 2.5 to `decimal(18, 0)` came back 2 there. It rounds a *decimal's* ties
away from zero, so the call converts to an exact decimal first — the same move
`round` makes on PostgreSQL, handing the engine the type it already rounds
correctly. That conversion is also what lets ClickHouse take a **text**
argument at all: `round('4.6', 2)` raises there, so a cast over a `json_value`
— which returns a string by definition — did not compile to something the
engine would run.

**What it does not pin is failure**, and that limit is the point of the entry
rather than a footnote on it. A cast over a number is portable. A cast over
*text* is only as portable as the text is clean:

| `cast('abc', 'double')` | answer |
|---|---|
| DuckDB, PostgreSQL, BigQuery, Snowflake, Databricks | error |
| ClickHouse | NULL |
| MySQL | **0** |

Measured, one `SELECT` per engine. A JSON field is exactly this case, since
`json_value` returns a string, which is what `to_number` is for.

## to_number, where NULL is the pinned answer

`to_number(x)` is the other half of `cast`: **text that does not name a number
is NULL, on all eight dialects**. Five engines have a form that says so —
`TRY_CAST` on DuckDB, Snowflake and Databricks, `SAFE_CAST` on BigQuery,
`toFloat64OrNull` on ClickHouse. PostgreSQL, MySQL and Dremio have none at any
version, so on those three the text is tested against a numeric pattern
**before** it is converted. Testing afterwards is not an option on MySQL: its
failure is a silent `0`, and nothing downstream can tell that from a genuine
zero.

Surrounding whitespace is ignored, because the engines split on it — `' 42 '` is
42 to DuckDB's `TRY_CAST` and NULL to ClickHouse's `toFloat64OrNull` — so the
argument is trimmed and the answer is 42 everywhere.

**Magnitude is not pinned**, and it splits four ways. `to_number('1e999')` is
infinity on DuckDB, ClickHouse, BigQuery, Databricks and Dremio, the largest
double on MySQL, NULL on Snowflake, and an exact unbounded `numeric` on
PostgreSQL — which
is the result type there, the same consequence `round` has on that engine, and
the reason a pattern test suffices for it where it would not over a float.
Pinning that would mean deciding 1e999 is not a number, which it is.

```yaml
Rate:
  expression: "to_number(json_value({Tags}, '$.rate'))"
  abstractType: float
```

**Targets left out**, each because the engines answer differently and the
catalog only carries what it can pin:

| target | what splits |
|---|---|
| `integer`, `bigint` | 2.5 is **3** on DuckDB, PostgreSQL, MySQL, BigQuery and Snowflake, **2** on ClickHouse and Databricks |
| `string` | 2.50 is **'2.50'** on DuckDB, PostgreSQL, MySQL and Databricks, **'2.5'** on BigQuery, ClickHouse and Snowflake |
| `date` | `'08/15/2026'` is accepted on PostgreSQL — because its `DateStyle` says MDY, which OBSL does not set — and on Snowflake, NULL on MySQL and ClickHouse, an error on the rest |
| `timestamp` | the value agrees, the type does not: zoned on PostgreSQL, Snowflake, BigQuery and Databricks |
| `boolean` | MySQL has no cast target for it at all |

## JSON access

`json_value`'s `path` must be a **literal**, not an expression: the engines do
not merely spell the call differently, they take the path apart differently.
Postgres wants the segments as separate arguments, Snowflake wants them dotted
without the `$`, and the rest take the JSONPath verbatim. The accepted subset is
object member access and array subscripts rooted at `$` — `$.a`, `$.a.b`,
`$.a[0]`, at least one of them. The bare root `$` is not accepted: it is not a
path to a scalar, and the entry already answers NULL for an object or array.

The scalar comes back as a string, so `1` reads as `'1'`.

A path resolving to an **object or array** is NULL, and that rule is enforced
rather than inherited: DuckDB, Postgres, Snowflake, MySQL and Databricks all
return the *serialized JSON* for a non-scalar path, so each is wrapped in a
type guard (`json_type`, `json_typeof`, `TYPEOF`, `JSON_TYPE`,
`schema_of_variant`). BigQuery and ClickHouse already answer NULL. Dremio alone
gets the rule from a cast that declines rather than fails,
`TRY_CONVERT_FROM(x AS ROW(… VARCHAR))`, whose innermost `VARCHAR` will not
accept an object or an array. Reach an array element with a subscript instead —
`json_value(x, '$.arr[0]')`.

ClickHouse is the one remaining deviation: it returns the empty string for an
absent path, so the call is wrapped in `nullIf(..., '')`. That restores NULL for
the common case but cannot distinguish an absent path from a genuine
empty-string value — both are NULL there.

A path that is not a literal from the accepted subset is rejected with
`INVALID_JSON_PATH`. Without that check the call would still compile, falling
through to the pass-through path and emitting verbatim SQL, which would slip
past both `expressionMode: portable` and a dialect's unsupported-function
guard.

The date/time entries take a **literal unit** from a closed vocabulary — `year`,
`quarter`, `month`, `week`, `day`, `hour`, `minute`, `second` — and it has to be
a literal, not an expression: every dialect switches on it to render the call at
all (a keyword on BigQuery and ClickHouse, a quoted string on Snowflake, an
interval qualifier on MySQL, a different expression per unit on Postgres). A unit
outside the vocabulary, or one that is not a literal, is rejected with
`UNKNOWN_TIME_UNIT`.

The pinned meaning is the point. Six of those rules are places where engines
disagree on the *answer* rather than on the spelling, and OBSL rewrites the call
so every engine gives the catalog's answer:

- `concat('a', NULL, 'c')` is NULL. DuckDB, Postgres and Dremio skip NULL
  arguments in their own `CONCAT`, so on those dialects the call is rendered as
  a `||` chain (DuckDB, Postgres) or a NULL-guarded `CASE` (Dremio).
- `greatest(1, NULL, 3)` is NULL, for the same reason and by the same guard on
  DuckDB, Postgres, ClickHouse and Databricks. To take the largest of the
  values that are *present*, say so: `greatest(coalesce({A}, 0), coalesce({B}, 0))`.
- `length('äbcd')` is 4. ClickHouse and MySQL count bytes in `LENGTH`, so they
  render `lengthUTF8` and `CHAR_LENGTH`.
- `split_part('a,b,c', ',', 9)` is `''`. MySQL's `SUBSTRING_INDEX` would hand
  back the *last* field and BigQuery's `SPLIT` would return NULL, so both get a
  guard.
- `round(2.5)` is 3. ClickHouse, PostgreSQL and MySQL round ties to even for
  floats and away from zero for decimals. PostgreSQL gets an exact-decimal cast
  and its own `ROUND` does the rest; MySQL and ClickHouse add half of the last
  kept place and truncate, because a cast there has to name a width and every
  width loses something.
- `trunc(-1.9)` is -1. Databricks has no numeric truncation at all (its `trunc`
  takes a date), so it becomes a signed floor of the magnitude.
- `date_diff('day', TIMESTAMP '2026-08-01 23:00:00', TIMESTAMP '2026-08-02 01:00:00')`
  is 1, and `date_diff('month', DATE '2026-01-31', DATE '2026-03-01')` is 2:
  boundaries crossed, not complete units. MySQL's `TIMESTAMPDIFF` answers 0 and 1,
  so both ends are truncated to the unit before it runs, and Postgres has no such
  function at all and gets one built out of arithmetic.
- `extract('week', DATE '2026-08-15')` is 33. MySQL's `WEEK` and BigQuery's `WEEK`
  are Sunday-based and answer 32, so they render `WEEK(x, 3)` and `ISOWEEK`. The
  two are not disagreeing about the date: ISO puts week 1 on the week containing
  the first Thursday, so 2026-01-01 (a Thursday) is already week 1, while the
  Sunday convention calls 1–3 January *week 0* and every later week is one lower.
- `date_diff('week', DATE '2026-08-09', DATE '2026-08-15')` is 1 — one Monday
  separates that Sunday from that Saturday. ClickHouse, Snowflake and BigQuery
  agree; DuckDB and MySQL count whole seven-day spans and answer 0, and Postgres
  has no week difference at all, so the week unit is measured rather than
  delegated on every engine: both ends are truncated to the week start and the
  day difference divided by seven.

## Which time zone a timestamp is read in

Bucketing happens inside the warehouse, and for a column that carries an instant
(`timestamp_tz`, Snowflake `TIMESTAMP_LTZ`, Postgres `timestamptz`) the answer
depends on the session's time zone. The same stored instant, read on three
Snowflake sessions:

```
TIMEZONE=Europe/Zagreb        2026-08-10 00:30+02:00  ->  week of 2026-08-10
TIMEZONE=UTC                  2026-08-09 22:30+00:00  ->  week of 2026-08-03
TIMEZONE=America/Los_Angeles  2026-08-09 15:30-07:00  ->  week of 2026-08-03
```

`settings.queryTimezone` takes that decision away from the connection:

```yaml
settings:
  queryTimezone: Europe/Zagreb   # bucket and report in this zone
  defaultTimezone: UTC           # what our naive timestamp columns mean
```

A timestamp column is then converted **at the column**, so every expression
reading it starts from the same frame — `AT TIME ZONE` on DuckDB and Postgres,
`toTimeZone` on ClickHouse, `CONVERT_TIMEZONE` on Snowflake and Dremio,
`CONVERT_TZ` on MySQL, `DATETIME(x, zone)` on BigQuery, `from_utc_timestamp` on
Databricks. Converting at the column rather than around an expression is what
keeps a conversion from being applied twice: on MySQL, the same conversion
applied twice moves 00:30 to 02:30.

Two column kinds are treated differently, because they are different questions:

| Column | Behaviour |
|---|---|
| `timestamp_tz` | carries an instant, so it is read in `queryTimezone` directly |
| `timestamp` (naive) | carries no zone, so it is first read as `defaultTimezone` — and if that is unset it is **left alone**, with an `UNDECLARED_TIMESTAMP_ZONE` warning, rather than guessed at |
| `date`, `time` | never converted — a date has no instant to move |

The session's own zone is deliberately not used as the fallback: it is a fact
about the connection, not about the data, and reading it into the SQL would make
the same query mean different things on different connections.

## Changing the week start

`date_trunc('week', …)` and `date_diff('week', …)` follow `settings.weekStart`:

```yaml
settings:
  weekStart: sunday   # default: monday (ISO 8601)
```

It governs **every** weekly path, not just the function: a `timeGrain: week`
dimension, a weekly period-over-period, and an explicit `date_trunc('week', …)`
all bucket the same rows the same way, because they render through one
implementation per dialect.

Under `sunday`, `date_trunc('week', DATE '2026-08-15')` is `2026-08-09` rather
than `2026-08-10`, on every dialect — including the six whose native truncation
only knows Monday, which are rewritten. `extract('week', …)` is deliberately
*not* affected: a Sunday-start week *number* has no definition the engines agree
on (MySQL alone offers eight numbering modes), so week numbering stays ISO and
says so rather than picking one silently.

Argument order and shape are rewritten wherever an engine needs it —
`position(needle, haystack)` becomes `POSITION(needle IN haystack)` on most
dialects and `STRPOS(haystack, needle)` on BigQuery; `log(base, x)` is reversed
on BigQuery and changes base through `log10` on ClickHouse, which has no
two-argument logarithm; `div(a, b)` is a function on three engines, an operator
on three, and a truncated quotient on Snowflake.

Date literals are written `DATE '2026-08-15'` and `TIMESTAMP '2026-08-15 13:45:00'`,
and compile to a cast, so they mean the same thing on every engine.

Three neighbours are deliberately *not* in the catalog. `current_timestamp` is
left out because the engines disagree on whether it carries a time zone, and
pinning that needs a stated stance on session time zones rather than a rewrite —
`current_date()` has no such ambiguity. `to_date` and `format_date` are left out
because format strings are strftime-style on Postgres, DuckDB and ClickHouse,
picture strings on Snowflake and `%`-style on BigQuery, which is its own problem
rather than a rewrite. The single-argument `log`
is base 10 on DuckDB and Postgres and natural on ClickHouse, MySQL and BigQuery,
a silent factor of 2.3, so only the explicit `log(base, x)` is admitted. And
`/` is left to the engine: it is float division everywhere except Postgres,
where `7 / 2` is 3, so ask for integer division with `div(a, b)` and write
`{A} * 1.0 / {B}` when you mean the float.

```yaml
Clients:
  columns:
    Client Email: { code: clientemail, abstractType: string }
    Client Email Domain:
      abstractType: string
      expression: "split_part({Client Email}, '@', 2)"
```

That column compiles to `SPLIT_PART(...)` on DuckDB, Postgres, Snowflake and
Databricks, `splitByString(...)[2]` on ClickHouse, `IFNULL(SPLIT(...)[SAFE_OFFSET(1)], '')`
on BigQuery, and a guarded `SUBSTRING_INDEX` on MySQL — same model, same values.
The bundled `examples/orionbelt_1_commerce.yaml` carries this column plus
`Client Initial` (`upper(substring(...))`) and `Product Label` (`concat(...)`).

!!! warning "Behaviour change for models written before the catalog"

    A call the catalog carries is now rendered per the catalog's meaning
    rather than passed to the engine, so three expressions changed their
    answer on the engines that disagreed. Everything else renders as before,
    and several calls that used to fail now work.

    | Expression | Dialects | Before | Now |
    |---|---|---|---|
    | `concat('a', NULL, 'c')` | DuckDB, Postgres, Dremio | `'ac'` | `NULL` |
    | `length('äbcd')` | ClickHouse, MySQL | `5` (bytes) | `4` (characters) |
    | `position('cd', 'abcd')` | ClickHouse | `0` (haystack first) | `3` (needle first) |

    To keep NULL-skipping concatenation, say it in the expression:
    `concat(coalesce({A}, ''), coalesce({B}, ''))` means the same thing on
    every engine.

**Validation.** A catalog function called with the wrong number of arguments is
rejected at validation time with `WRONG_FUNCTION_ARITY`, naming the canonical
signature. Nothing else about the call is checked — argument *types* are not
modelled.

## The escape hatch

A function the catalog does not carry is still emitted verbatim, so
vendor-specific SQL keeps working:

```yaml
Zip 5:
  abstractType: string
  expression: "regexp_extract({Zip}, '[0-9]{5}')"
```

OBSL cannot know that function's arity or meaning, so it neither checks nor
rewrites it: the model now depends on the engines that have `regexp_extract`.
That is a legitimate choice, and the one thing that should not happen is making
it by accident — so the call is reported as a `NON_PORTABLE_FUNCTION` warning
naming the function and the expression it appears in.

A model that has to run on any dialect closes the hatch:

```yaml
settings:
  expressionMode: portable   # default: permissive
```

Under `portable` the same call is an **error** rather than a warning, so the
model cannot acquire an engine dependency without someone deciding to. The mode
changes nothing about the SQL: it decides whether a model loads, not how it
compiles.

Where a call has to be non-portable, keeping it in one computed column rather
than spread across measures is what keeps the port to another vendor to a short
list of edits.
