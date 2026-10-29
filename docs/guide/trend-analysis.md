---
description: "Four trend-analysis primitives in OBML v2.6: partitioned rolling windows, window metrics (RANK, LAG, LEAD, NTILE, FIRST and LAST VALUE), statistical aggregates on a measure, and how they combine."
---

# Trend Analysis

OBML v2.6 ships four trend-analysis primitives. Two extend existing surfaces;
two are new metric types or aggregations. All are deliberately additive: a
v2.5 model continues to compile the same SQL.

| Primitive | Where it lives |
|---|---|
| Per-dimension partitioning on rolling windows | `MetricType.CUMULATIVE` + `partitionBy:` |
| Rank, lag, lead, ntile, first/last value | New `MetricType.WINDOW` |
| Two-column & spread statistical aggregates | New `aggregation:` values on `Measure` |
| Composition of any of the above | Existing `MetricType.DERIVED` — unchanged |

Each primitive is independently shippable; combining them across derived
metrics is the multiplier.

## 1. Partitioned rolling windows

A cumulative metric already supports running totals (unbounded), rolling
windows (`window: N`), and grain-to-date resets (`grainToDate: month`).
v2.6 adds `partitionBy:` — every entry is a dimension defined in the
model and present in the query's SELECT.

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

Each row reads the 12 calendar months ending at its own month, within its
own country, as a self-join of the aggregated rows (abridged, Postgres):

```sql
SELECT cur."Country", cur."order_month",
       AVG(prior."Revenue") AS "Revenue MA12 by Country"
FROM cumulative_base AS cur
JOIN cumulative_base AS prior
  ON cur."Country" = prior."Country"
 AND <months from prior."order_month" to cur."order_month"> BETWEEN 0 AND 11
GROUP BY cur."Country", cur."order_month"
```

The window counts months, not rows: a country with no sales in some month
still averages over the 12 months ending at each one, and the months without
data contribute nothing. A `ROWS BETWEEN 11 PRECEDING` frame would reach back
past the gap. (`RANGE` frames with an offset would say the same, but Dremio
rejects them, so every engine gets the join.)

A cumulative metric is always partitioned by the query's other selected
dimensions as well: `Country, order_month, Revenue YTD` accumulates per
country even without `partitionBy`. Dimensions over the same date column at
another grain (`order_year` next to `order_month`) are positions on the time
axis, not groups, and do not partition. `partitionBy` adds keys on top; a key
the query already selects is not repeated.

### Rolling over a span of time: `windowSpan`

`window: N` combines the per-period values, which is right for a sum and
wrong for a distinct count: three months of monthly active customers summed
count a customer once per month. `windowSpan` evaluates the measure over its
rows in a span of time ending at each period's end instead:

```yaml
metrics:
  Active Customers 90d:
    type: cumulative
    measure: Customer Count        # count_distinct
    timeDimension: order_month
    windowSpan: {count: 90, unit: day}
```

Each shown month joins the measure's rows by their date (abridged, Postgres):

```sql
SELECT k."order_month", COUNT(DISTINCT r."Customer Count") AS "Active Customers 90d"
FROM span_keys_1 AS k
LEFT JOIN span_1_rows AS r
  ON r."order_date" >= k."order_month" + INTERVAL '1 month' - INTERVAL '90 day'
 AND r."order_date" <  k."order_month" + INTERVAL '1 month'
GROUP BY k."order_month"
```

Any aggregation works, `avg`, `median` and `first` / `last` included, and
the span need not be a whole number of periods. Other selected dimensions
give each group its own window, a time filter picks the months shown without
clipping the span, and `cumulativeType`, `window`, `grainToDate` and
`partitionBy` do not apply beside it.

## 2. Window metrics — RANK, LAG, LEAD, NTILE, FIRST/LAST VALUE

A new `MetricType.WINDOW` covers the single-row-output window functions
(ranking, offsetting, positional) and moving aggregates over a frame of
rows. The `windowFunction:` field discriminates.

### Rank within a partition

```yaml
metrics:
  Revenue Rank by Quarter:
    type: window
    windowFunction: dense_rank
    measure: Revenue
    orderDirection: desc
    partitionBy: [Quarter]
```

```sql
DENSE_RANK() OVER (
  PARTITION BY "Quarter"
  ORDER BY "Revenue" DESC
) AS "Revenue Rank by Quarter"
```

