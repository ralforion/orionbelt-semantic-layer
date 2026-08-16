# Grain & Filter Context Overrides

Grain and filter context overrides give per-measure control over **which dimensions** the measure aggregates by and **which query filters** apply. This enables analytical patterns like percent-of-parent totals, unfiltered grand totals, and selective filter exclusion -- similar to DAX `CALCULATE`, Tableau LOD expressions, and ThoughtSpot `group_aggregate`.

## Grain Override

The `grain` property controls the aggregation grain independently from the query dimensions. Without it, every measure aggregates at the query's GROUP BY level. With `grain`, a measure can aggregate at a coarser level (fewer dimensions) within the same query.

### OBML Syntax

```yaml
measures:
  Total Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: FIXED
      # empty include = grand total (equivalent to total: true)

  Revenue by Region:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: FIXED
      include: [Region]     # aggregate at region level only

  Revenue excl. Subcategory:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: RELATIVE        # start from query dimensions
      exclude: [Subcategory] # remove subcategory from grain
```

### Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `mode` | `FIXED` \| `RELATIVE` | `RELATIVE` | `RELATIVE`: inherit query dimensions. `FIXED`: start with empty grain. |
| `exclude` | list[str] | `[]` | Dimension names to remove from inherited grain. Only valid with `RELATIVE`. |
| `include` | list[str] | `[]` | Dimension names to add. With `FIXED`: these become the grain. With `RELATIVE`: added to inherited. |
| `keepOnly` | list[str] | `[]` | Adaptive grain: `keepOnly ∩ query_dims`. Dimensions from this list that are present in the query. Only valid with `FIXED`. |

### How Effective Grain Is Resolved

```
if mode == FIXED:
    if keepOnly:
        effective = keepOnly ∩ query_dimensions
    else:
        effective = include
elif mode == RELATIVE:
    effective = query_dimensions - exclude + include
```

The effective grain must always be a subset of (or equal to) the query dimensions. This guarantees that joining the grain-overridden result back produces a many-to-one join (no fanout).

### Relationship to `total: true`

`total: true` is equivalent to `grain: { mode: FIXED }` (empty FIXED = grand total). Both are supported -- `total` is preserved as a convenient shorthand. They are mutually exclusive on the same measure.

## Filter Context

The `filterContext` property controls which query WHERE filters apply to a measure. This is distinct from the existing `filters` property (which adds `CASE WHEN` conditions inside the aggregate function).

| Feature | Purpose | SQL mechanism | When applied |
|---------|---------|---------------|-------------|
| `filters` (existing) | Narrow rows within the aggregate | `CASE WHEN` inside `SUM(...)` | Always, within same query |
| `filterContext` (new) | Change which query-level filters apply | Different WHERE clause | Requires separate CTE |

### OBML Syntax

```yaml
measures:
  Unfiltered Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    filterContext:
      mode: FIXED           # ignore all query filters

  Revenue without Color Filter:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    filterContext:
      mode: RELATIVE        # inherit query filters
      exclude: [Color]       # but remove the color filter

  Revenue for EMEA Only:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    filterContext:
      mode: FIXED
      include:
        - field: Region
          op: '='
          value: EMEA
```

### Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `mode` | `FIXED` \| `RELATIVE` | `RELATIVE` | `RELATIVE`: inherit query WHERE filters. `FIXED`: start with no filters. |
| `exclude` | list[str] | `[]` | Dimension names. Any query filter referencing these fields is removed. |
| `include` | list[FilterItem] | `[]` | Static filter conditions to add (each has `field`, `op`, `value`). |
| `keepOnly` | list[str] | `[]` | Only query filters referencing these fields are kept. Complement of `exclude`. |

!!! note
    `mode: FIXED` cannot be combined with `exclude` (there are no inherited filters to exclude from).

### Filter Matching

When deciding whether a query filter matches an `exclude` or `keepOnly` entry:

- Each query WHERE filter tracks which dimension fields it references.
- If any referenced field matches a listed name, the entire filter clause is matched.
- `exclude` removes matching filters; `keepOnly` keeps only matching filters.

