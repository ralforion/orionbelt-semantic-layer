---
description: "OrionBelt's structured QueryObject: select dimensions and measures by business name, apply WHERE and HAVING filters, sort, limit, and override join paths."
---

# Query Language

OrionBelt uses a structured query object to express analytical queries against a semantic model. The query language selects dimensions and measures, applies filters, sorts results, and limits output — all using business names rather than raw SQL.

## Query Object Structure

```yaml
select:
  dimensions:
    - Customer Country
    - "Order Date:month"      # with time grain
  measures:
    - Revenue
    - Order Count
where:
  - field: Customer Segment
    op: in
    value: [SMB, MidMarket]
having:
  - field: Revenue
    op: gt
    value: 10000
orderBy:
  - field: Revenue
    direction: desc
    nulls: last        # optional — "first" | "last"; omit for dialect default
limit: 1000
offset: 0              # optional, paired with limit for pagination
```

### In Python

```python
from orionbelt.models.query import (
    QueryObject,
    QuerySelect,
    QueryFilter,
    QueryOrderBy,
    FilterOperator,
    SortDirection,
)

query = QueryObject(
    select=QuerySelect(
        dimensions=["Customer Country", "Order Date:month"],
        measures=["Revenue", "Order Count"],
    ),
    where=[
        QueryFilter(field="Customer Segment", op=FilterOperator.IN, value=["SMB", "MidMarket"]),
    ],
    having=[
        QueryFilter(field="Revenue", op=FilterOperator.GT, value=10000),
    ],
    order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
    limit=1000,
)
```

## Select

The `select` section specifies which dimensions and measures to include.

### Dimensions

Dimensions are referenced by name as defined in the semantic model. They become `GROUP BY` columns in the generated SQL.

```yaml
select:
  dimensions:
    - Customer Country
    - Product Category
```

### Time Grain Override

Apply a time grain at query time using `"dimension:grain"` syntax:

```yaml
select:
  dimensions:
    - "Order Date:month"     # truncate to month
    - "Order Date:quarter"   # truncate to quarter
    - "Order Date:year"      # truncate to year
```

Supported grains: `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute`, `second`.

This overrides any `timeGrain` set on the dimension definition.

### Coalesce (Merging Role-Playing Dimensions)

Role-playing dimensions (e.g. `Sales Employee` and `Purchase Employee`, both pointing to `Employees.Employee Name` via different facts) appear as separate columns in CFL output — one row per role per person. To collapse them into a single output column, use a coalesce group inside `dimensions`:

```yaml
select:
  dimensions:
    - coalesce: [Employee Name, Purchase Employee]
      as: Employee
  measures:
    - Total Sales
    - Total Purchase Qty
```

Each leg still projects only its own role-playing dimension (others NULL); the outer wrapper emits `COALESCE("Employee Name", "Purchase Employee") AS "Employee"` and groups by it. A person who is both a sales rep and a purchase employee shows up as one row with totals on both sides.

**Rules:**

- At least 2 members; all must be existing model dimensions
- All members must share the same `resultType`
- The `as` alias must not collide with any model dimension or measure name
- `orderBy` may reference the alias directly (`field: Employee`) — ordering happens in the outer wrapper where the alias is in scope
- `where` filters belong on the underlying dimension names (filtering is applied per leg, before the COALESCE collapses the values)

**Error codes:** `COALESCE_MISSING_ALIAS`, `DUPLICATE_COALESCE_ALIAS`, `COALESCE_ALIAS_COLLISION`, `COALESCE_TOO_FEW_MEMBERS`, `COALESCE_TYPE_MISMATCH`.

### Measures

Measures are referenced by name. They can be simple aggregations, expression-based measures, or metrics.

```yaml
select:
  measures:
    - Revenue
    - Order Count
    - Revenue per Order    # metric
```

## Raw Mode (`select.fields`)

**Raw mode** returns un-aggregated rows from one or more data objects, projecting physical columns directly. It is the right choice when you need detail rows rather than aggregated metrics — e.g., exporting a transaction list, building a row-level export, or feeding a downstream tool that does its own aggregation.

```yaml
select:
  fields:
    - Customers.Country
    - Orders.Order ID
    - Orders.Amount
  distinct: false
where:
  - field: Orders.Amount
    op: gt
    value: 100
orderBy:
  - field: Orders.Amount
    direction: desc
limit: 100
```

