# Period-over-Period (PoP) Metrics

Period-over-Period metrics compare a measure against a prior time period. Common use cases include Year-over-Year (YoY) revenue growth, Month-over-Month (MoM) changes, and same-period-last-year comparisons.

OrionBelt implements PoP using a **synthetical date pattern** -- a 4-CTE architecture that:

- Auto-discovers the date range from actual data (no hardcoded date range in OBML)
- Generates a date spine with previous-period lookup
- LEFT JOINs facts onto the spine
- Self-joins for period comparison

This approach works across all eight supported dialects with no additional configuration beyond the metric definition itself.

## OBML Syntax

Period-over-Period metrics are declared with `type: period_over_period` and a `periodOverPeriod` configuration block:

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
```

### Properties

| Property | Type | Default | Description |
|----------|------|---------|-------------|
| `timeDimension` | string | -- | Name of time dimension used for period comparison (must be in SELECT) |
| `grain` | timeGrain | -- | Time grain for the date spine (e.g. `month`, `quarter`, `year`) |
| `offset` | integer | `-1` | Number of periods to look back (negative = past) |
| `offsetGrain` | timeGrain | -- | Unit for the offset (e.g. `year` for YoY, `month` for MoM) |
| `comparison` | enum | `percentChange` | How to compare: `ratio`, `difference`, `previousValue`, `percentChange` |

### Comparison Types

| Type | Formula | Example |
|------|---------|---------|
| `percentChange` | `current / NULLIF(prev, 0) - 1` | Revenue grew 15% -- result is `0.15` |
| `ratio` | `current / NULLIF(prev, 0)` | Revenue is 1.15x previous |
| `difference` | `current - prev` | Revenue increased by $50k |
| `previousValue` | `prev` | Last year's revenue was $300k |

!!! note
    The `NULLIF(prev, 0)` guard prevents division-by-zero errors when the previous period has no data. The result is `NULL` in that case.

## Examples

### Year-over-Year (YoY) Growth

Compare each month's revenue against the same month one year earlier:

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
```

### Month-over-Month (MoM) Difference

Compute the absolute change in revenue from one month to the next:

```yaml
metrics:
  Revenue MoM Change:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: month
      comparison: difference
```

### Previous Year Value

Retrieve last year's revenue alongside the current period (no calculation, just the prior value):

```yaml
metrics:
  Revenue Last Year:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: month
      offset: -1
      offsetGrain: year
      comparison: previousValue
```

### Quarter-over-Quarter Ratio

Compare quarterly revenue as a ratio:

```yaml
metrics:
  Revenue QoQ Ratio:
    type: period_over_period
    expression: '{[Revenue]}'
    periodOverPeriod:
      timeDimension: Order Date
      grain: quarter
      offset: -1
      offsetGrain: quarter
      comparison: ratio
```

## How It Works (CTE Architecture)

OrionBelt compiles PoP metrics into a 4-CTE structure. Each CTE builds on the previous one to produce the final period comparison.

| CTE | Purpose |
|-----|---------|
| `date_range` | Discover `MIN`/`MAX` date from fact tables with ALL query filters pushed down |
| `date_spine` | Generate date series with `spine_date` and `spine_date_prev` columns |
| `pop_base` | Aggregate measures using the spine as `FROM`, with facts LEFT JOINed |
| `pop_compare` | Self-join `pop_base` via `spine_date_prev` for period comparison |

### Filter Push-Down

!!! important
    ALL query `WHERE` filters -- both time filters and dimension filters -- are pushed into the `date_range` CTE. This means that dimension filters like `Country = 'Germany'` correctly narrow the date range. If sales in Germany only started in 2024, the spine will not extend further back than that.

This design ensures the date spine is scoped to the actual data range relevant to the query, avoiding unnecessary NULL rows for periods with no matching data.

### Generated SQL Example

The following shows the compiled Postgres SQL for a YoY growth query selecting `Order Date` (monthly) and `Revenue YoY Growth`:

=== "Postgres"

    ```sql
    WITH date_range AS (
      SELECT date_trunc('month', MIN("Orders"."ORDER_DATE")) AS min_date,
             date_trunc('month', MAX("Orders"."ORDER_DATE")) AS max_date
      FROM PUBLIC.ORDERS AS "Orders"
    ),
    date_spine AS (
      SELECT d::date AS spine_date,
             CASE WHEN (d + INTERVAL '-1 year')::date >= date_range.min_date
                  THEN (d + INTERVAL '-1 year')::date END AS spine_date_prev
      FROM date_range,
           generate_series(date_range.min_date::timestamp,
                           date_range.max_date::timestamp,
                           INTERVAL '1 month') AS t(d)
    ),
    pop_base AS (
      SELECT date_spine.spine_date AS "Order Date",
             SUM("Orders"."AMOUNT") AS "Revenue"
      FROM date_spine
      LEFT JOIN PUBLIC.ORDERS AS "Orders"
        ON date_trunc('month', "Orders"."ORDER_DATE") = date_spine.spine_date
      GROUP BY 1
    ),
    pop_compare AS (
      SELECT pop_base."Order Date",
             pop_base."Revenue",
             pop_base."Revenue" / NULLIF(prev."Revenue", 0) - 1
               AS "Revenue YoY Growth"
      FROM pop_base
      LEFT JOIN date_spine ON pop_base."Order Date" = date_spine.spine_date
      LEFT JOIN pop_base AS prev
        ON date_spine.spine_date_prev = prev."Order Date"
    )
    SELECT "Order Date", "Revenue", "Revenue YoY Growth"
    FROM pop_compare
    ORDER BY 1
    ```

