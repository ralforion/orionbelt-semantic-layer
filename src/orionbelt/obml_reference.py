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

sum, count, count_distinct, avg, min, max,
any_value, median, mode, listagg

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