Compiles to a flat `SELECT field1, field2, ... FROM base [LEFT JOIN ...] [WHERE ...] [ORDER BY ...] [LIMIT n]` — no `GROUP BY`, no aggregates.

### Field references

Each entry in `fields` is a qualified `DataObject.Column` reference to a physical column in the model. Logical dimension/measure names are **not** accepted in raw mode — use `dimensions` / `measures` for those.

The output column is aliased to the original `"DataObject.Column"` reference so result rows are self-describing.

### `distinct`

Set `select.distinct: true` to emit `SELECT DISTINCT`, deduplicating rows after projection. Outside raw mode this flag is rejected.

### What's excluded in raw mode

Raw mode is mutually exclusive with aggregate features. The following are rejected at validation time:

- `select.dimensions`
- `select.measures`
- `having` (HAVING references measures, which raw mode doesn't have)
- `dimensionsExclude`

### Filters and ordering in raw mode

- **WHERE**: same operators as aggregate mode. Filter fields can reference a dimension name (resolved to its physical column) or a qualified `DataObject.Column`.
- **ORDER BY**: must reference a `DataObject.Column` alias from `select.fields`, or a 1-based numeric position.

### Joins and fanout

Raw mode reuses the model's directed join graph: when fields span multiple data objects connected by many-to-one joins, the planner walks the graph and emits the necessary `LEFT JOIN`s. Fanout protection still applies — reversed many-to-one joins (which would multiply rows on the "one" side) are rejected.

### Multi-fact raw queries (raw CFL)

When `select.fields` references columns from **independent fact tables** — facts that share a common dimension via reverse many-to-one joins but are not connected to each other directly — the planner emits a Composite Fact Layer: one `UNION ALL` leg per leg-root fact, with NULL-padding for fields not reachable from that leg. The outer query selects from the composite CTE.

```yaml
# Customers ← Orders (m:1)
# Customers ← Returns (m:1)
select:
  fields:
    - Customers.Name
    - Orders.Order ID
    - Orders.Amount
    - Returns.Return ID
    - Returns.Refund
  distinct: true
```

Compiles roughly to:

```sql
WITH composite_raw_01 AS (
  SELECT c.NAME AS "Customers.Name",
         o.ORDER_ID AS "Orders.Order ID",
         o.AMOUNT AS "Orders.Amount",
         CAST(NULL AS VARCHAR) AS "Returns.Return ID",
         CAST(NULL AS FLOAT)   AS "Returns.Refund"
  FROM ORDERS o LEFT JOIN CUSTOMERS c ON o.CUSTOMER_ID = c.CUSTOMER_ID
  UNION ALL
  SELECT c.NAME, CAST(NULL AS VARCHAR), CAST(NULL AS FLOAT),
         r.RETURN_ID, r.REFUND
  FROM RETURNS r LEFT JOIN CUSTOMERS c ON r.CUSTOMER_ID = c.CUSTOMER_ID
)
SELECT DISTINCT * FROM composite_raw_01
```

A "leg root" is a fact data object referenced by some field that is not reachable from another field's source via directed joins — i.e. it is maximal under reachability. Each leg root yields one `UNION ALL` leg. Conformed dim columns (those reachable from every leg) project the same value across legs and line up; fact-specific columns are typed `CAST(NULL AS …)` in legs that don't cover them. `distinct: true` is applied once at the outer query — the most portable place.

WHERE filters are applied to legs whose joined objects contain all the filter's referenced data objects; ORDER BY is remapped to the field aliases at the outer level.

#### `UNION ALL BY NAME` optimization (DuckDB, Snowflake)

DuckDB and Snowflake both support `UNION ALL BY NAME`, which aligns columns by name across legs and auto-fills any missing columns with `NULL`. On these dialects the planner skips the per-leg `CAST(NULL AS …)` padding entirely and emits only the columns each leg actually has — the database does the rest:

```sql
-- DuckDB / Snowflake
WITH composite_raw_01 AS (
  SELECT c.NAME AS "Customers.Name", o.ORDER_ID AS "Orders.Order ID", o.AMOUNT AS "Orders.Amount"
  FROM ORDERS o LEFT JOIN CUSTOMERS c ON o.CUSTOMER_ID = c.CUSTOMER_ID
  UNION ALL BY NAME
  SELECT c.NAME, r.RETURN_ID AS "Returns.Return ID", r.REFUND AS "Returns.Refund"
  FROM RETURNS r LEFT JOIN CUSTOMERS c ON r.CUSTOMER_ID = c.CUSTOMER_ID
)
SELECT DISTINCT * FROM composite_raw_01
```

The other six dialects (BigQuery, ClickHouse, Databricks, Dremio, MySQL, Postgres) keep the explicit typed-NULL padding shown above. Output rows are identical either way; the optimization is purely about leg readability and slightly less SQL the database has to parse.

## Secondary Join Paths

When a model defines secondary joins (e.g., `Flights` → `Airports` via departure and arrival), use `usePathNames` to select which join path to use:

```yaml
select:
  dimensions:
    - Airport Name
  measures:
    - Total Ticket Price
usePathNames:
  - source: Flights
    target: Airports
    pathName: arrival
```

Each entry specifies a `(source, target, pathName)` triple. The `pathName` must match a secondary join defined in the model. When active, the secondary join replaces the primary join for that pair.

### In Python

```python
from orionbelt.models.query import QueryObject, QuerySelect, UsePathName

query = QueryObject(
    select=QuerySelect(
        dimensions=["Airport Name"],
        measures=["Total Ticket Price"],
    ),
    use_path_names=[
        UsePathName(source="Flights", target="Airports", path_name="arrival"),
    ],
)
```

### In JSON (full mode)

```json
{
  "select": {
    "dimensions": ["Airport Name"],
    "measures": ["Total Ticket Price"]
  },
  "usePathNames": [
    {"source": "Flights", "target": "Airports", "pathName": "arrival"}
  ]
}
```

If a `usePathNames` entry references a non-existent data object or pathName, the query will return a resolution error.

## Dimension Exclusion (Anti-Join)

The `dimensionsExclude` flag inverts a dimension-only query to return value combinations that do **not** exist in the data. This is useful for finding missing relationships — for example, directors and producers who have never collaborated on a movie.

```yaml
select:
  dimensions:
    - Director
    - Producer
dimensionsExclude: true
```

This generates an anti-join query using SQL `EXCEPT`:

1. **All possible combinations** — A `CROSS JOIN` of the distinct values of each dimension
2. **Existing combinations** — The actual dimension pairs found through the join graph
3. **Result** — All combinations `EXCEPT` existing ones

### Constraints

- **No measures allowed** — `dimensionsExclude` only works with dimension-only queries (no measures or metrics)
- **2+ dimensions required** — At least two dimensions must be specified

### In Python

```python
from orionbelt.models.query import QueryObject, QuerySelect

query = QueryObject(
    select=QuerySelect(dimensions=["Director", "Producer"]),
    dimensions_exclude=True,
)
```

### In JSON

```json
{
  "select": {
    "dimensions": ["Director", "Producer"]
  },
  "dimensionsExclude": true
}
```

### Error Codes

| Error Code | Cause |
|------------|-------|
| `DIMENSIONS_EXCLUDE_WITH_MEASURES` | Query includes measures — not allowed with `dimensionsExclude` |
| `DIMENSIONS_EXCLUDE_INSUFFICIENT` | Fewer than 2 dimensions specified |

## Filters

Filters restrict the result set. **Dimension filters** go in `where` (become SQL `WHERE`), and **measure filters** go in `having` (become SQL `HAVING`).

```yaml
where:
  # By dimension name
  - field: Customer Country
    op: equals
    value: Germany
  # By qualified column (DataObject.Column) — no dimension needed
  - field: Orders.Order Status
    op: equals
    value: F

having:
  - field: Revenue
    op: gte
    value: 5000
```

Multiple top-level filters are combined with **AND**. For **OR** logic or more complex boolean expressions, use [filter groups](#filter-groups).

### Filter Structure

| Property | Type | Description |
|----------|------|-------------|
| `field` | string | Dimension name or `DataObject.Column` (`where`), measure name (`having`) |
| `op` | string | Filter operator (see table below) |
| `value` | any | Comparison value (string, number, boolean, date, list, etc.) |

**Date and timestamp values** are supported as ISO 8601 strings. All variants work: `"2026-01-01"`, `"2026-01-01T14:30:00"`, `"2026-01-01T00:00:00Z"`, `"2026-01-01T14:30:00+02:00"`. When constructing queries in Python, `datetime.date` and `datetime.datetime` objects are automatically coerced to ISO strings.

```json
{
  "where": [
    { "field": "Order Date", "op": ">=", "value": "2026-01-01" },
    { "field": "Created At", "op": ">=", "value": "2026-01-01T00:00:00Z" },
    { "field": "Order Date", "op": "<", "value": "2027-01-01" }
  ]
}
```

### Filter Groups (AND / OR / NOT) { #filter-groups }

A **filter group** combines multiple filters with `and` or `or` logic. Groups can be nested recursively for complex boolean expressions.

```yaml
where:
  # Simple OR: country is US or CA
  - logic: or
    filters:
      - field: Customer Country
        op: equals
        value: US
      - field: Customer Country
        op: equals
        value: CA
```

#### Nested groups: (A OR B) AND C

```yaml
where:
  - logic: and
    filters:
      - logic: or
        filters:
          - field: Customer Country
            op: equals
            value: US
          - field: Customer Country
            op: equals
            value: CA
      - field: Market Segment
        op: equals
        value: BUILDING
```

#### Negation: NOT (A OR B)

```yaml
where:
  - logic: or
    negated: true
    filters:
      - field: Order Status
        op: equals
        value: P
      - field: Order Status
        op: equals
        value: F
```

#### Filter Group Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `logic` | enum | `and` | `and` or `or` — how to combine child filters |
| `filters` | list | — | Child filters (leaf filters or nested filter groups) |
| `negated` | bool | `false` | Wrap the combined expression with `NOT` |

Filter groups and leaf filters can be mixed freely at any level — in `where`, `having`, or nested inside other groups.

#### In Python

```python
from orionbelt.models.query import (
    QueryFilter, QueryFilterGroup, FilterOperator,
)

# (country = 'US' OR country = 'CA') AND segment = 'BUILDING'
where = [
    QueryFilterGroup(
        logic="and",
        filters=[
            QueryFilterGroup(
                logic="or",
                filters=[
                    QueryFilter(field="Customer Country", op=FilterOperator.EQ, value="US"),
                    QueryFilter(field="Customer Country", op=FilterOperator.EQ, value="CA"),
                ],
            ),
            QueryFilter(field="Market Segment", op=FilterOperator.EQ, value="BUILDING"),
        ],
    ),
]
```

### Filter Reachability

A `where` filter field can reference:

- A **dimension name** (e.g. `Order Priority`) — resolves to the dimension's data object and column
- A **qualified column** using `DataObject.Column` dot notation (e.g. `Orders.Order Priority`) — directly references a column without requiring a dimension definition

The referenced data object must be reachable from the query's join graph:

- Directly joined in the query (base object or any object in the join path)
- A descendant — reachable via directed joins from any already-joined object

If the data object is reachable but not yet in the join path, it is **auto-joined** automatically. If the data object is not reachable at all, the filter is **silently skipped** — it is irrelevant to the current query.

A `having` filter field must reference a **measure** name.

### Filter Operators

OrionBelt supports two operator naming conventions — OBML style and SQL style. Both are equivalent.

#### Comparison Operators

| OBML | SQL Style | SQL Output | Value Type |
|-----------|-----------|------------|------------|
| `equals` | `=`, `eq` | `= value` | scalar |
| `notequals` | `!=`, `neq` | `<> value` | scalar |
| `gt` | `>`, `greater` | `> value` | scalar |
| `gte` | `>=`, `greater_eq` | `>= value` | scalar |
| `lt` | `<`, `less` | `< value` | scalar |
| `lte` | `<=`, `less_eq` | `<= value` | scalar |

#### Set Operators

| OBML | SQL Style | SQL Output | Value Type |
|-----------|-----------|------------|------------|
| `inlist` | `in` | `IN (v1, v2, ...)` | list |
| `notinlist` | `not_in` | `NOT IN (v1, v2, ...)` | list |

#### Null Operators

| OBML | SQL Style | SQL Output | Value Type |
|-----------|-----------|------------|------------|
| `set` | `is_not_null` | `IS NOT NULL` | none |
| `notset` | `is_null` | `IS NULL` | none |
| `blank` | — | `(col IS NULL OR TRIM(col) = '')` | none |
| `notblank` | — | `(col IS NOT NULL AND TRIM(col) <> '')` | none |

`blank` / `notblank` go beyond `is_null`: they also treat a string of whitespace-only characters as empty, which is the practical "missing value" check for free-text columns.

#### String Operators

| Operator | SQL Output | Value Type |
|----------|------------|------------|
| `contains` | `LIKE '%value%'` (dialect-specific) | string |
| `notcontains` | `NOT LIKE '%value%'` | string |
| `starts_with` | `LIKE 'value%'` | string |
| `ends_with` | `LIKE '%value'` | string |
| `like` | `LIKE 'pattern'` | string |
| `notlike` | `NOT LIKE 'pattern'` | string |

#### Regex Operators

| Operator | Value Type | Notes |
|----------|------------|-------|
| `regex` | string (regex pattern) | Match values against a regular expression |
| `notregex` | string (regex pattern) | Inverse of `regex` |

Regex syntax is delegated to the target dialect's native regex engine, so the *flavour* of regex differs per dialect. The compiler emits dialect-appropriate SQL:

| Dialect | Generated SQL |
|---------|---------------|
| Postgres | `(col ~ 'pattern')` / `(col !~ 'pattern')` (POSIX) |
| DuckDB | `regexp_matches(col, 'pattern')` (RE2) |
| ClickHouse | `match(col, 'pattern')` (RE2) |
| BigQuery | `REGEXP_CONTAINS(col, 'pattern')` (RE2) |
| MySQL | `(col REGEXP 'pattern')` (POSIX-extended via ICU) |
| Databricks | `(col RLIKE 'pattern')` (Java regex) |
| Snowflake, Dremio | `REGEXP_LIKE(col, 'pattern')` (POSIX-extended) |

Use only the common subset of regex features (anchors, character classes, alternation, basic quantifiers) if a query has to be portable across dialects. Backreferences, lookarounds, and named groups are not portable.

#### String Length Operators

| Operator | SQL Output | Value Type |
|----------|------------|------------|
| `length_eq` | `LENGTH(col) = N` | integer |
| `length_gt` | `LENGTH(col) > N` | integer |
| `length_lt` | `LENGTH(col) < N` | integer |

Useful for filtering on padded codes, fixed-width identifiers, or detecting truncated strings. The value must be a non-negative integer.

#### Range Operators

| Operator | SQL Output | Value Type |
|----------|------------|------------|
| `between` | `BETWEEN low AND high` | list of 2 |
| `notbetween` | `NOT BETWEEN low AND high` | list of 2 |
| `relative` | Relative time range | object |

**Relative filter object**

The `relative` operator expects an object with the following keys:

- `unit`: one of `day`, `week`, `month`, `year`
- `count`: positive integer number of units
- `direction` (optional): `past` (default) or `future`
- `include_current` (optional): boolean, default `true`

Example (last 7 days, inclusive of today):

```yaml
where:
  - field: Order Date
    op: relative
    value:
      unit: day
      count: 7
      direction: past
      include_current: true
```

!!! info "String contains is dialect-aware"
    The `contains` operator generates different SQL per dialect:

    - **Postgres/ClickHouse**: `ILIKE '%' || value || '%'`
    - **Snowflake**: `CONTAINS(column, value)`
    - **Dremio/Databricks**: `LOWER(column) LIKE '%' || LOWER(value) || '%'`

#### Existence Operators

`exists` / `nonexists` express "this row has (or doesn't have) a matching row in a related data object" as a correlated `EXISTS (SELECT 1 FROM …)` subquery. The first-class primitive for regulatory data-quality rules, coverage and anti-join reports, and any "parent has at least one child of kind X" question.

| Operator | SQL Output | Payload |
|----------|------------|---------|
| `exists` | `EXISTS (SELECT 1 FROM target WHERE target.k = subject.k …)` | `subquery` object |
| `nonexists` | `NOT EXISTS (…)` | `subquery` object |

Both operators take a `subquery:` object (not `value:`) describing the target and any extra predicates restricting which target rows count:

```yaml
where:
  - field: Order ID
    op: exists
    subquery:
      dataObject: OrderItems           # target — reachable from the subject's data object
      filter:                          # optional predicates on the target rows
        - field: Is Returned
          op: equals
          value: true
```

For "orders with no payment":

```yaml
where:
  - field: Order ID
    op: nonexists
    subquery:
      dataObject: Payments
```

The join columns are **not** restated — the planner walks the model's existing `joins:` from the subject's data object to `subquery.dataObject`. When multiple secondary joins exist between subject and target, pin the path with `pathName:`:

```yaml
where:
  - field: Order ID
    op: exists
    subquery:
      dataObject: Returns
      pathName: viaWarehouse        # matches a declared secondary join's pathName
```

**Subquery filter rules**

`subquery.filter` accepts the same operator vocabulary as the outer filter, with two exceptions:

* Nested `exists` / `nonexists` are rejected (`NESTED_SUBQUERY_NOT_SUPPORTED`) — keeps the planner simple.
* `field` is interpreted as a column **on the target data object** (e.g. `Is Returned` lives on `OrderItems`), not a dimension on the model.

**`exists` / `nonexists` are WHERE-only.** Both operators are rejected in `having:` with `INVALID_FILTER_OPERATOR` because the correlation predicate references the subject's row-level column, which is out of scope after `GROUP BY`. Measure-level EXISTS — "groups where some matching child row exists" — is a deferred follow-up (`MeasureFilter.subquery`).

`EXISTS` is portable — all 8 dialects compile the same shape, only identifier quoting differs.

## Ordering

Sort results by dimension or measure names from the query's SELECT, or by numeric position:

```yaml
orderBy:
  - field: Revenue
    direction: desc
  - field: Customer Country
    direction: asc       # default
  - field: "1"           # numeric position (1-based)
    direction: asc
```

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `field` | string | — | Dimension or measure name from the query's SELECT, or a numeric position (e.g. `"1"`) |
| `direction` | enum | `asc` | `asc` or `desc` |

The `field` must reference a dimension or measure that appears in the query's `select`. It cannot reference fields outside the SELECT. Alternatively, use a numeric string to reference the SELECT column by position (1-based).

## Limit

Restrict the number of returned rows:

```yaml
limit: 1000
```

## Validation

Invalid queries return error responses:

| Error Code | Status | Cause |
|------------|--------|-------|
| `UNKNOWN_DIMENSION` | 400 | Dimension name not in model |
| `UNKNOWN_MEASURE` | 400 | Measure name not in model |
| `UNKNOWN_FILTER_FIELD` | 400 | Filter field is not a dimension (WHERE) or measure (HAVING) |
| `UNKNOWN_ORDER_BY_FIELD` | 400 | ORDER BY field not in query's SELECT |
| `INVALID_ORDER_BY_POSITION` | 400 | Numeric ORDER BY position out of range |
| `INVALID_FILTER_OPERATOR` | 400 | Unrecognized filter operator |
| `INVALID_RELATIVE_FILTER` | 400 | Malformed relative time filter |
| `UNKNOWN_SUBQUERY_DATA_OBJECT` | 400 | `exists` / `nonexists` references an unknown target data object |
| `NO_JOIN_PATH_TO_SUBQUERY` | 400 | No join path exists from the filter subject to the subquery target |
| `UNKNOWN_SUBQUERY_FILTER_COLUMN` | 400 | `subquery.filter` references a column not on the target |
| `NESTED_SUBQUERY_NOT_SUPPORTED` | 400 | `subquery.filter` cannot contain another `exists` / `nonexists` |
| `UNKNOWN_PATH_NAME` | 400 | `subquery.pathName` does not match any declared secondary join |
| `AMBIGUOUS_JOIN` | 422 | Multiple join paths possible |
| `DIMENSIONS_EXCLUDE_WITH_MEASURES` | 400 | `dimensionsExclude` used with measures |
| `DIMENSIONS_EXCLUDE_INSUFFICIENT` | 400 | `dimensionsExclude` with fewer than 2 dimensions |

## Semantics Summary

| Query Element | SQL Equivalent |
|---------------|----------------|
| `select.dimensions` | `SELECT` + `GROUP BY` columns |
| `select.measures` | `SELECT` aggregate expressions |
| `where` | `WHERE` clause |
| `having` | `HAVING` clause |
| `orderBy` | `ORDER BY` clause |
| `limit` | `LIMIT` clause |
| `dimensionsExclude` | Anti-join via `CROSS JOIN` + `EXCEPT` (dimension-only) |