## Combining Grain and Filter Context

A measure can use both `grain` and `filterContext` together. The grain controls the GROUP BY and the filter context controls the WHERE clause of the isolated CTE.

```yaml
measures:
  Unfiltered Region Total:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: FIXED
      include: [Region]
    filterContext:
      mode: FIXED
```

This produces a CTE with `GROUP BY region` and no WHERE clause, joined back to the main query on `region`.

## Examples

### Percent of Total

Revenue per product as a percentage of overall revenue:

```yaml
measures:
  Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'

  Total Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: FIXED

metrics:
  Pct of Total:
    expression: '{[Revenue]} / {[Total Revenue]}'
```

**Query:** Select `Product`, `Revenue`, `Pct of Total`

```sql
WITH base AS (
  SELECT "Product", SUM("AMOUNT") AS "Revenue"
  FROM ORDERS
  WHERE "YEAR" = 2024
  GROUP BY "Product"
)
SELECT "Product", "Revenue",
       "Revenue" / SUM("Revenue") OVER () AS "Pct of Total"
FROM base
```

### Percent of Parent (Region)

Revenue per product as a percentage of its region's revenue:

```yaml
measures:
  Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'

  Revenue by Region:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: FIXED
      include: [Region]

metrics:
  Pct of Region:
    expression: '{[Revenue]} / {[Revenue by Region]}'
```

**Query:** Select `Region`, `Product`, `Revenue`, `Pct of Region`

```sql
WITH base AS (
  SELECT "Region", "Product", SUM("AMOUNT") AS "Revenue"
  FROM ORDERS
  WHERE "YEAR" = 2024
  GROUP BY "Region", "Product"
)
SELECT "Region", "Product", "Revenue",
       "Revenue" / SUM("Revenue") OVER (PARTITION BY "Region")
         AS "Pct of Region"
FROM base
```

### Unfiltered Grand Total

A grand total that ignores all query filters:

```yaml
measures:
  Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'

  Unfiltered Total:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    grain:
      mode: FIXED
    filterContext:
      mode: FIXED
```

**Query:** Select `Region`, `Revenue`, `Unfiltered Total` with WHERE `Year = 2024`

```sql
WITH main AS (
  SELECT "Region", SUM("AMOUNT") AS "Revenue"
  FROM ORDERS
  WHERE "YEAR" = 2024
  GROUP BY "Region"
),
fc_0 AS (
  SELECT SUM("AMOUNT") AS "Unfiltered Total"
  FROM ORDERS
)
SELECT main."Region", main."Revenue", fc_0."Unfiltered Total"
FROM main
CROSS JOIN fc_0
```

### Selective Filter Exclusion

Revenue without the color filter, while other filters still apply:

```yaml
measures:
  Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'

  Revenue No Color:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    filterContext:
      mode: RELATIVE
      exclude: [Color]
```

**Query:** Select `Region`, `Revenue`, `Revenue No Color` with WHERE `Year = 2024 AND Color = 'Red'`

```sql
WITH main AS (
  SELECT "Region", SUM("AMOUNT") AS "Revenue"
  FROM ORDERS
  WHERE "YEAR" = 2024 AND "COLOR" = 'Red'
  GROUP BY "Region"
),
fc_0 AS (
  SELECT "Region", SUM("AMOUNT") AS "Revenue No Color"
  FROM ORDERS
  WHERE "YEAR" = 2024
  GROUP BY "Region"
)
SELECT main."Region", main."Revenue", fc_0."Revenue No Color"
FROM main
LEFT JOIN fc_0 ON main."Region" = fc_0."Region"
```

## How It Works (Compilation)

The compilation pipeline handles grain and filter context in two phases:

| Phase | Module | Handles |
|-------|--------|---------|
| Phase 2.3 | `filter_wrap.py` | Measures with `filterContext` -- isolates into separate CTEs with modified WHERE |
| Phase 2.5 | `total_wrap.py` | Measures with `grain` (no filterContext) -- window functions `OVER (PARTITION BY ...)` |

