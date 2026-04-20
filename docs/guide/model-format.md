# OrionBelt ML (OBML) Model Format

OrionBelt ML (OBML) is the YAML-based format for defining semantic models in OrionBelt. A model describes your data warehouse tables (data objects), business dimensions, aggregate measures, and composite metrics.

## Top-Level Structure

```yaml
# yaml-language-server: $schema=schema/obml-schema.json
version: 1.0
owner: team-data           # Optional: model-level owner

settings:     # Optional: model-level compilation settings
  defaultNumericDataType: "decimal(18, 4)"
  defaultTimezone: "Europe/Zagreb"
  allowUtcFallback: true

dataObjects:  # Database tables/views with columns and joins
  ...

dimensions:   # Named dimensions referencing data object columns
  ...

measures:     # Aggregations with expressions
  ...

metrics:      # Composite metrics combining measures
  ...

filters:      # Optional: static WHERE conditions applied to every query
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
    code: ORDERS              # Table name or custom SQL
    database: WAREHOUSE         # Database/catalog
    schema: PUBLIC              # Schema
    columns:
      Order ID:
        code: ORDER_ID        # Physical column name
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

### Columns

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `code` | string | Yes | Physical column name in the database |
| `abstractType` | enum | Yes | `string`, `int`, `float`, `date`, `time`, `time_tz`, `timestamp`, `timestamp_tz`, `boolean`, `json` |
| `sqlType` | string | No | Informational: SQL data type (e.g. `VARCHAR`, `INTEGER`, `NUMERIC(10,2)`) |
| `sqlPrecision` | int | No | Informational: numeric precision |
| `sqlScale` | int | No | Informational: numeric scale |
| `numClass` | enum | No | Classification of numeric columns to control aggregation behavior. `categorical` (IDs/codes), `additive` (sum-safe), `non-additive` (rates/ratios) |
| `comment` | string | No | Documentation |
| `synonyms` | list | No | Alternative names or terms (LLM hints) |
| `owner` | string | No | Responsible team or person |

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

!!! note "Fact tables declare joins"
    By convention, fact tables (e.g., `Orders`) declare joins to dimension tables (e.g., `Customers`, `Products`). The compiler uses this to identify fact tables — data objects with joins are preferred as base objects during query resolution.

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
| `resultType` | enum | Yes | Data type of the result (informative only, not used for SQL generation) |
| `label` | string | No | Display label |
| `timeGrain` | enum | No | Time grain: `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute`, `second` |
| `format` | string | No | Display format |
| `synonyms` | list | No | Alternative names or terms (LLM hints) |
| `owner` | string | No | Responsible team or person |

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
| `aggregation` | enum | Yes | `sum`, `count`, `count_distinct`, `avg`, `min`, `max`, `listagg` |
| `expression` | string | No | Expression with `{[DataObject].[Column]}` placeholders |
| `distinct` | bool | No | Apply DISTINCT to aggregation |
| `total` | bool | No | Use the total (unfiltered) value when referenced in a metric |
| `delimiter` | string | No | Separator for `listagg` aggregation (default: `","`) |
| `withinGroup` | object | No | Ordering clause for `listagg` — specifies `column` and `order` (`ASC`/`DESC`) |
| `dataType` | string | No | OBML data type (e.g. `decimal(18, 4)`, `bigint`). Overrides automatic type inference for CAST wrapping. |
| `filters` | list | No | Filters applied to this measure (supports AND/OR/NOT groups) |
| `allowFanOut` | bool | No | Allow fan-out joins (default: false) |
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
-- Unfiltered:   SUM("extendedprice" * (1 - "discount"))
-- Filtered:     SUM(CASE WHEN "returnflag" = 'R'
--                        THEN "extendedprice" * (1 - "discount") END)
```

This works with all planners (star, CFL, cumulative, period-over-period) and all 8 dialects. Filtered measures can be combined with unfiltered measures in ratio metrics:

```yaml
metrics:
  Return Rate:
    expression: "{[Returned Revenue]} / {[Revenue]}"
```

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