=== "Snowflake"

    ```sql
    WITH date_range AS (
      SELECT DATE_TRUNC('month', MIN("Orders"."ORDER_DATE")) AS min_date,
             DATE_TRUNC('month', MAX("Orders"."ORDER_DATE")) AS max_date
      FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
    ),
    date_spine AS (
      SELECT DATEADD('month', ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1,
                     date_range.min_date)::DATE AS spine_date,
             CASE WHEN DATEADD('year', -1,
                     DATEADD('month', ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1,
                             date_range.min_date))::DATE >= date_range.min_date
                  THEN DATEADD('year', -1,
                     DATEADD('month', ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1,
                             date_range.min_date))::DATE END AS spine_date_prev
      FROM date_range,
           TABLE(GENERATOR(ROWCOUNT => DATEDIFF('month', date_range.min_date,
                                                 date_range.max_date) + 1))
    ),
    pop_base AS (
      SELECT date_spine.spine_date AS "Order Date",
             SUM("Orders"."AMOUNT") AS "Revenue"
      FROM date_spine
      LEFT JOIN WAREHOUSE.PUBLIC.ORDERS AS "Orders"
        ON DATE_TRUNC('month', "Orders"."ORDER_DATE") = date_spine.spine_date
      GROUP BY 1
    ),
    pop_compare AS (
      SELECT pop_base."Order Date",
             pop_base."Revenue",
             pop_base."Revenue" / NULLIF(prev."Revenue", 0) - 1
               AS "Revenue YoY Growth"
      FROM pop_base
      LEFT JOIN date_spine ON pop_base."Order Date" = date_spine.spine_date
      LEFT JOIN pop_base AS prev
        ON date_spine.spine_date_prev = prev."Order Date"
    )
    SELECT "Order Date", "Revenue", "Revenue YoY Growth"
    FROM pop_compare
    ORDER BY 1
    ```

=== "BigQuery"

    ```sql
    WITH date_range AS (
      SELECT DATE_TRUNC(MIN(`Orders`.`ORDER_DATE`), MONTH) AS min_date,
             DATE_TRUNC(MAX(`Orders`.`ORDER_DATE`), MONTH) AS max_date
      FROM `PROJECT.DATASET.ORDERS` AS `Orders`
    ),
    date_spine AS (
      SELECT d AS spine_date,
             CASE WHEN DATE_SUB(d, INTERVAL 1 YEAR) >= date_range.min_date
                  THEN DATE_SUB(d, INTERVAL 1 YEAR) END AS spine_date_prev
      FROM date_range,
           UNNEST(GENERATE_DATE_ARRAY(date_range.min_date,
                                       date_range.max_date,
                                       INTERVAL 1 MONTH)) AS d
    ),
    pop_base AS (
      SELECT date_spine.spine_date AS `Order Date`,
             SUM(`Orders`.`AMOUNT`) AS `Revenue`
      FROM date_spine
      LEFT JOIN `PROJECT.DATASET.ORDERS` AS `Orders`
        ON DATE_TRUNC(`Orders`.`ORDER_DATE`, MONTH) = date_spine.spine_date
      GROUP BY 1
    ),
    pop_compare AS (
      SELECT pop_base.`Order Date`,
             pop_base.`Revenue`,
             pop_base.`Revenue` / NULLIF(prev.`Revenue`, 0) - 1
               AS `Revenue YoY Growth`
      FROM pop_base
      LEFT JOIN date_spine ON pop_base.`Order Date` = date_spine.spine_date
      LEFT JOIN pop_base AS prev
        ON date_spine.spine_date_prev = prev.`Order Date`
    )
    SELECT `Order Date`, `Revenue`, `Revenue YoY Growth`
    FROM pop_compare
    ORDER BY 1
    ```

## Dialect Support

Each dialect uses a different technique to generate the date spine:

| Dialect | Date Spine Technique |
|---------|---------------------|
| Postgres | `generate_series(min, max, INTERVAL)` |
| DuckDB | `generate_series(min, max, INTERVAL)` |
| Snowflake | `TABLE(GENERATOR(ROWCOUNT => ...))` + `DATEADD` |
| BigQuery | `UNNEST(GENERATE_DATE_ARRAY(min, max, INTERVAL))` |
| Databricks | `EXPLODE(SEQUENCE(min, max, INTERVAL))` |
| MySQL | Recursive CTE: `WITH RECURSIVE dates AS (...)` |
| ClickHouse | `arrayJoin(range(...))` + date arithmetic |
| Dremio | Recursive CTE: `WITH RECURSIVE dates AS (...)` |

!!! note "Recursive CTE fallback"
    MySQL and Dremio lack built-in series-generating functions, so the date spine is generated using a recursive CTE that starts at `min_date` and increments by the grain interval until `max_date` is reached.

## Constraints

!!! warning "Current limitations"
    - All PoP metrics in a single query must share the same `timeDimension` and `grain`.
    - The `timeDimension` must be included in the query's selected dimensions.
    - PoP metrics require an `expression` referencing at least one measure.
