---
description: "OBML reference for OrionBelt Semantic Layer: YAML format for defining data objects, dimensions, measures, metrics, joins, and filters that compile to analytical SQL."
---

# OrionBelt ML (OBML) Model Format

OrionBelt ML (OBML) is the YAML-based format for defining semantic models in OrionBelt. A model describes your data warehouse tables (data objects), business dimensions, aggregate measures, and composite metrics.

## Top-Level Structure

```yaml
# yaml-language-server: $schema=schema/obml-schema.json
version: 1.0
owner: team-data # Optional: model-level owner

exposeCounts: true # Optional: synthesize per-object row-count measures (default true)
countLabelPattern: "{object} Count" # Optional: count label template ({object} token only)

settings: # Optional: model-level compilation settings
  defaultNumericDataType: "decimal(18, 4)"
  defaultTimezone: "Europe/Zagreb"
  defaultLocale: "de-DE" # BCP-47; default locale for result value formatting

dataObjects: # Database tables/views with columns and joins
  ...

dimensions: # Named dimensions referencing data object columns
  ...

measures: # Aggregations with expressions
  ...

metrics: # Composite metrics combining measures
  ...

filters: # Optional: static WHERE conditions applied to every query
  ...
```

The four main sections (`dataObjects`, `dimensions`, `measures`, `metrics`) are dictionaries keyed by name. The optional `filters` section is a list.

### Owner Field

Every level of the model supports an optional `owner` field — a free-text string identifying the responsible team or person. The owner is returned in model discovery API responses.

```yaml
version: 1.0
owner: team-data

dataObjects:
  Orders:
    owner: team-sales
    columns:
      Price:
        owner: team-finance
dimensions:
  Country:
    owner: team-analytics
measures:
  Revenue:
    owner: team-analytics
metrics:
  Revenue per Order:
    owner: team-analytics
```

## Data Objects

A **data object** maps to a database table or custom SQL statement. Each data object declares its columns and optional join relationships.

```yaml
dataObjects:
  Orders:
    code: ORDERS # Table name or custom SQL
    database: WAREHOUSE # Database/catalog
    schema: PUBLIC # Schema
    columns:
      Order ID:
        code: ORDER_ID # Physical column name
        abstractType: string
      Order Date:
        code: ORDER_DATE
        abstractType: date
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Price:
        code: PRICE
        abstractType: float
        numClass: non-additive
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
        - Customer ID
        columnsTo:
        - Customer ID
```