### Strategy Selection

| Grain | Filter context | Strategy |
|-------|---------------|----------|
| Same as query | Same as query | Inline (no wrapping needed) |
| Subset of query dims | Same as query | Window function `OVER (PARTITION BY ...)` |
| Empty (grand total) | Same as query | Window function `OVER ()` |
| Same as query | Different | CTE + LEFT JOIN on all dims |
| Subset of query dims | Different | CTE + LEFT JOIN on subset dims |
| Empty (scalar) | Different | CTE + CROSS JOIN |

### CTE Structure for Filter Context

Measures with `filterContext` are grouped by their effective filter + grain
combination, and by the fact they read. Each group gets its own CTE:

- **`main`** CTE: the original query with inline measures (no filter context)
- **`fc_0`, `fc_1`, ...** CTEs: the isolated scans
- **Outer SELECT**: joins all CTEs together (LEFT JOIN on shared dimensions, CROSS JOIN for scalar)

An isolated scan is **planned as a query in its own right**, not assembled from
the main query's `FROM` with a different `WHERE`. It resolves its own base
object, its own join path to the grain dimensions, and runs the same
fanout and deduplication phases any query runs. That is what lets it read a
fact the rest of the query does not, and what makes a measure on the *one* side
of a join count once rather than once per row of the many side.

Two consequences worth knowing:

- The scan joins whatever its own filters need. An `include:` filter on a
  column nothing else in the query reads still gets its join.
- A filterContext measure does not decide the main query's base object, since
  it is not planned there. Removing it from that decision can leave the rest of
  the query single-fact where it used to be multi-fact -- the same answer,
  planned more directly.

## What Composes

| Combination | Result |
|---|---|
| Read through a metric (`{[Revenue]} / {[Unfiltered Revenue]}`) | Works. The component is computed in the same CTE a selected measure gets, and the formula is rebuilt in the outer query over the CTEs' columns. |
| `total: true` or a `grain` override on the same measure | Works. `total: true` is read as the empty grain it is, so the scan groups by nothing and reports a grand total. |
| A query spanning facts that cannot be joined | Works. The measure is kept out of the UNION ALL and its scan reads its own fact. |
| `HAVING` on the measure, or on a metric reading it | Works. The predicate moves to the outer query, where the value is a column. |
| `ORDER BY` on the measure | Works. It reads whichever CTE computed the value. |
| Several contexts, over one fact or several | Works. One scan per (context, grain, fact). |
| A **cumulative** metric over the measure | Works. The running total accumulates the context's values, not the query-filtered ones. |

## What Is Refused

Each of these is refused, with a message naming what is wrong and what to
change, rather than compiled into a number that looks right:

| Combination | Why |
|---|---|
| An **averaged** `total: true` measure in the same query | A total average is rebuilt from the `SUM` and `COUNT` of the aggregate's argument. Once the filter context has been computed, that argument is a value already averaged. Total a sum instead. |
| A **period-over-period** metric in the same query | PoP rebuilds the query's `FROM` from a date spine, which cannot read the isolated scan and regroups the query to its own grain. |
| A measure whose own arguments span facts no join reaches | There is no single fact to plan the scan against. |
| A grain dimension the measure's fact cannot reach | The scan has to group by the dimensions it joins back on. Reported as an unreachable object, by the same rule that governs any query asking for one. |

## Constraints

!!! warning "Current limitations"
    - The effective grain must be a subset of the query dimensions (superset/disjoint grains are rejected to prevent fanout).
    - `total: true` and `grain` are mutually exclusive on the same measure.
    - `mode: FIXED` cannot be combined with `exclude` on either `grain` or `filterContext`.
    - Grain overrides are not combined with period-over-period or cumulative metrics in the same query. A warning is emitted if attempted.

## Dialect Support

Grain and filter context overrides produce standard SQL (window functions and CTEs) and work identically across all eight supported dialects. No dialect-specific compilation is needed.