### Lag — prior-period value, side by side

```yaml
metrics:
  Revenue Prior Month:
    type: window
    windowFunction: lag
    measure: Revenue
    offset: 1
    timeDimension: order_month
    partitionBy: [Country]
```

```sql
LAG("Revenue", 1) OVER (
  PARTITION BY "Country"
  ORDER BY "order_month"
) AS "Revenue Prior Month"
```

LAG and LEAD accept an optional `defaultValue:` field — emits the
three-argument form when set.

### NTILE — bucket into quartiles / deciles

```yaml
metrics:
  Revenue Quartile:
    type: window
    windowFunction: ntile
    measure: Revenue
    buckets: 4
    partitionBy: [Year]
```

### Moving aggregates over rows: `frame`

`sum`, `avg`, `min`, `max` and `count` aggregate the base measure over a
frame of the result's rows: the current row and the `preceding` rows before
it, ordered by the time dimension.

```yaml
metrics:
  Revenue Moving Avg 3:
    type: window
    windowFunction: avg
    measure: Revenue
    timeDimension: order_month
    frame: {preceding: 2}
```

```sql
AVG("Revenue") OVER (
  PARTITION BY "Country"            -- the query's other dimensions
  ORDER BY "order_month" ASC NULLS LAST
  ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
) AS "Revenue Moving Avg 3"
```

The frame counts rows, not calendar periods: with no orders in February,
March's three rows are December, January and March. A cumulative metric
with `window: 3` counts calendar months instead (January to March), and
`windowSpan` the source rows in a span of time. Like a cumulative metric,
the frame is partitioned by the query's other dimensions and `partitionBy`,
and a dimension over the same date column at another grain orders the rows
rather than partitioning them.

Like every window metric, the frame reads the rows the query returns: a
`where` on the time dimension clips the frame, so the first month shown
averages over itself alone. `timeDimension` is required and must be
selected; `offset`, `buckets`, `defaultValue` and `orderDirection` do not
apply, and the frame is refused with ROLLUP / CUBE (`WINDOW_FRAME_NOT_SUPPORTED`)
and with `fill`.

### Composition — moving-average crossover

The killer feature is composing window metrics through `DERIVED`:

```yaml
metrics:
  Revenue MA3:
    type: cumulative
    measure: Revenue
    timeDimension: order_month
    cumulativeType: avg
    window: 3
    partitionBy: [Country]
  Revenue MA12:
    type: cumulative
    measure: Revenue
    timeDimension: order_month
    cumulativeType: avg
    window: 12
    partitionBy: [Country]
  MA Crossover Signal:
    type: derived
    expression: "CASE WHEN {[Revenue MA3]} > {[Revenue MA12]} THEN 1 ELSE -1 END"
```

No new compiler logic — `DERIVED` already composes any metric by name.

### Validation rules (window)

| Rule | Error code |
|---|---|
| `windowFunction:` required | `INVALID_METRIC` |
| `partitionBy:` entry must resolve to a model dimension in SELECT | `UNKNOWN_PARTITION_DIMENSION` |
| `lag` / `lead` require `offset >= 1` and `timeDimension:` | `INVALID_LAG_LEAD` |
| `ntile` requires `buckets >= 2` | `INVALID_NTILE_BUCKETS` |
| `sum` / `avg` / `min` / `max` / `count` require `frame` and `timeDimension`; `frame` is refused elsewhere | model load error |
| A `frame` with ROLLUP / CUBE | `WINDOW_FRAME_NOT_SUPPORTED` |

## 3. Statistical aggregates on Measure { #statistical-aggregates-on-measure }

Nine new values for `Measure.aggregation`: four single-column and five
two-column. No new metric type.

| Aggregation | SQL | Columns |
|---|---|:---:|
| `stddev`, `stddev_samp` | `STDDEV_SAMP(x)` | 1 |
| `stddev_pop` | `STDDEV_POP(x)` | 1 |
| `variance`, `var_samp` | `VAR_SAMP(x)` | 1 |
| `var_pop` | `VAR_POP(x)` | 1 |
| `corr` | `CORR(x, y)` | 2 |
| `covar_pop` | `COVAR_POP(x, y)` | 2 |
| `covar_samp` | `COVAR_SAMP(x, y)` | 2 |
| `regr_slope` | `REGR_SLOPE(y, x)` | 2 |
| `regr_intercept` | `REGR_INTERCEPT(y, x)` | 2 |

