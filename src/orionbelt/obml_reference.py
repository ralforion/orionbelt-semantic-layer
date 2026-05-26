"""OBML reference text — served via the REST API reference endpoint."""

from __future__ import annotations

OBML_REFERENCE = """\
# OBML (OrionBelt ML) Reference

OBML is a YAML-based semantic model format. A model has four top-level sections:

## 1. dataObjects — physical tables/views

```yaml
dataObjects:
  Orders:                         # data object name
    code: ORDERS                  # physical table/view name
    database: EDW                 # database
    schema: SALES_MART            # schema
    columns:
      Order ID:                   # column name — must be unique within this data object
        code: ID                  # physical column name
        abstractType: string      # see abstractType values below
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive        # categorical | additive | non-additive
    joins:                        # optional — defined on fact tables
      - joinType: many-to-one     # many-to-one | one-to-one
        joinTo: Customers         # target data object name
        columnsFrom:
          - Customer ID           # local column name
        columnsTo:
          - Customer ID           # target column name
```

## 2. dimensions — named analytical dimensions

```yaml
dimensions:
  Customer Country:
    dataObject: Customers         # which data object owns this dimension
    column: Country               # column within that data object
    resultType: string            # data type of the result (informative only)
    timeGrain: month              # optional: year | quarter | month | week | day | hour
                                  # REQUIRES the underlying column's abstractType to be
                                  # date, timestamp, or timestamp_tz. Setting timeGrain on
                                  # a string/int column is rejected at validation time
                                  # (error code TIME_GRAIN_ON_NON_TEMPORAL). For text columns
                                  # encoding dates (e.g. '2024-03'), define a computed column
                                  # with to_date() and point the dimension at that.
    via: Orders                   # optional: force join path through this data object

  # Role-playing dimensions — same target, different join paths
  SalesEmployee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Sales                    # reach Employees through Sales

  ReturnEmployee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Returns                  # reach Employees through Returns
```

## 3. measures — aggregations

```yaml
measures:
  Total Revenue:                  # measure name
    columns:                      # column references (for simple aggregations)
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum              # see aggregation values below
    total: false                  # optional: grand total shorthand (= grain: { mode: FIXED })

  Profit:                         # expression-based measure
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Amount]} - {[Orders].[Cost]}'  # {[DataObject].[Column]} syntax

  Revenue by Region:              # grain override — aggregate at region level
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: FIXED                 # FIXED or RELATIVE (default)
      include: [Region]           # aggregate at region level only

  Unfiltered Revenue:             # filter context — ignore query filters
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    filterContext:
      mode: FIXED                 # FIXED: ignore all query filters
                                  # RELATIVE: inherit and modify

  Filtered Measure:               # measure with a filter
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
    filter:
      column:
        dataObject: Orders
        column: Status
      operator: equals            # equals | gt | gte | lt | lte | in | not_in | ...
      values:
        - dataType: string
          valueString: completed
```

## 4. metrics — composite calculations from measures

```yaml
metrics:
  Profit Margin:
    expression: '{[Profit]} / {[Total Revenue]}'  # {[Measure Name]} syntax
```

## abstractType Values

string, int, float, date, time, time_tz, timestamp,
timestamp_tz, boolean, json

## numClass Values (optional — classification of numeric columns to control aggregation behavior)

categorical, additive, non-additive

## Aggregation Values

Core: sum, count, count_distinct, avg, min, max,
any_value, median, mode, listagg

Statistical (v2.6+): stddev, stddev_pop, variance, var_pop,
corr, covar_pop, covar_samp, regr_slope, regr_intercept

Single-column statistical aggregates (stddev, stddev_pop, variance, var_pop)
require exactly one column. Two-column aggregates (corr, covar_pop,
covar_samp, regr_slope, regr_intercept) require exactly two columns:

```yaml
measures:
  Revenue Spend Correlation:
    aggregation: corr
    columns:
      - dataObject: Orders
        column: Revenue
      - dataObject: Marketing
        column: Spend
```

Dialect coverage:

| Aggregation | Postgres | Snowflake | BigQuery | Databricks | DuckDB | ClickHouse | MySQL | Dremio |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| stddev, stddev_pop, variance, var_pop | yes | yes | yes | yes | yes | yes | yes | yes |
| corr, covar_pop, covar_samp | yes | yes | yes | yes | yes | yes | no | yes |
| regr_slope, regr_intercept | yes | yes | no | yes | yes | no | no | yes |

Unsupported combinations raise `UNSUPPORTED_AGGREGATION_FOR_DIALECT` at
compile time — express via a derived metric (e.g.
`{[Covariance]} / {[Variance X]}`) if you need the gap covered.

## Metric Types

OBML supports four metric types — pick by setting `type:`:

- `derived` (default) — expression composing existing measures
- `cumulative` — windowed aggregation (running, rolling, grain-to-date)
- `period_over_period` — current vs. prior-period comparison
- `window` — single-row window functions (rank, lag, lead, ntile,
  first_value, last_value)

### Cumulative — partitioning by dimension (v2.6+)

The optional `partitionBy:` list adds `PARTITION BY` keys alongside the
implicit `ORDER BY <timeDimension>`. Useful for per-entity moving
averages, per-region running totals, etc.

```yaml
metrics:
  Revenue MA12 by Country:
    type: cumulative
    measure: Revenue
    timeDimension: order_month
    cumulativeType: avg
    window: 12
    partitionBy: [Country]
```

Every entry must be a dimension defined in the model and present in the
query's SELECT. Default `[]` preserves prior behavior (no partition).

### Window — rank, lag, lead, ntile, first/last value (v2.6+)

```yaml
metrics:
  Revenue Rank by Quarter:
    type: window
    windowFunction: dense_rank
    measure: Revenue
    orderDirection: desc
    partitionBy: [Quarter]

  Revenue Prior Month:
    type: window
    windowFunction: lag
    measure: Revenue
    offset: 1
    timeDimension: order_month
    partitionBy: [Country]

  Revenue Quartile:
    type: window
    windowFunction: ntile
    measure: Revenue
    buckets: 4
    partitionBy: [Year]
```

- `windowFunction:` one of `rank | dense_rank | row_number | ntile |
  lag | lead | first_value | last_value` (required)
- `measure:` base measure (required except for ROW_NUMBER / NTILE that
  rank over time alone)
- `partitionBy:` dimensions to partition by (optional, default `[]`)
- `orderDirection:` `asc` or `desc` (default `desc`)
- `offset:` integer >= 1 (LAG/LEAD only)
- `buckets:` integer >= 2 (NTILE only)
- `timeDimension:` required for LAG/LEAD

Window metrics compose freely with derived metrics — e.g.
`'{[Revenue]} - {[Revenue Prior Month]}'`.

## 5. synonyms — alternative names (optional, LLM hints)

All five element levels (dataObject, column, dimension, measure, metric) support
an optional `synonyms` list — alternative names or terms that help LLMs
map natural-language questions to the correct model element:

```yaml
dataObjects:
  Customers:
    code: CUSTOMERS
    database: EDW
    schema: SALES
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

## 6. customExtensions — vendor-keyed metadata (optional)

All six levels (model, dataObject, column, dimension, measure, metric) support
an optional `customExtensions` array for vendor-specific metadata:

```yaml
customExtensions:
  - vendor: OSI
    data: '{"instructions": "Use for retail analytics", "synonyms": ["sales"]}'
  - vendor: GOVERNANCE
    data: '{"owner": "data-team", "classification": "internal"}'
```

Each entry has `vendor` (identifier string) and `data` (opaque JSON string).
OrionBelt preserves these during parsing but does not interpret them.

## Key Rules

1. **Column names are unique within each data object**.
   Dimensions, measures, and metrics must be unique across the model.
2. Measure expressions use `{[DataObject].[Column]}` to reference columns.
3. Metric expressions use `{[Measure Name]}` to reference measures by name.
4. Joins are defined on fact tables pointing to dimension tables \
(many-to-one or one-to-one).
5. A dimension references exactly one `dataObject` + `column` pair.
6. A dimension may set `via` to force the join path through a specific \
intermediate data object (role-playing dimensions). The dimension's \
`dataObject` must be reachable from `via` in the directed join graph.
7. **Strict parsing (v2.7.2+)**: unknown keys on any OBML object are \
rejected with error code `UNKNOWN_PROPERTY`. A typo like `filtter:` or \
`columsFrom:` fails validation instead of being silently dropped — there \
is no flag to bypass this.

## Complete Minimal Example

```yaml
version: 1.0

dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    columns:
      Order ID:
        code: ID
        abstractType: string
      Customer ID:
        code: CUST_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Customer ID
        columnsTo:
          - Cust ID

  Customers:
    code: CUSTOMERS
    database: EDW
    schema: SALES
    columns:
      Cust ID:
        code: ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string

measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum

metrics:
  Revenue Per Order:
    expression: '{[Total Revenue]} / {[Order Count]}'
```

## Supported SQL Dialects

postgres, snowflake, clickhouse, databricks, dremio

## Workflow

1. `load_model(model_yaml)` — parse, validate, store → returns `model_id`
2. `describe_model(model_id)` — inspect data objects, dimensions, measures, metrics
3. `compile_query(model_id, dimensions=[...], measures=[...])` — generate SQL
"""
