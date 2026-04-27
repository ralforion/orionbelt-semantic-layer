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
order_by:
  - field: Revenue
    direction: desc
limit: 1000
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

### Measures

Measures are referenced by name. They can be simple aggregations, expression-based measures, or metrics.

```yaml
select:
  measures:
    - Revenue
    - Order Count
    - Revenue per Order    # metric
```

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
- `order_by` may reference the alias directly (`field: Employee`) — ordering happens in the outer wrapper where the alias is in scope
- `where` filters belong on the underlying dimension names (filtering is applied per leg, before the COALESCE collapses the values)

**Error codes:** `COALESCE_MISSING_ALIAS`, `DUPLICATE_COALESCE_ALIAS`, `COALESCE_ALIAS_COLLISION`, `COALESCE_TOO_FEW_MEMBERS`, `COALESCE_TYPE_MISMATCH`.

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

### Filter Groups (AND / OR / NOT)

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

#### String Operators

| Operator | SQL Output | Value Type |
|----------|------------|------------|
| `contains` | `LIKE '%value%'` (dialect-specific) | string |
| `notcontains` | `NOT LIKE '%value%'` | string |
| `starts_with` | `LIKE 'value%'` | string |
| `ends_with` | `LIKE '%value'` | string |
| `like` | `LIKE 'pattern'` | string |
| `notlike` | `NOT LIKE 'pattern'` | string |

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

## Ordering

Sort results by dimension or measure names from the query's SELECT, or by numeric position:

```yaml
order_by:
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
| `order_by` | `ORDER BY` clause |
| `limit` | `LIMIT` clause |
| `dimensionsExclude` | Anti-join via `CROSS JOIN` + `EXCEPT` (dimension-only) |