```yaml
measures:
  Daily Revenue StdDev:
    aggregation: stddev
    columns:
      - { dataObject: Orders, column: revenue }

  Revenue Spend Correlation:
    aggregation: corr
    columns:
      - { dataObject: Orders,    column: revenue }
      - { dataObject: Marketing, column: spend }
```

Arity is enforced at model-load time — single-column aggregates with two
columns (or vice-versa) raise `INVALID_AGGREGATION_INPUTS`.

### Dialect coverage

| Aggregation | Postgres | Snowflake | BigQuery | Databricks | DuckDB | ClickHouse | MySQL | Dremio |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| stddev / variance family | yes | yes | yes | yes | yes | yes | yes | yes |
| covar_pop / covar_samp | yes | yes | yes | yes | yes | yes | no | yes |
| corr | yes | yes | yes | yes | yes | yes | no | no |
| regr_slope / regr_intercept | yes | yes | no | yes | yes | no | no | no |

Each answers the SQL standard's statistic on every dialect, which takes some
rendering:

- `stddev` and `variance` are the **sample** statistics. MySQL's own `STDDEV`
  and `VARIANCE` are the population ones, so OBSL emits `STDDEV_SAMP` and
  `VAR_SAMP` there.
- ClickHouse's `varSamp` family subtracts sums of squares, which cancel on
  values far from zero (the variance of 10^9 + 0..9 came back as 0). OBSL emits
  the numerically stable `varSampStable`, `covarSampStable`, `corrStable` and
  so on.
- Where the standard leaves a statistic undefined (a sample statistic of one
  value, a correlation of a constant column, a slope over a constant `x`), the
  answer is NULL. DuckDB, BigQuery and ClickHouse answer `NaN` or `inf` there,
  and Databricks fails the query with `DIVIDE_BY_ZERO` when a `corr` sits beside
  a covariance or regression, so OBSL guards each.
- Dremio cannot parse `CORR` or `REGR_*`, so they are refused at compile time.

Precision: Snowflake (over doubles, and its covariance and correlation over
integers) and Dremio (over every type) also subtract sums of squares and have
no stable function, so on values far from zero, relative to their spread,
their result loses most or all of its digits.

Unsupported combinations raise `UNSUPPORTED_AGGREGATION_FOR_DIALECT` at
compile time. There is no silent fallback. If you need regression on
BigQuery / ClickHouse / MySQL, write a `DERIVED` metric with the
two-aggregate identity:

```yaml
metrics:
  Revenue Slope vs Spend:
    type: derived
    expression: "{[Covariance]} / {[Variance Spend]}"
```

## 4. Worked example — multi-region trend dashboard

```yaml
dimensions:
  Country: { dataObject: Orders, column: country, resultType: string }
  Quarter: { dataObject: Orders, column: order_date, resultType: date, timeGrain: quarter }

measures:
  Revenue:
    aggregation: sum
    columns: [{ dataObject: Orders, column: revenue }]

metrics:
  Revenue MA4 by Country:
    type: cumulative
    measure: Revenue
    timeDimension: Quarter
    cumulativeType: avg
    window: 4
    partitionBy: [Country]

  Revenue Rank by Quarter:
    type: window
    windowFunction: dense_rank
    measure: Revenue
    orderDirection: desc
    partitionBy: [Quarter]

  Revenue Prior Quarter:
    type: window
    windowFunction: lag
    measure: Revenue
    offset: 1
    timeDimension: Quarter
    partitionBy: [Country]

  QoQ Delta:
    type: derived
    expression: "{[Revenue]} - {[Revenue Prior Quarter]}"
```

One model, four trend primitives, every dialect — no SQL hand-written.

## Out of scope (deliberate)

- **Fiscal calendars** (4-4-5, custom year start) — tracked separately;
  touches PoP, cumulative, and date dimension grain everywhere.
- **Forecasting / extrapolation** (linear regression beyond the
  `regr_*` aggregates, ARIMA, exponential smoothing) — belongs in the
  BI tool or downstream analytics layer.
- **Semi-additive measures** (the total of period-end balances across accounts, period-average) are no separate primitive: a [`last` measure](model-format.md#first-and-last) reaggregated with `sum` per account. With `population: dimension` and `carryForward: true`, an account with no snapshot in a period carries its earlier balance. See [Semi-additive measures](model-format.md#semi-additive-measures).