### Data Object Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `code` | string | Yes | Table name or SQL statement |
| `database` | string | Yes | Database/catalog name |
| `schema` | string | Yes | Schema name |
| `columns` | map | Yes | Dictionary of column definitions |
| `joins` | list | No | Join relationships to other data objects |
| `comment` | string | No | Documentation |
| `synonyms` | list | No | Alternative names or terms (LLM hints) |
| `owner` | string | No | Responsible team or person |
| `countable` | bool | No | Synthesize a row-count measure for this object (default `true`); see [Row-Count Measures](#row-count-measures-auto-synthesized) |
| `countLabel` | string | No | Name/label for this object's synthesized count measure (overrides `countLabelPattern`) |
| `nestedIn` | object | No | Take this object's rows by unnesting an array column on another object instead of from a table — see [Nested data objects](#nested-data-objects-nestedin) |

### Nested data objects (`nestedIn`)

Some tables carry a column that is itself a little table: a repeated record, an
`ARRAY<STRUCT>`. Cloud billing exports do this for labels and credits, and so
does anything landed from a document store.

A repeated column *is* a table — one per parent row — so OBSL models it as a
data object whose rows come from unnesting it, rather than reaching into it with
an accessor:

```yaml
dataObjects:
  Charges:
    code: charges
    database: WAREHOUSE
    schema: FINOPS
    columns:
      Charge Id:
        code: id
        abstractType: string
        primaryKey: true
      Cost:
        code: cost
        abstractType: float

  Charge Labels:
    nestedIn:
      dataObject: Charges     # the object holding the array
      column: Labels          # the array column on it
    columns:
      Label Key:
        code: Key             # a field of the array's element
        abstractType: string
      Label Value:
        code: Value
        abstractType: string
```

`Charge Labels` has no `code`, `database` or `schema`, because it has no table of
its own. Everything above it is ordinary: dimensions, measures and queries treat
it like any other data object.

**What this buys you.** The label *keys* stay data. Asking "how much spend carries
each label key?" is a normal group-by, where a flattened model would need one
declared column per key and could never answer for a key nobody thought of:

```yaml
dimensions:
  Label Key:   {dataObject: Charge Labels, column: Label Key}
  Label Value: {dataObject: Charge Labels, column: Label Value}
```

#### Three rules worth knowing before you use it

**1. The parent needs a `primaryKey` if you group its measures by a nested
dimension.** One charge carrying two labels appears twice under the unnest, so
`SUM(Cost)` by label value would count it twice. OBSL deduplicates on the
parent's key instead — and refuses the query, naming this rule, if the parent
declares no key. Measures on the *nested* object need nothing: each element
appears exactly once, which is why two identical credit lines both still count.

**2. A charge with an empty array keeps its row.** The unnest is an outer one, so
a charge with no labels still contributes its cost, under a NULL label. That is
almost always what you want for a total — on a real billing export, 61% of rows
carry no labels, and dropping them loses most of the spend.

**3. It is reached through its parent, never selected from.** Nothing joins *to*
a nested object, it cannot be a query's base object, and it cannot appear in a
multi-fact union — its rows exist only inside its parent's. OBSL refuses each of
those with an error saying so, rather than emitting SQL that cannot run.

#### Dialect support

Seven of the eight dialects unnest in the `FROM` clause, each in its own way —
`UNNEST` on BigQuery and DuckDB, `LATERAL FLATTEN` on Snowflake, `LATERAL VIEW
explode` on Databricks, `ARRAY JOIN` on ClickHouse, `LATERAL UNNEST` on
PostgreSQL, `JSON_TABLE` on MySQL. You write the model once; OBSL renders the
right shape.

Dremio has no `FROM`-clause unnest. Declare `code` alongside `nestedIn` and it
reads that flattening view there instead, warning you which source it used:

```yaml
  Charge Labels:
    nestedIn: {dataObject: Charges, column: Labels}
    code: v_charge_labels     # fallback where the dialect cannot unnest
    database: WAREHOUSE
    schema: FINOPS
    joins:                    # a view is a separate table, so it needs a key
      - joinType: many-to-one
        joinTo: Charges
        columnsFrom: [Charge Id]
        columnsTo: [Charge Id]
    columns:
      Charge Id: {code: charge_key, abstractType: string}
      Label Value: {code: Value, abstractType: string}
```

A dotted `column` reaches an array nested inside a struct — `column:
Project.Ancestors` addresses the array without any further declaration.

### Columns

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `code` | string | Yes (or `expression`) | Physical column name in the database. Mutually exclusive with `expression` |
| `expression` | string | Yes (or `code`) | SQL-style expression that references sibling columns via single-brace `{Column}` placeholders. Defines a **computed column** — see below. Mutually exclusive with `code` |
| `abstractType` | enum | Yes | `string`, `int`, `float`, `date`, `time`, `time_tz`, `timestamp`, `timestamp_tz`, `boolean`, `json` |
| `sqlType` | string | No | Informational: SQL data type (e.g. `VARCHAR`, `INTEGER`, `NUMERIC(10,2)`) |
| `sqlPrecision` | int | No | Informational: numeric precision |
| `sqlScale` | int | No | Informational: numeric scale |
| `numClass` | enum | No | Classification of numeric columns to control aggregation behavior. `categorical` (IDs/codes), `additive` (sum-safe), `non-additive` (rates/ratios) |
| `primaryKey` | bool | No | Marks the column as part of the data object's primary key. Informational only — set on multiple columns for composite keys. Rendered as `PK` in the ER diagram and emitted as `obsl:primaryKey` in the OBSL graph. |
| `comment` | string | No | Documentation |
| `synonyms` | list | No | Alternative names or terms (LLM hints) |
| `owner` | string | No | Responsible team or person |

#### Computed Columns

A column with `expression` instead of `code` defines a **computed column**: a column-level SQL expression that references *sibling columns of the same data object* via single-brace `{Column}` placeholders. The expression is inlined wherever the column is referenced — there's no materialization, no extra join.

```yaml
dataObjects:
  Date:
    code: date_dim
    columns:
      Year:
        code: d_year
        abstractType: int
      Month of Year:
        code: d_moy
        abstractType: int
      Year-Month:
        # Combines year and month-of-year into one int like 200011 — useful
        # as a sortable, single-column time bucket.
        expression: "({Year} * 100 + {Month of Year})"
        abstractType: int
```

The `Year-Month` column behaves like any other column afterwards: surface it through a dimension, group by it, sort by it.

```yaml
dimensions:
  Year-Month:
    dataObject: Date
    column: Year-Month
    resultType: int
```

Generated SQL substitutes the expression in place of the column reference:

```sql
SELECT (("Date"."d_year" * 100) + "Date"."d_moy") AS "Year-Month",
 SUM("Store Sales"."ss_ext_sales_price") AS "Store Sales Amount"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."date_dim" AS "Date" ON ...
GROUP BY (("Date"."d_year" * 100) + "Date"."d_moy")
ORDER BY (("Date"."d_year" * 100) + "Date"."d_moy") ASC
```

**Reference syntax recap:**

| Syntax | Where used | What it references |
|---|---|---|
| `{Column}` (single brace) | column-level `expression`, `Date.columns.Year-Month` | a sibling column in the **same** data object |
| `{[DataObject].[Column]}` (double brace + brackets) | column-level and measure-level `expression` | any column anywhere in the model |
| `{[Measure Name]}` (double brace + brackets) | metric-level `expression` | a measure by name |

**Constraints:**

- `expression` and `code` are mutually exclusive on a single column.
- The expression is parsed and rendered through the dialect's `compile_expr` like any other AST node. Arithmetic, comparisons, `CASE` in **both** SQL forms (`CASE WHEN <condition> THEN ...` and `CASE <subject> WHEN <value> THEN ...`, the second parsed into the first), `IN`, `BETWEEN` and `LIKE` are part of OBML's own grammar and therefore portable; function calls are portable when the function is in the [portable function catalog](functions.md), and passed through to the database verbatim when it is not.
- The two `CASE` forms mean the same thing, because the standard defines the simple one as the searched one: `CASE x WHEN a THEN r` *is* `CASE WHEN x = a THEN r`, and that carries the NULL rule with it — `WHEN NULL` matches nothing in either form, since `x = NULL` is unknown rather than false. Write the simple form when a single subject is being mapped to labels; it is the one that does not repeat the subject on every branch. A *condition* in the value position (`CASE x WHEN y > 5 THEN ...`) is refused rather than compared against the subject: it is legal SQL meaning `x = (y > 5)`, which is a type error on most engines and a silent coercion on MySQL.
- Every `{Column}` placeholder must name a sibling column of the same data object. An unresolvable placeholder is rejected at validation time with `UNKNOWN_COLUMN_IN_EXPRESSION` — it is not silently dropped, so a typo cannot reach the database as a string literal.
- A column of a *different* data object is referenced with the qualified `{[DataObject].[Column]}` form. This is what makes a column-to-column comparison across objects expressible — the thing query filters cannot do, since they compare a column to a literal:

    ```yaml
    Store:
      columns:
        Store Zip: { code: s_zip, abstractType: string }
        Zip Matches Customer:
          expression: "SUBSTRING({Store Zip}, 1, 5) = {[Customer Address].[Zip 5]}"
          abstractType: boolean
    ```

    The result is an ordinary column: use it as a dimension, filter it (`{field: Store.Zip Matches Customer, op: "=", value: false}`), or read it from a measure. The compiler joins the referenced data object into the query the same way it joins one a dimension names, so an unknown object or column is rejected with `UNKNOWN_DATA_OBJECT_IN_EXPRESSION` / `UNKNOWN_COLUMN_IN_EXPRESSION`, and one the query's base object cannot reach is rejected with `UNREACHABLE_REQUIRED_OBJECT`. Reachability follows the usual rule — joins are traversed forward, so the referenced object must sit on the *one* side of the path, which is also what keeps the reference from multiplying rows.

- A computed column may reference another computed column, on its own data object or another; the referenced expression is inlined recursively (`{doubled} * 2` where `doubled` is `{amount} * 2` compiles to `amount * 2 * 2`). A reference cycle is rejected with `CYCLIC_COMPUTED_COLUMN`, across data objects as well as within one.
- Braces inside a single-quoted string literal are data, never placeholders. A regex quantifier such as `regexp_extract({Zip}, '[0-9]{5}')` keeps its `{5}`, and `'{Zip}'` stays the literal five characters rather than becoming a column reference. Validation and compilation apply the same rule.
- Both halves of a column reference are required wherever one appears (`dimensions`, a measure's `columns`, `withinGroup`, measure filters). Omitting `dataObject` or `column` is rejected with `INCOMPLETE_COLUMN_REF`, because an omitted half would otherwise reach SQL as an empty identifier.
- A computed column must carry its own guards. Filters do not protect it: engines are free to evaluate the projection before, or regardless of, a predicate that would have made it safe. `({Dependent Count} * 1.000) / {Vehicle Count}` alongside a `Vehicle Count > 0` filter is tolerated by DuckDB and raises `Division by zero` on ClickHouse. Write the guard into the expression — `CASE WHEN {Vehicle Count} > 0 THEN ... ELSE NULL END`, or `NULLIF({Vehicle Count}, 0)` as the divisor — and it holds everywhere.
- ORDER BY on a computed column works correctly — the planner emits the inlined expression, not the alias, in `ORDER BY` (the recent compiler fix in the [Compilation guide](compilation.md)).

### Portable functions in expressions

A function call inside an `expression` — a computed column, a measure
expression, a metric formula — is either **in the catalog**, in which case OBSL
owns what it means and renders it per dialect, or **outside it**, in which case
the call is emitted verbatim and the model is pinned to whatever engines happen
to spell it that way.

The 41 entries, what each is pinned to mean, JSON access, and the
`expressionMode` setting are in **[Portable Functions](functions.md)**.

### Joins

Joins define relationships between data objects. The data object that declares the join is the "from" side.

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `joinType` | enum | Yes | `many-to-one`, `one-to-one`, `many-to-many` |
| `joinTo` | string | Yes | Target data object name |
| `columnsFrom` | list | Yes | Column names in this data object (join keys) |
| `columnsTo` | list | Yes | Column names in the target data object (join keys) |
| `secondary` | bool | No | Mark as a secondary (alternative) join path (default: `false`) |
| `pathName` | string | No | Unique name for this join path (required when `secondary: true`) |
| `required` | bool | No | Whether a row without a match survives the join (default: `false` → `LEFT JOIN`; `true` → `INNER JOIN`) |

!!! note "Fact tables declare joins"
 By convention, fact tables (e.g., `Orders`) declare joins to dimension tables (e.g., `Customers`, `Products`). The compiler uses this to identify fact tables — data objects with joins are preferred as base objects during query resolution.

### Required Joins

Joins compile to `LEFT JOIN`, so a fact row whose foreign key matches nothing
still appears, with NULLs on the other side. Where the key is mandatory in the
data and an unmatched row is meaningless, say so:

```yaml
joins:
  - joinType: many-to-one
    joinTo: Store
    columnsFrom: [Store Key]
    columnsTo: [Store Key]
    required: true          # INNER JOIN — unmatched rows drop
```

This is not the same statement as `joinType`, which is a *cardinality* — how
many rows meet how many — and says nothing about whether the match is optional.

It is worth stating in the model rather than filtering per query, because the
filter that stands in for it is not portable. `WHERE right.key IS NOT NULL`
works on most engines and **silently keeps every row on ClickHouse**, where an
unmatched right-side column comes back as the type's default (`0`, `''`) rather
than NULL. Which side of the join to test is not something a model author
should have to know.

### Secondary Joins

When a data object has multiple relationships to the same target (e.g., a `Flights` table joining to `Airports` via both departure and arrival), mark the additional joins as `secondary` with a unique `pathName`:

```yaml
dataObjects:
  Flights:
    code: FLIGHTS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Departure Airport:
        code: DEP_AIRPORT
        abstractType: string
      Arrival Airport:
        code: ARR_AIRPORT
        abstractType: string
    joins:
      - joinType: many-to-one
        joinTo: Airports
        columnsFrom:
        - Departure Airport
        columnsTo:
        - Airport ID
      - joinType: many-to-one
        joinTo: Airports
        secondary: true
        pathName: arrival
        columnsFrom:
        - Arrival Airport
        columnsTo:
        - Airport ID
```

Rules:

- Every secondary join **must** have a `pathName`
- `pathName` must be unique per `(source, target)` pair (not globally)
- Secondary joins are excluded from cycle detection and multipath validation
- Queries use `usePathNames` to select a secondary join instead of the default primary — see [Query Language](query-language.md#secondary-join-paths)

## Column References

Columns are referenced using the `dataObject` + `column` pair throughout the model:

```yaml
dimensions:
  Product Name:
    dataObject: Products
    column: Product Name
    resultType: string
```

Column names must be unique within each data object. Dimensions, measures, and metrics must have unique names across the whole model.

## Dimensions

A **dimension** defines a business attribute used for grouping (GROUP BY) in queries.

```yaml
dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string

  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month
```

### Dimension Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `dataObject` | string | Yes | Source data object name |
| `column` | string | Yes | Column name in the data object |
| `resultType` | enum | Yes | Data type of the result. A temporal `resultType` over a `timeGrain` is emitted as a CAST, so the dimension keeps its declared type rather than whatever the engine's truncation returns. It must be wide enough to hold the bucket: an hour, minute or second grain needs `timestamp`, and `time` never holds a grain because it drops the date. A narrower declaration is rejected at load with `RESULT_TYPE_LOSES_GRAIN`, because the cast is applied in the `GROUP BY` too and would merge buckets and change the measures |
| `timeGrain` | enum | No | Time grain: `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute`, `second`. The underlying column's `abstractType` must be `date`, `timestamp`, or `timestamp_tz` — validation rejects `timeGrain` on string/numeric columns (error code `TIME_GRAIN_ON_NON_TEMPORAL`). For text columns that encode dates (e.g. `'2024-03'`), define a computed column with `to_date()` first and point the dimension at that. |
| `via` | string | No | Force join path through this intermediate data object (role-playing dimensions) |
| `format` | string | No | Display format pattern (e.g. `#,##0.00`, `0.00%`) |
| `synonyms` | list | No | Alternative names or terms (LLM hints) |
| `owner` | string | No | Responsible team or person |

### Role-Playing Dimensions (via)

When multiple fact tables join to the same dimension table, use `via` to scope a dimension to a specific join path. This is called a **role-playing dimension** — the same physical table serves different business roles depending on which fact table provides the context.

```yaml
dimensions:
  # Without via: the compiler picks the shortest path (may be ambiguous)
  EmployeeName:
    dataObject: Employees
    column: employeename
    resultType: string

  # With via: scoped to Sales context — joins Sales → Employees
  SalesEmployee:
    dataObject: Employees
    column: employeename
    resultType: string
    via: Sales

  # With via: scoped to Returns context — joins Returns → Employees
  ReturnEmployee:
    dataObject: Employees
    column: employeename
    resultType: string
    via: Returns
```

The `via` data object must be reachable from the query's base object, and the dimension's `dataObject` must be reachable from `via` in the directed join graph. The compiler validates this at model load time.

The `via` object can be any ancestor on the path — it doesn't have to be the immediate parent. For example, `via: Sales` on a dimension targeting `Regions` would force the path `Sales → Clients → Countries → Regions`.

The validator will emit `MISSING_VIA` warnings when a dimension's target is reachable from multiple fact tables without `via` set.

### Time Dimensions

Set `timeGrain` to apply time grain truncation:

```yaml
dimensions:
  Order Month:
    dataObject: Orders
    column: Order Date
    resultType: date
    timeGrain: month
```

This generates `date_trunc('month', col)` in Postgres/Snowflake or `toStartOfMonth(col)` in ClickHouse.

You can also apply time grain at query time using the `"dimension:grain"` syntax — see [Query Language](query-language.md).

## Measures

A **measure** defines an aggregate computation over data object columns.

### Simple Measure (single column)

```yaml
measures:
  Order Count:
    columns:
      - dataObject: Orders
        column: Order ID
    resultType: int
    aggregation: count
```

### Row-Count Measures (auto-synthesized)

Row counting is first-class in OBSL without exposing a `dataObject` as a queryable
artifact and without ad-hoc `COUNT(*)` in queries. Every countable data object
yields a **grain-anchored** count measure whose **name and label are the same
human string** (default `"Sales Count"`) — exactly like a declared measure such as
`"Order Count"`. It appears in the model's measure list, on the discovery API, in
the BI catalog, and in composability results.

```yaml
select:
dimensions: [Customer Country]
measures:
  - Sales Count # synthesized COUNT(*) anchored on Sales, integer-typed
```

Because the count is anchored on its object and rides the normal planner, a
many-to-one join does not inflate it, and fan-trap prevention still applies.

Knobs:

| Knob | Level | Default | Effect |
|------|-------|---------|--------|
| `countable` | dataObject | `true` | Set `false` to opt a data object out of count synthesis |
| `countLabel` | dataObject | — | Name/label for this object's count (overrides the pattern) |
| `exposeCounts` | model | `true` | Set `false` to suppress all synthesized counts |
| `countLabelPattern` | model | `"{object} Count"` | Name/label template; the only valid token is `{object}` (interpolates the object's display label) |

The count measure's **name is its label** (name precedence: `countLabel` >
`countLabelPattern` > `"{object} Count"`), so you reference it exactly as it reads
(`measures: [Sales Count]`). Declaring a measure with that same name overrides
synthesis — the escape hatch for self-fanning models (e.g. `aggregation:
count_distinct` on a primary key). Synthesized counts are derived on read and
never roundtrip through YAML/OSI; the knobs above do.

### Expression Measure (computed from columns)

Reference columns directly in the expression using `{[DataObject].[Column]}`:

```yaml
measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Price]} * {[Orders].[Quantity]}'
```

```yaml
measures:
  Profit:
    resultType: float
    aggregation: sum
    expression: '{[Sales].[Salesamount]} - {[Sales].[Salescosts]}'
    total: true
```

### Measure Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `columns` | list | No | List of column references (`dataObject`+`column`) for simple single-column measures |
| `resultType` | enum | Yes | Data type of the result (informative only, not used for SQL generation) |
| `aggregation` | enum | Yes | `sum`, `count`, `count_distinct`, `avg`, `min`, `max`, `any_value`, `median`, `mode`, `listagg`; statistical: `stddev`, `stddev_pop`, `variance`, `var_pop`, `corr`, `covar_pop`, `covar_samp`, `regr_slope`, `regr_intercept` — see [Aggregation Types](#aggregation-types) for dialect coverage |
| `expression` | string | No | Expression with `{[DataObject].[Column]}` placeholders |
| `distinct` | bool | No | Apply DISTINCT to aggregation |
| `total` | bool | No | Grand total shorthand (equivalent to `grain: { mode: FIXED }`) |
| `anchor` | string | No | [Data object whose grain a cross-fact expression is evaluated at](compilation.md#cross-fact-measure-expressions). Only meaningful when the expression reads facts no join path reaches together |
| `grain` | object | No | [Grain override](grain-filter-context.md#grain-override) -- controls aggregation grain independently from query dimensions |
| `filterContext` | object | No | [Filter context override](grain-filter-context.md#filter-context) -- controls which query WHERE filters apply |
| `delimiter` | string | No | Separator for `listagg` aggregation (default: `","`) |
| `withinGroup` | object | No | Ordering clause for `listagg` — specifies `column` and `order` (`ASC`/`DESC`). The `column` must resolve to a real data object column (`UNKNOWN_DATA_OBJECT` / `UNKNOWN_COLUMN`). With `distinct: true` it must additionally be the one being aggregated (error code `WITHIN_GROUP_NOT_IN_DISTINCT_ARGS`). |
| `dataType` | string | No | OBML data type (e.g. `decimal(18, 4)`, `bigint`). Overrides automatic type inference for CAST wrapping. |
| `format` | string | No | Display format pattern (e.g. `#,##0.00`, `0.00%`) |
| `description` | string | No | Business description |
| `filters` | list | No | Filters applied to this measure (supports AND/OR/NOT groups) |
| `allowFanOut` | bool | No | Allow fan-out joins (default: false) |
| `defaultValue` | str/number/bool | No | Value to report when the aggregate has nothing to add up (emitted as `COALESCE` around the aggregate). Unset keeps the SQL-standard NULL. |
| `synonyms` | list | No | Alternative names or terms (LLM hints) |
| `owner` | string | No | Responsible team or person |

### Aggregation Types

| Type | SQL | Example |
|------|-----|---------|
| `sum` | `SUM(expr)` | Total revenue |
| `count` | `COUNT(expr)` | Number of orders |
| `count_distinct` | `COUNT(DISTINCT expr)` | Unique customers |
| `avg` | `AVG(expr)` | Average price |
| `min` | `MIN(expr)` | Earliest date |
| `max` | `MAX(expr)` | Latest date |
| `any_value` | `ANY_VALUE(expr)` | Any single value from the group (`any()` in ClickHouse) |
| `median` | `MEDIAN(expr)` | Median value (`PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY ...)` in Postgres) |
| `mode` | `MODE(expr)` | Most frequent value (`MODE() WITHIN GROUP (ORDER BY ...)` in Postgres, `topK(1)(col)[1]` in ClickHouse; not supported in Dremio) |
| `listagg` | `LISTAGG(expr, sep)` | Concatenated values (dialect-specific: `STRING_AGG` in Postgres, `ARRAY_JOIN(COLLECT_LIST(...))` in Databricks, `arrayStringConcat(groupArray(...))` in ClickHouse) |

#### Statistical aggregates

Single-column aggregates take exactly one entry in `columns`; two-column aggregates take exactly two (arity is enforced at model-load time, error code `INVALID_AGGREGATION_INPUTS`). Dialect coverage varies — MySQL has no correlation / covariance / regression; BigQuery and ClickHouse lack the linear-regression family. Unsupported combinations raise `UNSUPPORTED_AGGREGATION_FOR_DIALECT` at compile time. See [Trend Analysis](trend-analysis.md#statistical-aggregates-on-measure) for the full coverage matrix and a worked example.

| Type | SQL | Arity |
|------|-----|:---:|
| `stddev`, `stddev_samp` | `STDDEV_SAMP(x)` | 1 |
| `stddev_pop` | `STDDEV_POP(x)` | 1 |
| `variance`, `var_samp` | `VAR_SAMP(x)` | 1 |
| `var_pop` | `VAR_POP(x)` | 1 |
| `corr` | `CORR(x, y)` | 2 |
| `covar_pop` | `COVAR_POP(x, y)` | 2 |
| `covar_samp` | `COVAR_SAMP(x, y)` | 2 |
| `regr_slope` | `REGR_SLOPE(y, x)` | 2 |
| `regr_intercept` | `REGR_INTERCEPT(y, x)` | 2 |

### Expression Placeholders

| Placeholder | Resolves to |
|-------------|-------------|
| `{[DataObject].[Column]}` | Column reference by data object and column name |

### Measure Filters

Apply filters to a measure so it only aggregates matching rows. The `filters` property accepts a list of leaf filters and filter groups.

#### Single filter

```yaml
measures:
  Sales Profit Ratio:
    resultType: float
    aggregation: sum
    expression: '({[Sales].[Salesamount]} / {[Sales].[Salescosts]}) * 100'
    filters:
      - column:
          dataObject: Sales
          column: Salescosts
        operator: gt
        values:
      - dataType: float
        valueFloat: 100.00
```

#### Multiple filters with AND/OR logic

Use filter groups for boolean combinations:

```yaml
measures:
  Domestic Revenue:
    columns:
      - dataObject: Line Items
        column: Extended Price
    resultType: float
    aggregation: sum
    filters:
      - logic: or
        filters:
        - column:
            dataObject: Nations
            column: Name
          operator: equals
          values:
        - dataType: string
          valueString: UNITED STATES
        - column:
            dataObject: Nations
            column: Name
          operator: equals
          values:
        - dataType: string
          valueString: CANADA
```

#### Filter Group Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `logic` | enum | `and` | `and` or `or` — how to combine child filters |
| `filters` | list | — | Child filters (leaf filters or nested filter groups) |
| `negated` | bool | `false` | Wrap the combined expression with `NOT` |

Multiple top-level filters are combined with **AND**. Filter groups and leaf filters can be mixed freely and nested recursively.

#### How Measure Filters Compile

Filtered measures compile to `CASE WHEN` inside the aggregate function. The implicit `ELSE NULL` is ignored by all aggregate functions (SUM, COUNT, AVG, etc.):

```sql
-- Unfiltered: SUM("extendedprice" * (1 - "discount"))
-- Filtered: SUM(CASE WHEN "returnflag" = 'R'
-- THEN "extendedprice" * (1 - "discount") END)
```

This works with all planners (star, CFL, cumulative, period-over-period) and all 8 dialects. Filtered measures can be combined with unfiltered measures in ratio metrics:

```yaml
metrics:
  Return Rate:
    expression: "{[Returned Revenue]} / {[Revenue]}"
```

### Empty-Set Values

An aggregate over no rows is NULL in standard SQL, and a filtered measure
reaches that state routinely — the group exists, the filter matches none of it.
Whether that should read as NULL or as zero is a modelling decision, and
engines do not agree on the default: ClickHouse answers `0` for an aggregate
over an empty row set where Postgres, DuckDB and the rest answer NULL.

`defaultValue` settles it in the model, on every dialect:

```yaml
measures:
  Returned Revenue:
    columns: [{dataObject: Returns, column: Amount}]
    aggregation: sum
    defaultValue: 0          # COALESCE(SUM(...), 0) — omit to keep NULL
    filters:
      - column: {dataObject: Returns, column: Status}
        operator: equals
        values: [{dataType: string, valueString: refunded}]
```

It wraps the aggregate rather than its input: `COALESCE(SUM(x), 0)` answers 0
when the aggregate saw nothing, where `SUM(COALESCE(x, 0))` would answer 0 for
a row whose value is missing — a different claim.

Reach for it especially on a measure used as a divisor. A NULL denominator
yields NULL, but a `0` denominator is a division-by-zero error on some engines,
so a metric over filtered measures is one empty group away from failing on one
engine and not another.

### LISTAGG Measures

Use `listagg` to concatenate column values into a delimited string. OrionBelt renders the correct SQL for each database dialect automatically.

```yaml
measures:
  Product Names:
    columns:
      - dataObject: Products
        column: Product Name
    resultType: string
    aggregation: listagg
    delimiter: ', '
    withinGroup:
      column:
        dataObject: Products
        column: Product Name
      order: ASC
```

The `delimiter` defaults to `","` if omitted. The `withinGroup` clause is optional and specifies ordering of the concatenated values.

With `distinct: true`, `withinGroup.column` must be the column the measure aggregates. SQL restricts a DISTINCT
aggregate's `ORDER BY` to expressions in its argument list — the engine sorts values it has already deduplicated,
so it cannot order them by something it collapsed away. Postgres, DuckDB and BigQuery all reject it (*"In a
DISTINCT aggregate, ORDER BY expressions must appear in the argument list"*). Model validation catches this up
front (`WITHIN_GROUP_NOT_IN_DISTINCT_ARGS`) rather than letting every query on the measure fail at execution
time. Order by the aggregated column, or drop `distinct: true` if the ordering matters more.

## Metrics

Metrics come in four types: **derived** (composite expression), **cumulative** (window function over a measure), **period-over-period** (time comparison), and **window** (rank / lag / lead / ntile / first/last value — single-row window functions).

### Derived Metrics

A **derived metric** combines multiple measures into a KPI. The expression references measures by name using `{[Measure Name]}` template syntax.

```yaml
metrics:
  Revenue per Order:
    expression: '{[Revenue]} / {[Order Count]}'

  Net Revenue:
    expression: '{[Sales Amount]} - {[Return Amount]}'
```

All artefacts (data objects, dimensions, measures, metrics) have unique names. A `{[Name]}` placeholder must match one exactly — a measure, another derived metric, or a window metric. See [Metric Expression Placeholders](#metric-expression-placeholders) for what may be referenced where.

### Cumulative Metrics

A **cumulative metric** applies a window function to an existing measure, ordered by a time dimension. Three patterns are supported:

| Pattern | Configuration | SQL Frame |
|---------|--------------|-----------|
| Running total | (default — no `window` or `grainToDate`) | `ROWS UNBOUNDED PRECEDING` |
| Rolling window | `window: N` | `ROWS BETWEEN N-1 PRECEDING AND CURRENT ROW` |
| Grain-to-date | `grainToDate: month` | `PARTITION BY DATE_TRUNC('month', ...)` + unbounded |

```yaml
metrics:
  # Running total (unbounded cumulative sum)
  Cumulative Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    description: Running total of revenue

  # Rolling 7-period average
  7-Day Rolling Avg Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    cumulativeType: avg
    window: 7

  # Month-to-Date (resets each month)
  MTD Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    grainToDate: month

  # Year-to-Date (resets each year)
  YTD Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    grainToDate: year

  # Rolling peak
  30-Day Peak Revenue:
    type: cumulative
    measure: Revenue
    timeDimension: Order Date
    cumulativeType: max
    window: 30
```

!!! note "Time dimension requirement"
 The `timeDimension` must be included in the query's selected dimensions. Cumulative metrics without their time dimension in the SELECT will raise a validation error.

!!! tip "Partition by dimension"
 Add `partitionBy: [Country, ...]` to compute per-entity rolling windows (e.g. 12-month MA per country). Every entry must be a model dimension present in the query's SELECT. See [Trend Analysis](trend-analysis.md#1-partitioned-rolling-windows) for worked examples.

### Period-over-Period Metrics

A **period-over-period metric** compares a measure against a prior time period. The `expression` references the base measure, and the `periodOverPeriod` block configures how to shift time and compute the comparison.

```yaml
metrics:
  Revenue YoY Growth:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: year
      comparison: percentChange

  Revenue MoM Diff:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
```

Four comparison modes are available:

| Comparison | Formula | Use case |
|------------|---------|----------|
| `percentChange` | `current / NULLIF(prev, 0) - 1` | YoY growth rate |
| `ratio` | `current / NULLIF(prev, 0)` | Current-to-previous ratio |
| `difference` | `current - prev` | Absolute change |
| `previousValue` | `prev` | Prior period value alongside current |

!!! note "Time dimension requirement"
 The `timeDimension` must be included in the query's selected dimensions. All PoP metrics in a single query must share the same `timeDimension` and `grain`.

For a detailed guide on PoP metrics, including CTE architecture, filter push-down, and dialect-specific SQL examples, see the [Period-over-Period Metrics](period-over-period.md) guide.

### Window Metrics

A **window metric** wraps a single-row SQL window function — `RANK`, `DENSE_RANK`, `ROW_NUMBER`, `NTILE`, `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`. Use `partitionBy:` to scope to subgroups and `orderDirection:` to flip ranking direction.

```yaml
metrics:
  # Rank revenue within each quarter
  Revenue Rank by Quarter:
    type: window
    windowFunction: dense_rank
    measure: Revenue
    orderDirection: desc
    partitionBy: [Quarter]

  # Prior-month revenue side-by-side with the current row
  Revenue Prior Month:
    type: window
    windowFunction: lag
    measure: Revenue
    offset: 1
    timeDimension: Order Date
    partitionBy: [Country]

  # Quartile bucket
  Revenue Quartile:
    type: window
    windowFunction: ntile
    measure: Revenue
    buckets: 4
    partitionBy: [Year]
```

Window metrics compose freely with derived metrics — `expression: '{[Revenue]} - {[Revenue Prior Month]}'` yields a MoM delta without writing any SQL. See [Trend Analysis](trend-analysis.md#2-window-metrics-rank-lag-lead-ntile-firstlast-value) for the full feature surface, validation rules, and dialect coverage.

### Metric Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `type` | `"derived"` \| `"cumulative"` \| `"period_over_period"` \| `"window"` | `"derived"` | Metric category |
| `expression` | string | — | Expression with `{[Measure Name]}` placeholders (required for derived and period_over_period) |
| `measure` | string | — | Name of base measure (required for cumulative and window) |
| `timeDimension` | string | — | Dimension used for ordering (required for cumulative and for lag/lead window metrics) |
| `cumulativeType` | `"sum"` \| `"avg"` \| `"min"` \| `"max"` \| `"count"` | `"sum"` | Window aggregation function |
| `window` | integer | — | Rolling window size in periods (mutually exclusive with `grainToDate`) |
| `grainToDate` | `"year"` \| `"quarter"` \| `"month"` \| `"week"` | — | Reset boundary (mutually exclusive with `window`) |
| `partitionBy` | list | `[]` | Dimensions used as `PARTITION BY` keys for cumulative or window metrics. Each entry must be a model dimension in the query's SELECT. |
| `periodOverPeriod` | object | — | Period-over-period configuration (required for period_over_period) |
| `windowFunction` | `"rank"` \| `"dense_rank"` \| `"row_number"` \| `"ntile"` \| `"lag"` \| `"lead"` \| `"first_value"` \| `"last_value"` | — | Window function family (required for window metrics) |
| `offset` | integer | — | Row offset for `lag` / `lead` (>= 1) |
| `buckets` | integer | — | Bucket count for `ntile` (>= 2) |
| `orderDirection` | `"asc"` \| `"desc"` | `"desc"` | Window `ORDER BY` direction |
| `defaultValue` | scalar | — | Default value for `lag` / `lead` when the offset row is absent |
| `dataType` | string | — | OBML data type (e.g. `decimal(18, 4)`). Overrides automatic type inference for CAST wrapping. |
| `description` | string | — | Business description |
| `format` | string | — | Display format pattern (e.g. `#,##0.00`, `0.00%`) |
| `synonyms` | list | — | Alternative names or terms (LLM hints) |
| `owner` | string | — | Responsible team or person |

### Metric Expression Placeholders

| Placeholder | Resolves to |
|-------------|-------------|
| `{[Measure Name]}` | Named reference to any defined measure, or to another derived or window metric (derived metrics only) |

A metric expression may reference another **derived** metric, at any depth, so a
KPI can be named once and reused:

```yaml
metrics:
  Margin:
    expression: '{[Revenue]} - {[Total Cost]}'
  Margin Pct:
    expression: '{[Margin]} / {[Revenue]}'
```

The planner expands the inner metric in place, down to real aggregates, so
`Margin Pct` compiles exactly as if its formula had been written out in full.

!!! warning "Cumulative and period-over-period metrics do not nest"
    Referencing a **cumulative** or **period-over-period** metric from another
    metric is rejected at model load with `UNSUPPORTED_METRIC_REF`. Those are
    computed by their own wrapper, which only runs when the metric carrying the
    feature is selected directly, so wrapping one in a derived metric would
    skip the wrapper and leave its placeholder in the SQL as a bare column name
    no engine can bind. Reference the base measure instead.

    A **window** metric is the exception (`'{[Revenue]} - {[Revenue Prior
    Month]}'`): the window wrapper follows derived references and projects the
    window metric's base measure as a column of its base CTE.

## Data Types & Numerical Precision

OrionBelt automatically wraps aggregate expressions with `CAST` to ensure consistent numerical precision across dialects. Each measure and metric resolves to an **OBML data type** that maps to the appropriate SQL type per dialect.

### OBML Data Types

| Type | Example | Description |
|------|---------|-------------|
| `decimal(p, s)` | `decimal(18, 2)` | Fixed-point numeric with precision and scale |
| `bigint` | — | 64-bit integer |
| `integer` | — | 32-bit integer |
| `double` | — | 64-bit floating point |
| `date` | — | Calendar date |
| `timestamp` | — | Date and time with timezone |
| `time` | — | Time of day |
| `string` | — | Text |
| `boolean` | — | True/false |

### Type Resolution Order

The effective data type for a measure or metric is resolved in this order (first match wins):

1. **Explicit declaration** — `dataType` on the measure or metric
2. **Structural inference** — COUNT/COUNT_DISTINCT → `bigint`; division in expression → `decimal(18, 6)`; a measure with `resultType: int` aggregated with SUM → `bigint`
3. **Model-level default** — `settings.defaultNumericDataType`
4. **Built-in default** — `decimal(18, 2)` for SUM/AVG aggregations

Pass-through (no CAST emitted): `min`, `max`, `any_value`, `median`, `mode`, `listagg`.

!!! note "A column declared wider than the default"

    The built-in default carries 16 integer digits. If a column declares both
    `sqlPrecision` and `sqlScale`, and that width holds more integer digits
    than the default, the inferred type widens to fit - the precision moves,
    the scale stays, since the scale is the rounding the model asked for.

    ```yaml
    Wide: {code: amt, abstractType: float, sqlPrecision: 38, sqlScale: 15}
    # a SUM over this emits DECIMAL(25, 2), not DECIMAL(18, 2)
    ```

    Both halves are required. `sqlPrecision` alone says nothing about scale,
    and assuming zero there rounds every row before the aggregate sees it.

    An **undeclared** column cannot be widened, because nothing in the model
    says how large its values are. A total that outgrows `decimal(18, 2)` then
    fails on PostgreSQL, DuckDB and ClickHouse. It does not fail on MySQL,
    whose casts carry 38 digits precisely because it would otherwise saturate
    rather than refuse (see [Numeric Overflow](#numeric-overflow)) - so a model
    that only ever runs there will not notice. Declare the column width, or pin
    the measure's `dataType`, if your totals can reach 16 digits.

!!! note "Large integer measures"

    The built-in default holds only 16 integer digits, but a 64-bit column
    needs 19. Left to default, `SUM` over a `BIGINT` produced a value the
    engine computed correctly and then failed to cast, so the query errored on
    a perfectly legal figure. A measure declaring `resultType: int` therefore
    infers `bigint` for `SUM`.

    **On two engines the sum itself is rewritten**, because their accumulator
    is 64-bit and *wraps* rather than overflowing. Measured on two rows of
    `9000000000000000000`, ClickHouse and Dremio both returned
    `-446744073709551616`, a negative total from two positive rows, where
    DuckDB, PostgreSQL, BigQuery and Databricks raise and Snowflake answers
    exactly. No output type repairs that: `sumWithOverflow` gives the same
    value on ClickHouse, and so does casting the result to `DECIMAL(38, 0)`,
    because the accumulator has already wrapped. So the **argument** is widened
    instead, and the result carries `decimal(38, 0)`:

    | dialect | `SUM` over a 64-bit measure |
    | --- | --- |
    | ClickHouse | `SUM(toDecimal128(x, 0))` |
    | Dremio | `SUM(CAST(x AS DECIMAL(38, 0)))` |
    | everyone else | plain `SUM`, cast to `bigint` |

    Only `resultType: int` is rewritten, and only a plain `SUM`. A windowed sum
    inside a cumulative or period-over-period metric keeps the engine's own
    behaviour; the measure it aggregates is already rewritten inside the CTE,
    which is where the accumulation over rows happens.

    **`AVG` over an integer measure is rewritten** on the engines that need
    it, rather than widened. See the note below for which, and why the
    distinction matters.

!!! note "`AVG` above ~15 significant digits"

    `AVG` is a floating-point aggregate on several engines **whatever the
    input type**, so it drifts once the average passes a `double` mantissa.
    This is not limited to integer columns: a wide `DECIMAL` measure drifts
    too. On DuckDB, averaging `9223372036854775807.12` and `...807.24` returns
    `9.223372036854776e+18` rather than `9223372036854775807.18`, while `SUM`
    over the same column stays exact - see
    [duckdb/duckdb#6829](https://github.com/duckdb/duckdb/issues/6829), closed
    as not planned.

    Because the loss is inside the aggregate, no output type repairs it. A
    measure declaring `resultType: int` therefore has its **expression**
    rewritten into an exact form, by whichever route the engine offers:

    | dialect | `AVG` over a 64-bit value | what OBSL emits |
    | --- | --- | --- |
    | PostgreSQL, MySQL, Snowflake | already exact | plain `AVG`, widened result type |
    | BigQuery | FLOAT64, drifts | `AVG(CAST(x AS NUMERIC))`, BIGNUMERIC above scale 9 |
    | Dremio, Databricks | drift | `CAST(SUM(CAST(x AS DECIMAL(38, 0))) AS DECIMAL(38, s)) / COUNT(x)` |
    | ClickHouse | Float64, drifts | `divideDecimal(SUM(toDecimal128(x, 0)), toDecimal128(COUNT(x), 0), s)`, guarded against a zero count |
    | DuckDB | DOUBLE, drifts | `(2 * (SUM(x) * 10^s) + SIGN(SUM(x)) * COUNT(x)) // (2 * COUNT(x)) * 10^-s`, guarded against a zero count - no exact *division* exists there, so the average is assembled from integer arithmetic |

    **The inner cast is the load-bearing part** of the Dremio and Databricks
    form, and of the ClickHouse one. `SUM` over a 64-bit column accumulates in
    64 bits, so widening afterwards only widens a number that has already
    overflowed: two rows of 9000000000000000000 summed to
    -446744073709551616 on Dremio and ClickHouse, silently, and raise
    `ARITHMETIC_OVERFLOW` on Databricks. Casting the argument first is what
    makes the running total exact. A plain `SUM` measure meets the same
    accumulator and is rewritten the same way - see the note above.

    The divisor is also NULLIF-guarded, as every division is - see
    [Division by Zero](#division-by-zero) above.

    **The result type is widened wherever the average is exact**, whether it
    got there natively or by rewrite, since an exact average the declared type
    cannot hold is no better than an inexact one. `decimal(18, 2)` carries 16
    integer digits and a 64-bit value needs 19. Measured, the default made
    PostgreSQL raise and MySQL return `9999999999999999.99` for a true
    `1000000000000000003` - a saturated cast, which its widened cast now holds
    (see [Numeric Overflow](#numeric-overflow)), but the widened *result type*
    is what keeps the exact average on the other engines. An explicit
    `dataType` is respected as declared.


    **DuckDB is the awkward one**, and is exact now too. Every division there
    returns `DOUBLE` — decimal over decimal, `SUM`/`COUNT` with either operand
    cast, `AVG` over a cast input — so there is nothing exact to divide *with*.
    Its integer arithmetic is exact, though, so the average is assembled rather
    than divided: scale the sum by `10^s`, take the rounded quotient with `//`,
    and put the scale back by *multiplying* by a decimal constant, since
    dividing by `10^s` would go back to floating point.

    ```sql
    CASE WHEN COUNT(qty) = 0 THEN NULL ELSE
      (2 * (SUM(qty) * 100) + SIGN(SUM(qty)) * COUNT(qty)) // (2 * COUNT(qty))
        * CAST(0.01 AS DECIMAL(3, 2))
    END
    ```

    Ties go **away from zero**, which is what `round` pins and what the engines
    that were already exact answer: measured against PostgreSQL at `2.365` and
    `-2.365`, all three say 2.37 and -2.37. The remaining limit is loud rather
    than quiet: `2 * SUM * 10^s` has to fit 128 bits, so a total beyond about
    8.5x10^35 at scale 2 raises rather than drifting.

    Only integer-sourced measures are rewritten. A `float` measure keeps the
    plain `AVG` everywhere: BigQuery's `NUMERIC` is (38, 9), so casting a float
    column with more decimals would trade one silent error for another.

### Division by Zero

**A zero divisor yields NULL, on every dialect.** Every division OBSL compiles
gets its divisor wrapped in `NULLIF(..., 0)` - in a metric expression, in a
measure expression, and in the divisions OBSL generates itself (such as an
`AVG` with `total: true`).

This exists because the engines disagree completely. Measured on
`SUM(amt) / SUM(qty)` with a zero divisor:

| dialect | without the guard |
| --- | --- |
| DuckDB | `inf` |
| MySQL | `NULL` |
| PostgreSQL | raises `division by zero` |
| BigQuery | raises `400 division by zero` |
| ClickHouse | raises code 153 |

The same model and the same query would otherwise return a number on one
warehouse, NULL on another, and fail outright on three. NULL was chosen because
it reads naturally as "no value" in a BI tool, it matches what MySQL already
did, and it removes DuckDB's `inf` - the only outcome that can flow on into
downstream aggregation and formatting rather than stopping.

A literal divisor that is plainly not zero is left unwrapped:

```yaml
Halved:
  expression: "{[S].[Amt]} / 2"     # emits  amt / 2, no guard
Rate:
  expression: "{[S].[Amt]} / {[S].[Qty]}"   # emits  amt / NULLIF(qty, 0)
```

!!! note "Named division functions follow the same rule"

    The two catalog functions that divide internally are covered too, each by
    its own entry rather than by this one:

    - `div(a, b)` yields NULL when `b` is zero.
    - `log(base, x)` yields NULL outside its domain: a base of 0 or 1, or a
      value of 0 or less. A base of 1 is the subtle case, since `LOG10(1)` is
      zero and two dialects rewrite the call as `log10(x) / log10(base)`.

    They needed pinning for the same reason the operator did. Measured, `div(7,
    0)` returned NULL on two engines and raised on four, and `log` outside its
    domain had four different answers - including `inf`, `-0.0`, `-inf` and
    `nan` on ClickHouse, which is the silent case NULL exists to remove.

### Numeric Overflow

**A value that outgrows its type is an error on every engine but MySQL, which
returns a wrong number instead.** Measured on the same measure over the same
data, a true total of `100000000000000000` under `dataType: "decimal(18, 2)"`:

| dialect | result |
| --- | --- |
| DuckDB | raises `Conversion Error` |
| PostgreSQL | raises `numeric field overflow` |
| ClickHouse | raises code 407 |
| Snowflake | raises |
| Databricks | raises |
| BigQuery | `100000000000000000` - its `NUMERIC` is (38, 9), so the value fits |
| MySQL | `9999999999999999.99` - saturated |

MySQL attaches warning 1264 to that row, but a warning is not an error and no
driver on this stack surfaces one, so what reaches a dashboard is a plausible
wrong number. No session setting changes it: `STRICT_ALL_TABLES`,
`STRICT_TRANS_TABLES` and `TRADITIONAL` all saturate exactly as the default
does, because those modes govern writes rather than a `SELECT`-time `CAST`.

So on MySQL a measure's decimal cast is widened to at least 38 digits, and the
overflow stops being reachable rather than being caught:

```sql
CAST(SUM(`s`.`amt`) AS DECIMAL(38, 2))   -- MySQL; DECIMAL(18, 2) elsewhere
```

Only the **precision** moves. The scale is what shapes the value and is carried
through exactly as declared, so `decimal(18, 2)` still rounds to two places and
only stops refusing totals the source holds legally. A model that declares more
than 38 keeps what it declared.

38 and no further, though MySQL itself allows 65: it is what every other
supported engine accepts, so a value MySQL now returns is one a portable model
could have carried anyway. Widening to 65 would let this one engine answer
where the other seven cannot, which reverses the divergence rather than
removing it.

!!! warning "One case is still saturated: a 64-bit integer cast"

    A measure that resolves to `bigint` casts to `SIGNED`, which is MySQL's
    only 64-bit integer cast target - its `CAST` vocabulary has no wider one.
    A `SUM` over a bigint column past `9223372036854775807` therefore still
    returns `9223372036854775807` on MySQL where the other engines raise.

    Widening it would mean casting every count to `DECIMAL`, changing the type
    family of the most common measure in a model to reach a value no real count
    has. If your integer totals can pass 9.2 quintillion, declare a
    `dataType: "decimal(38, 0)"` on the measure, which is not affected.

### Explicit Data Type

```yaml
measures:
  Revenue:
    resultType: float
    aggregation: sum
    expression: "{[Orders].[Price]}"
    dataType: "decimal(38, 8)"
```

### Model-Level Default

Override the built-in default for all numeric measures/metrics in the model:

```yaml
version: 1.0
settings:
  defaultNumericDataType: "decimal(18, 4)"

dataObjects:
# ...
measures:
  Revenue:
    aggregation: sum
    expression: "{[Orders].[Price]}"

    # Will use decimal(18, 4) instead of built-in decimal(18, 2)```

### Dialect-Specific Type Mapping

| OBML Type | Postgres | Snowflake | ClickHouse | BigQuery | MySQL | Databricks |
|-----------|----------|-----------|------------|----------|-------|------------|
| `decimal(18, 2)` | `NUMERIC(18, 2)` | `NUMBER(18, 2)` | `Decimal(18, 2)` | `NUMERIC(18, 2)` | `DECIMAL(18, 2)` | `DECIMAL(18, 2)` |
| `bigint` | `BIGINT` | `NUMBER(38, 0)` | `Int64` | `INT64` | `BIGINT` | `BIGINT` |
| `double` | `DOUBLE PRECISION` | `FLOAT` | `Float64` | `FLOAT64` | `DOUBLE` | `DOUBLE` |

Each dialect enforces its own maximum decimal precision (Postgres: 131072; Snowflake/DuckDB/Databricks/Dremio: 38; ClickHouse: 76; MySQL: 65). Values exceeding the limit are automatically clamped.

### Generated SQL Example

```yaml
measures:
  Revenue:
    aggregation: sum
    expression: "{[Orders].[Price]}"
    dataType: "decimal(18, 2)"
```

Compiles to:

=== "Postgres"

 ```sql
 SELECT CAST(SUM("Orders"."PRICE") AS NUMERIC(18, 2)) AS "Revenue"
 ```

=== "Snowflake"

 ```sql
 SELECT CAST(SUM("Orders"."PRICE") AS NUMBER(18, 2)) AS "Revenue"
 ```

=== "ClickHouse"

 ```sql
 SELECT CAST(SUM("Orders"."PRICE") AS Decimal(18, 2)) AS "Revenue"
 ```

## Display Formatting

Dimensions, measures, and metrics support a `format` property that defines how values are displayed in the UI and returned in the execute response metadata.

### Format Patterns

| Pattern | Description | Example Output |
|---------|-------------|----------------|
| `#,##0.00` | Thousands separator, 2 decimals | `1,399.86` |
| `#,##0` | Thousands separator, no decimals | `1,400` |
| `0.00%` | Percentage with 2 decimals | `12.34%` |
| `0.00` | No thousands separator, 2 decimals | `1399.86` |

### Example

```yaml
measures:
  Revenue:
    aggregation: sum
    expression: "{[Orders].[Price]}"
    dataType: "decimal(18, 2)"
    format: "#,##0.00"

metrics:
  Return Rate:
    expression: "{[Total Returns]} / {[Total Sales]}"
    dataType: "decimal(5, 4)"
    format: "0.00%"
```

### Locale-Aware Rendering

The Gradio UI detects the browser's locale via the `Accept-Language` header and applies locale-specific separators automatically. For example, the pattern `#,##0.00` renders as:

| Locale | Output |
|--------|--------|
| `en-US` | `1,399.86` |
| `de-DE` | `1.399,86` |
| `fr-FR` | `1.399,86` |

### Execute Response

Format patterns are returned in the column metadata of the execute response:

```json
{
 "columns": [
 {"name": "Revenue", "type": "decimal(18, 2)", "format": "#,##0.00"},
 {"name": "Return Rate", "type": "decimal(5, 4)", "format": "0.00%"}
 ]
}
```

The `type` field uses the model's `dataType` when set, falls back to `settings.defaultNumericDataType`, then to a simple hint (`number`, `string`, `datetime`).

## Timezone Settings

OrionBelt supports timezone-aware serialization of temporal query results. When executing queries, naive timestamps (without timezone info) from the database are coerced to the configured timezone and serialized in ISO 8601 format.

### Configuration

```yaml
version: 1.0
settings:
  defaultTimezone: "Europe/Zagreb"
```

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `defaultTimezone` | string | — | IANA timezone (e.g. `Europe/Zagreb`, `America/New_York`, `UTC`) |
| `overrideDatabaseTimezone` | boolean | `false` | If true, use `defaultTimezone` instead of the auto-detected database session timezone |
| `defaultDialect` | string | — | One of the 8 registered dialects (`bigquery`, `clickhouse`, `databricks`, `dremio`, `duckdb`, `mysql`, `postgres`, `snowflake`). Used by `/v1/query/{sql,execute}` when the request omits `dialect`. Resolution order at request time: explicit `dialect` → `settings.defaultDialect` → `DB_VENDOR` env → `postgres`. |
| `defaultLocale` | string | — | BCP-47 locale tag (e.g. `en-US`, `de-DE`). Default locale for result value formatting (thousand/decimal separators) on `/v1/query/execute?format_values=true`. Resolution order at request time: explicit `?locale=` → `settings.defaultLocale` → `DEFAULT_LOCALE` env. |
| `expressionMode` | `permissive` \| `portable` | `permissive` | How function calls in expressions are held to the catalog. See [The escape hatch](functions.md#the-escape-hatch). |
| `queryTimezone` | string | — | IANA zone (e.g. `Europe/Zagreb`) that timestamp columns are read in, so which day or week a row falls in is the model's decision rather than the warehouse session's. See below. |
| `weekStart` | `monday` \| `sunday` | `monday` | Which day a week begins on, for `date_trunc('week', …)` and the boundaries `date_diff('week', …)` counts. ISO 8601 by default; `sunday` for a US retail calendar. Week *numbering* from `extract('week', …)` stays ISO either way — see below. |

### Resolution Order

The effective timezone for naive timestamp coercion is resolved in this order (first match wins):

1. **Database session timezone** — auto-detected from the connection (one query, cached per dialect)
2. **Model setting** — `settings.defaultTimezone` (fallback when detection fails)
3. **Host process timezone** — the server's system timezone (if not UTC)
4. **UTC** — automatic final fallback

When `overrideDatabaseTimezone: true` is set and `defaultTimezone` is configured, the model timezone takes priority over the detected database session timezone. Use this when naive timestamps are stored in a known timezone that differs from the DB session (e.g. users storing local timestamps in a UTC-configured database).

**Database session timezone detection** queries the connected database once per dialect:

| Dialect | Detection Query |
|---------|----------------|
| Snowflake | `SELECT CURRENT_TIMEZONE()` |
| Postgres | `SELECT current_setting('TIMEZONE')` |
| MySQL | `SELECT @@session.time_zone` |
| DuckDB | `SELECT current_setting('TimeZone')` |
| ClickHouse | `SELECT timezone()` |
| BigQuery | Fixed: UTC |
| Databricks | Not detected (uses model fallback) |
| Dremio | Not detected (uses model fallback) |

This ensures naive timestamps from the database are labeled with the timezone they actually represent (the database session's timezone), not a potentially different model-level setting.

### Serialization Rules

| Input | Output |
|-------|--------|
| Naive datetime + resolved TZ | ISO 8601 with offset: `2026-04-19T14:30:00+02:00` |
| UTC datetime | ISO 8601 with Z: `2026-04-19T14:30:00Z` |
| TZ-aware datetime | Preserved as-is: `2026-04-19T14:30:00+02:00` |
| Date | ISO 8601: `2026-04-19` |
| Time (no microseconds) | `14:30:00` |
| Time (with microseconds) | `14:30:00.123456` |

Zero microseconds are elided for cleaner output. UTC offsets (`+00:00`) use the compact `Z` suffix.

### Example

```yaml
version: 1.0
settings:
  defaultNumericDataType: "decimal(18, 2)"
  defaultTimezone: "Europe/Zagreb"

dataObjects:
  Orders:
    code: ORDERS
    columns:
      Order Date: { code: ORDER_DATE, abstractType: timestamp }
      Price: { code: PRICE, abstractType: float }

dimensions:
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date

measures:
  Revenue:
    aggregation: sum
    expression: "{[Orders].[Price]}"
    dataType: "decimal(18, 2)"
```

When executing this model, timestamps in the `Order Date` column will be serialized with the `Europe/Zagreb` offset (e.g. `+01:00` in winter, `+02:00` in summer).

## Synonyms

All five element levels (data object, column, dimension, measure, metric) support an optional `synonyms` list. Synonyms provide alternative names or terms that help LLMs map natural-language questions to the correct model element.

```yaml
dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    synonyms: [client, buyer, purchaser]
    columns:
      Country:
        code: COUNTRY
        abstractType: string
        synonyms: [nation, region]

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    synonyms: [client country, buyer country]

measures:
  Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    synonyms: [sales, income, turnover]
```

Synonyms are surfaced in the `describe_model` response (REST API and MCP) so LLMs can match user intent to the correct dimension, measure, or data object even when the user uses different terminology.

## Custom Extensions

All six levels (model, data object, column, dimension, measure, metric) support an optional `customExtensions` array for vendor-specific metadata. OrionBelt preserves these during parsing and compilation but does not interpret them.

```yaml
customExtensions:
  - vendor: OSI
    data: '{"instructions": "Use for retail analytics", "synonyms": ["sales"]}'
  - vendor: GOVERNANCE
    data: '{"owner": "data-team", "classification": "internal"}'
```

### Custom Extension Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `vendor` | string | Yes | Vendor or format identifier (e.g. `OSI`, `GOVERNANCE`) |
| `data` | string | Yes | Opaque data payload (typically a JSON string) |

Use cases:

- **OSI interoperability**: Preserving `ai_context` (instructions, synonyms, examples) from OSI models during conversion
- **Governance tags**: Owner, classification, cost center, lineage information
- **Vendor-specific metadata**: Any key-value data that OrionBelt should pass through without interpretation

## Static Filters

A model can declare **static filters** — mandatory WHERE conditions applied to every query against the model. Use them to restrict data by business unit, region, status, time range, or any column-level condition.

```yaml
filters:
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed
  - dataObject: Orders
    column: Order Date
    operator: ">="
    value: 2026-01-01
  - dataObject: Customers
    column: Region
    operator: in
    values:
    - EMEA
    - APAC
```

Multiple static filters are combined with **AND**. They are always injected before any query-time filters and cannot be overridden at query time.

### Filter Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `dataObject` | string | Yes | Data object containing the column |
| `column` | string | Yes | Column name in the data object |
| `operator` | string | Yes | Comparison operator (see table below) |
| `value` | scalar | No | Single comparison value |
| `values` | list | No | List of values (for `in` / `not_in` / `between`) |

### Supported Operators

| Operator | SQL | Example |
|----------|-----|---------|
| `equals` | `= 'val'` | Exact match |
| `!=` | `<> 'val'` | Not equal |
| `>` | `> val` | Greater than |
| `>=` | `>= val` | Greater than or equal |
| `<` | `< val` | Less than |
| `<=` | `<= val` | Less than or equal |
| `in` | `IN ('a', 'b')` | Match any value in list (use `values`) |
| `not_in` | `NOT IN ('a', 'b')` | Exclude values in list (use `values`) |
| `is_not_null` | `IS NOT NULL` | Column is not null (no `value` needed) |
| `is_null` | `IS NULL` | Column is null (no `value` needed) |
| `between` | `BETWEEN a AND b` | Range (use `values` with two elements) |
| `contains` | `LIKE '%val%'` | Substring match |
| `starts_with` | `LIKE 'val%'` | Prefix match |
| `ends_with` | `LIKE '%val'` | Suffix match |

### Date and Timestamp Values

Date and timestamp values follow ISO 8601 format. They can be written as bare YAML dates/timestamps or quoted strings — both produce valid SQL:

```yaml
filters:
  # Bare date — YAML parses as date, coerced to ISO string
  - dataObject: Orders
    column: Order Date
    operator: ">="
    value: 2026-01-01

  # ISO timestamp with timezone
  - dataObject: Orders
    column: Created At
    operator: ">="
    value: 2026-01-01T00:00:00Z

  # ISO timestamp with offset
  - dataObject: Orders
    column: Created At
    operator: "<"
    value: 2026-07-01T00:00:00+02:00

  # Quoted string — works identically
  - dataObject: Orders
    column: Order Date
    operator: "<"
    value: "2027-01-01"

  # Date range
  - dataObject: Orders
    column: Order Date
    operator: between
    values:
    - "2026-01-01"
    - "2026-12-31"
```

All ISO 8601 variants are supported:

| Format | Example | Notes |
|--------|---------|-------|
| Date | `2026-01-01` | Bare or quoted |
| Timestamp | `2026-01-01T14:30:00` | ISO with `T` separator |
| Timestamp (space) | `2026-01-01 14:30:00` | YAML-style space separator |
| UTC | `2026-01-01T00:00:00Z` | Zulu/UTC timezone |
| Offset | `2026-01-01T14:30:00+02:00` | Explicit timezone offset |

### Auto-Join Extension

If a static filter references a data object that is not already in the query's join path, the compiler automatically extends the join graph to include it. For example, a filter on `Customers.Region` will add the `Customers` join even if the query only selects measures from `Orders`.

### Interaction with Query-Time Filters

Static filters are injected **before** query-time `where` filters. Both sets are combined with AND in the final SQL. Static filters cannot be removed or overridden by the query.

```yaml
# Model-level: always applied
filters:
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed
```

```json
// Query-time: added on top
{
 "select": { "dimensions": ["Customer Country"], "measures": ["Total Revenue"] },
 "where": [{ "field": "Customer Country", "op": "equals", "value": "Germany" }]
}
```

Produces: `WHERE "STATUS" = 'completed' AND "COUNTRY" = 'Germany'`

## Refresh contracts

The optional `refresh:` block on a `dataObject` declares the freshness contract of the physical table that the dataObject maps to. It drives the result-cache TTL: a query's effective TTL is the minimum across the refresh contracts of every physical table it touches. See the dedicated guide page for full details: [Freshness contracts](freshness-contracts.md).

```yaml
dataObjects:
  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
    refresh:
      mode: interval # or: heartbeat | static
      interval: 1h # required for interval mode
    columns:
      ...
```

Two `dataObject` entries that map to the same physical table should declare equivalent contracts. When they disagree, OBSL emits a `SHARED_TABLE_CONTRACT_DISAGREEMENT` warning at load time and applies the strictest contract.

## Examples

The optional top-level `examples:` block lists canonical queries authored alongside the model — the kinds of questions the model is designed to answer. Surfaced through `GET /v1/sessions/{sid}/models/{mid}/examples` so agents can ground on the model in one round trip without guessing from dimension and measure names alone.

```yaml
examples:
  - name: revenue_by_country
    description: "Total completed-order revenue, broken down by customer country, last 90 days."
    intent_tags: [revenue, geography, "trailing window"]
    query:
      select:
        dimensions: ["Customer Country"]
        measures: ["Total Revenue"]
      where:
        - field: "Order Date"
          op: ">="
          value: "2026-01-01"
      orderBy:
        - { field: "Total Revenue", direction: "desc" }
      limit: 100

  - name: refund_rate_by_product
    description: "Returns as percentage of sales, by product."
    intent_tags: [returns, rate, product]
    query:
      select:
        dimensions: ["Product Name"]
        measures: ["Refund Rate"]
```

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Snake_case identifier, unique within the examples block. |
| `description` | string | yes | One- or two-sentence explanation of what this example shows. |
| `intent_tags` | list of strings | no | Free-form tags. The `?intent=` query parameter on the examples list endpoint matches against these (exact → contains → fuzzy fallback). camelCase alias `intentTags` is accepted. |
| `query` | object | yes | Full QueryObject payload — same shape accepted by `/query/sql`. |

The single-example endpoint (`GET .../examples/{name}`) returns the full query plus a best-effort `compiled_sql_preview` so agents can inspect what the example would produce without executing it.

## Validation Rules

OrionBelt validates models against these rules:

1. **Unique identifiers** — Column names unique within each data object; dimension, measure, and metric names unique across the model
2. **No cyclic joins** — Join graph must be acyclic (secondary joins are excluded)
3. **No multipath joins** — No ambiguous diamond patterns (secondary joins are excluded). A **canonical join exception** applies: when a data object has a direct join to a target AND also an indirect path through intermediaries, the direct join is treated as canonical and no error is raised. Only true diamonds (two indirect paths to the same target) are flagged.
4. **Secondary join constraints** — Every secondary join must have a `pathName`; `pathName` must be unique per `(source, target)` pair
5. **Measures resolve** — All column references in measures must point to existing data object columns
6. **Join targets exist** — All `joinTo` targets must be defined data objects
7. **References resolve** — All dimension references (dataObject/column) must resolve
8. **Static filters resolve** — Filter `dataObject` and `column` must reference existing data objects and columns

Validation errors include source positions (line/column) when available.

## Full Example

See the [Sales Model Walkthrough](../examples/sales-model.md) for a complete annotated example.
