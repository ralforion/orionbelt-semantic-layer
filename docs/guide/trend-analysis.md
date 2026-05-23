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

Compiles to:

```sql
AVG("Revenue") OVER (
  PARTITION BY "Country"
  ORDER BY "order_month"
  ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
) AS "Revenue MA12 by Country"
```

Default is `[]` (no partition) — existing cumulative metrics produce
identical SQL.

## 2. Window metrics — RANK, LAG, LEAD, NTILE, FIRST/LAST VALUE

A new `MetricType.WINDOW` covers the single-row-output window functions
(ranking, offsetting, positional). The `windowFunction:` field
discriminates.

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

### Composition — moving-average crossover

The killer feature is composing window metrics through `DERIVED`:

```yaml
metrics:
  - { label: Revenue MA3,  type: cumulative, measure: Revenue, timeDimension: order_month, cumulativeType: avg, window: 3,  partitionBy: [Country] }
  - { label: Revenue MA12, type: cumulative, measure: Revenue, timeDimension: order_month, cumulativeType: avg, window: 12, partitionBy: [Country] }
  - label: MA Crossover Signal
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

## 3. Statistical aggregates on Measure

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
| corr / covariance | yes | yes | yes | yes | yes | yes | no | yes |
| regr_slope / regr_intercept | yes | yes | no | yes | yes | no | no | yes |

ClickHouse uses camelCase function names internally (`stddevPop`,
`covarSamp`, etc.) — OBSL maps automatically.

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
- **Semi-additive measures** (period-end balances, period-average) —
  separate primitive; planned independently.