## Metrics

Metrics come in three types: **derived** (composite expression), **cumulative** (window function over a measure), and **period-over-period** (time comparison).

### Derived Metrics

A **derived metric** combines multiple measures into a KPI. The expression references measures by name using `{[Measure Name]}` template syntax.

```yaml
metrics:
  Revenue per Order:
    expression: '{[Revenue]} / {[Order Count]}'

  Net Revenue:
    expression: '{[Sales Amount]} - {[Return Amount]}'
```

All artefacts (data objects, dimensions, measures, metrics) have unique names. The `{[Name]}` placeholders in a metric expression must match existing measure names exactly.

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

### Metric Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `type` | `"derived"` \| `"cumulative"` \| `"period_over_period"` | `"derived"` | Metric category |
| `expression` | string | — | Expression with `{[Measure Name]}` placeholders (required for derived and period_over_period) |
| `measure` | string | — | Name of base measure (required for cumulative) |
| `timeDimension` | string | — | Dimension used for ordering (required for cumulative) |
| `cumulativeType` | `"sum"` \| `"avg"` \| `"min"` \| `"max"` \| `"count"` | `"sum"` | Window aggregation function |
| `window` | integer | — | Rolling window size in periods (mutually exclusive with `grainToDate`) |
| `grainToDate` | `"year"` \| `"quarter"` \| `"month"` \| `"week"` | — | Reset boundary (mutually exclusive with `window`) |
| `periodOverPeriod` | object | — | Period-over-period configuration (required for period_over_period) |
| `dataType` | string | — | OBML data type (e.g. `decimal(18, 4)`). Overrides automatic type inference for CAST wrapping. |
| `label` | string | — | Display label |
| `description` | string | — | Business description |
| `format` | string | — | Display format |
| `synonyms` | list | — | Alternative names or terms (LLM hints) |
| `owner` | string | — | Responsible team or person |

### Metric Expression Placeholders

| Placeholder | Resolves to |
|-------------|-------------|
| `{[Measure Name]}` | Named reference to any defined measure (derived metrics only) |

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
2. **Structural inference** — COUNT/COUNT_DISTINCT → `bigint`; division in expression → `decimal(18, 6)`
3. **Model-level default** — `settings.defaultNumericDataType`
4. **Built-in default** — `decimal(18, 2)` for SUM/AVG aggregations

Pass-through (no CAST emitted): `min`, `max`, `any_value`, `median`, `mode`, `listagg`.

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
version: "1.0"
settings:
  defaultNumericDataType: "decimal(18, 4)"

dataObjects:
  # ...
measures:
  Revenue:
    aggregation: sum
    expression: "{[Orders].[Price]}"
    # Will use decimal(18, 4) instead of built-in decimal(18, 2)
```

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

## Timezone Settings

OrionBelt supports timezone-aware serialization of temporal query results. When executing queries, naive timestamps (without timezone info) from the database are coerced to the configured timezone and serialized in ISO 8601 format.

### Configuration

```yaml
version: "1.0"
settings:
  defaultTimezone: "Europe/Zagreb"
  allowUtcFallback: true
```

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `defaultTimezone` | string | — | IANA timezone (e.g. `Europe/Zagreb`, `America/New_York`, `UTC`) |
| `allowUtcFallback` | boolean | `false` | If true, falls back to UTC when no timezone can be resolved |

### Resolution Order

The effective timezone for naive timestamp coercion is resolved in this order (first match wins):

1. **Database session timezone** — auto-detected from the connection (one query, cached per dialect)
2. **Model setting** — `settings.defaultTimezone` (fallback when detection fails)
3. **Host process timezone** — the server's system timezone (if not UTC)
4. **UTC fallback** — only if `settings.allowUtcFallback` is `true`

If no timezone resolves, naive timestamps pass through without offset information.

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
version: "1.0"
settings:
  defaultNumericDataType: "decimal(18, 2)"
  defaultTimezone: "Europe/Zagreb"
  allowUtcFallback: true

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
