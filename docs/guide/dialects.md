# SQL Dialects

OrionBelt compiles semantic queries into SQL for eight database dialects. Each dialect has its own identifier quoting, function names, and SQL syntax. The plugin architecture allows adding new dialects without modifying the core compiler.

## Supported Dialects

| Dialect | Identifier | Description |
|---------|-----------|-------------|
| BigQuery | `bigquery` | Google Cloud analytics warehouse with QUALIFY, STRUCT/ARRAY, semi-structured types |
| ClickHouse | `clickhouse` | Column-oriented OLAP with custom date/aggregation functions |
| Databricks SQL | `databricks` | Spark SQL semantics with backtick identifiers |
| Dremio | `dremio` | Data lakehouse with reduced function surface |
| DuckDB / MotherDuck | `duckdb` | Embedded analytics engine with PostgreSQL-like syntax, QUALIFY, UNION ALL BY NAME |
| MySQL | `mysql` | MySQL 8.0+ with backtick identifiers, DATE_FORMAT time grains, GROUP_CONCAT |
| PostgreSQL | `postgres` | Standard PostgreSQL with strict GROUP BY |
| Snowflake | `snowflake` | Cloud data warehouse with QUALIFY, semi-structured types |

### Connecting to MotherDuck

MotherDuck is DuckDB served remotely, so it needs no separate dialect — the
`duckdb` codegen is what runs, and every DuckDB capability below applies
unchanged. What differs is only the connection:

```bash
DUCKDB_DATABASE=md:my_database
MOTHERDUCK_ACCESS_TOKEN=<token>
```

Create the token at [app.motherduck.com](https://app.motherduck.com) under
your organization name → **Settings** → **Create token**. A **Read Scaling**
token is the better fit than the default Read/Write one: OrionBelt only ever
issues `SELECT`, and read-scaling tokens are built for concurrent readers.

The lowercase `motherduck_token` that MotherDuck's own CLI exports is accepted
as an alias, so an environment already set up for the CLI needs no changes.
`MOTHERDUCK_ACCESS_TOKEN` is the canonical spelling, matching the other vendor
credentials.

!!! warning "A `md:` database without a token is refused"

    OrionBelt raises `MotherDuckTokenMissingError` rather than connecting.
    This is deliberate. Without an explicit token the DuckDB extension falls
    back to **interactive browser authentication**, which on a server does not
    fail — it *hangs*, so it surfaces as a stuck worker rather than a
    configuration error. Refusing up front also prevents an ambient
    `motherduck_token` in the environment from silently connecting to a
    different account than the one you configured.

`read_only` stays enabled for MotherDuck as it is for a local file: OrionBelt
only reads, and a `md:` connection accepts it.

## Capabilities Matrix

Each dialect declares capability flags that the compiler uses to choose SQL generation strategies.

| Capability | BigQuery | ClickHouse | Databricks | Dremio | DuckDB | MySQL | Postgres | Snowflake |
|-----------|----------|------------|------------|--------|--------|-------|----------|-----------|
| `supports_cte` | Yes | Yes | Yes | Yes | Yes | Yes | Yes | Yes |
| `supports_qualify` | Yes | No | No | No | Yes | No | No | Yes |
| `supports_arrays` | Yes | Yes | Yes | No | Yes | No | Yes | Yes |
| `supports_window_filters` | Yes | No | No | No | Yes | No | No | Yes |
| `supports_ilike` | No | Yes | No | No | Yes | No | Yes | Yes |
| `supports_time_travel` | No | No | No | No | No | No | No | Yes |
| `supports_semi_structured` | Yes | No | No | No | No | No | No | Yes |
| `supports_union_all_by_name` | No | No | No | No | Yes | No | No | Yes |
| `supports_group_by_all` | Yes | Yes | Yes | No | Yes | No | No | Yes |

## Identifier Quoting

| Dialect | Style | Example |
|---------|-------|---------|
| BigQuery | Backticks | `` `column_name` `` |
| ClickHouse | Double quotes | `"column_name"` |
| Databricks | Backticks | `` `column_name` `` |
| Dremio | Double quotes | `"column_name"` |
| DuckDB | Double quotes | `"column_name"` |
| MySQL | Backticks | `` `column_name` `` |
| Postgres | Double quotes | `"column_name"` |
| Snowflake | Double quotes | `"column_name"` |

## Time Grain Functions

The `timeGrain` is rendered differently per dialect:

=== "BigQuery"

    ```sql
    DATE_TRUNC(`order_date`, 'month')
    DATE_TRUNC(`order_date`, 'year')
    DATE_TRUNC(`order_date`, 'quarter')
    DATE_TRUNC(`order_date`, 'ISOWEEK')   -- week
    ```

=== "ClickHouse"

    ```sql
    toStartOfMonth("order_date")
    toStartOfYear("order_date")
    toStartOfQuarter("order_date")
    toMonday("order_date")        -- week
    toDate("order_date")          -- day
    toStartOfHour("order_date")
    toStartOfMinute("order_date")
    toStartOfSecond("order_date")
    ```

=== "Databricks"

    ```sql
    date_trunc('month', `order_date`)
    date_trunc('year', `order_date`)
    ```

=== "Dremio"

    ```sql
    DATE_TRUNC('month', "order_date")
    DATE_TRUNC('year', "order_date")
    ```

=== "DuckDB"

    ```sql
    date_trunc('month', "order_date")
    date_trunc('year', "order_date")
    date_trunc('quarter', "order_date")
    ```

=== "MySQL"

    ```sql
    DATE_FORMAT(`order_date`, '%Y-%m-01')           -- month
    DATE_FORMAT(`order_date`, '%Y-01-01')           -- year
    DATE_ADD(MAKEDATE(YEAR(`order_date`), 1),
      INTERVAL (QUARTER(`order_date`) - 1) * 3 MONTH)  -- quarter
    DATE_FORMAT(`order_date`, '%Y-%u')              -- week (ISO)
    DATE_FORMAT(`order_date`, '%Y-%m-%d')           -- day
    ```

=== "Postgres"

    ```sql
    date_trunc('month', "order_date")
    date_trunc('year', "order_date")
    date_trunc('quarter', "order_date")
    ```

=== "Snowflake"

    ```sql
    DATE_TRUNC('month', "order_date")
    DATE_TRUNC('year', "order_date")
    DATE_TRUNC('quarter', "order_date")
    ```

## String Contains

The `contains` filter operator is rendered per dialect:

=== "BigQuery"

    ```sql
    LOWER(`column`) LIKE '%' || LOWER('search') || '%'
    ```

=== "ClickHouse"

    ```sql
    "column" ILIKE '%' || 'search' || '%'
    ```

=== "Databricks"

    ```sql
    lower(`column`) LIKE '%' || lower('search') || '%'
    ```

=== "Dremio"

    ```sql
    LOWER("column") LIKE '%' || LOWER('search') || '%'
    ```

=== "DuckDB"

    ```sql
    "column" ILIKE '%' || 'search' || '%'
    ```

=== "MySQL"

    ```sql
    `column` LIKE CONCAT('%', 'search', '%')
    ```

    MySQL string comparisons are case-insensitive by default with `utf8mb4_general_ci` collation, so `LIKE` is sufficient (no `ILIKE` needed).

=== "Postgres"

    ```sql
    "column" ILIKE '%' || 'search' || '%'
    ```

=== "Snowflake"

    ```sql
    CONTAINS("column", 'search')
    ```

## CAST Handling

OrionBelt automatically wraps aggregate expressions with `CAST` based on resolved OBML data types. Each dialect maps OBML types to its native SQL types:

| OBML Type | Postgres | Snowflake | ClickHouse | BigQuery | MySQL | Databricks | DuckDB | Dremio |
|-----------|----------|-----------|------------|----------|-------|------------|--------|--------|
| `decimal(p, s)` | `NUMERIC(p, s)` | `NUMBER(p, s)` | `Decimal(p, s)` | `NUMERIC(p, s)` | `DECIMAL(p, s)` | `DECIMAL(p, s)` | `DECIMAL(p, s)` | `DECIMAL(p, s)` |
| `bigint` | `BIGINT` | `NUMBER(38, 0)` | `Int64` | `INT64` | `BIGINT` | `BIGINT` | `BIGINT` | `BIGINT` |
| `integer` | `INTEGER` | `NUMBER(38, 0)` | `Int32` | `INT64` | `INTEGER` | `INT` | `INTEGER` | `INTEGER` |
| `double` | `DOUBLE PRECISION` | `FLOAT` | `Float64` | `FLOAT64` | `DOUBLE` | `DOUBLE` | `DOUBLE` | `DOUBLE` |
| `string` | `TEXT` | `VARCHAR` | `String` | `STRING` | `VARCHAR(65535)` | `STRING` | `VARCHAR` | `VARCHAR` |
| `boolean` | `BOOLEAN` | `BOOLEAN` | `Bool` | `BOOL` | `TINYINT(1)` | `BOOLEAN` | `BOOLEAN` | `BOOLEAN` |

**Maximum decimal precision** (values are clamped):

| Dialect | Max Precision |
|---------|---------------|
| Postgres | 131,072 |
| ClickHouse | 76 |
| MySQL | 65 |
| Snowflake, DuckDB, Databricks, Dremio | 38 |
| BigQuery | 38 |

!!! warning "ClickHouse: decimal division keeps the operand's scale"

    Dividing two decimals on ClickHouse produces a result at the *operands'*
    scale rather than widening it, so a ratio built from `decimal(18, 2)`
    columns is truncated to two places:

    ```sql
    SELECT toDecimal64(4254, 2) / toDecimal64(10000, 2)   -- 0.42
    SELECT toDecimal64(4254, 4) / toDecimal64(10000, 4)   -- 0.4254
    ```

    Every other supported dialect widens the scale for you. If a metric divides
    decimal measures and you need the precision, declare the metric's
    `dataType` with the scale you want, or give the measures a `float`
    `resultType` so the division happens in floating point. This is a
    ClickHouse arithmetic rule, not something OrionBelt applies -- it is
    documented here because a ratio that reads `0.42` on one engine and
    `0.4254` on another looks like a compiler bug and is not.

=== "BigQuery / Databricks / Dremio / DuckDB / MySQL / Postgres / Snowflake"

    ```sql
    CAST(expr AS INTEGER)
    CAST(expr AS VARCHAR)
    CAST(expr AS DATE)
    ```

    BigQuery uses its own type names (`INT64`, `FLOAT64`, `STRING`, `BOOL`) but standard `CAST` syntax.

=== "ClickHouse"

    ClickHouse uses native conversion functions:

    ```sql
    toInt64(expr)      -- int / integer
    toFloat64(expr)    -- float / double
    toString(expr)     -- string / varchar
    toDate(expr)       -- date
    -- Other types fall back to CAST
    CAST(expr AS DateTime)
    ```

## Aggregation Functions

Most aggregations (`SUM`, `COUNT`, `AVG`, `MIN`, `MAX`) compile identically across dialects. The following aggregations require dialect-specific rendering:

### ANY_VALUE

| Dialect | SQL |
|---------|-----|
| BigQuery | `ANY_VALUE(col)` |
| ClickHouse | `any(col)` |
| Databricks | `ANY_VALUE(col)` |
| Dremio | `ANY_VALUE(col)` |
| DuckDB | `ANY_VALUE(col)` |
| MySQL | `ANY_VALUE(col)` |
| Postgres | `ANY_VALUE(col)` |
| Snowflake | `ANY_VALUE(col)` |

### MEDIAN

| Dialect | SQL |
|---------|-----|
| BigQuery | `APPROX_QUANTILES(col, 2)[OFFSET(1)]` |
| ClickHouse | `MEDIAN(col)` |
| Databricks | `MEDIAN(col)` |
| Dremio | `MEDIAN(col)` |
| DuckDB | `MEDIAN(col)` |
| MySQL | `MAX(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY col))` |
| Postgres | `PERCENTILE_DISC(0.5) WITHIN GROUP (ORDER BY col)` |
| Snowflake | `MEDIAN(col)` |

### MODE

| Dialect | SQL |
|---------|-----|
| BigQuery | `APPROX_TOP_COUNT(col, 1)[OFFSET(0)].value` |
| ClickHouse | `topK(1)(col)[1]` |
| Databricks | `MODE(col)` |
| Dremio | Not supported |
| DuckDB | `MODE(col)` |
| MySQL | Not supported |
| Postgres | `MODE() WITHIN GROUP (ORDER BY col)` |
| Snowflake | `MODE(col)` |

### LISTAGG

| Dialect | Base | + DISTINCT | + ORDER BY |
|---------|------|------------|------------|
| BigQuery | `STRING_AGG(col, sep)` | `STRING_AGG(DISTINCT col, sep)` | `STRING_AGG(col, sep ORDER BY col)` |
| ClickHouse | `arrayStringConcat(groupArray(col), sep)` | `arrayStringConcat(groupUniqArray(col), sep)` | `arrayStringConcat(arraySort(groupArray(col)), sep)` |
| Databricks | `ARRAY_JOIN(COLLECT_LIST(col), sep)` | `ARRAY_JOIN(COLLECT_SET(col), sep)` | `ARRAY_JOIN(SORT_ARRAY(COLLECT_LIST(col)), sep)` |
| Dremio | `LISTAGG(col, sep)` | `LISTAGG(DISTINCT col, sep)` | `LISTAGG(col, sep) WITHIN GROUP (ORDER BY col)` |
| DuckDB | `STRING_AGG(col, sep)` | `STRING_AGG(DISTINCT col, sep)` | `STRING_AGG(col, sep ORDER BY col)` |
| MySQL | `GROUP_CONCAT(col SEPARATOR sep)` | `GROUP_CONCAT(DISTINCT col SEPARATOR sep)` | `GROUP_CONCAT(col ORDER BY col SEPARATOR sep)` |
| Postgres | `STRING_AGG(col, sep)` | `STRING_AGG(DISTINCT col, sep)` | `STRING_AGG(col, sep ORDER BY col)` |
| Snowflake | `LISTAGG(col, sep)` | `LISTAGG(DISTINCT col, sep)` | `LISTAGG(col, sep) WITHIN GROUP (ORDER BY col)` |

!!! warning "LISTAGG ordering limitations"
    ClickHouse and Databricks only support self-ordering (sorting by the aggregated column). Ordering by a different column raises an error at compile time.

!!! warning "MySQL GROUP_CONCAT limitations"
    MySQL's `GROUP_CONCAT` has a default length limit of 1024 bytes (`group_concat_max_len`). For large aggregations, users may need to increase this: `SET SESSION group_concat_max_len = 1000000`. Additionally, MySQL silently ignores `ORDER BY` when `DISTINCT` is also present in `GROUP_CONCAT`.

!!! warning "Total not supported"
    `MEDIAN`, `MODE`, `LISTAGG`, and `ANY_VALUE` do not support `total: true` because they cannot be meaningfully re-aggregated via window functions.

## Dialect Plugin Architecture

Each dialect implements the abstract `Dialect` base class:

```python
class Dialect(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> DialectCapabilities: ...

    @abstractmethod
    def quote_identifier(self, name: str) -> str: ...

    @abstractmethod
    def render_time_grain(self, column: Expr, grain: TimeGrain) -> Expr: ...

    @abstractmethod
    def render_cast(self, expr: Expr, target_type: str) -> Expr: ...

    def render_string_contains(self, column: Expr, pattern: Expr) -> Expr: ...

    def compile(self, ast: Select) -> str: ...
```

Dialects register themselves via the `@DialectRegistry.register` decorator:

```python
@DialectRegistry.register
class PostgresDialect(Dialect):
    @property
    def name(self) -> str:
        return "postgres"
    ...
```

The registry provides lookup by name:

```python
from orionbelt.dialect.registry import DialectRegistry

dialect = DialectRegistry.get("snowflake")
sql = dialect.compile(ast)
```

### Adding a New Dialect

1. Create `src/orionbelt/dialect/my_dialect.py`
2. Subclass `Dialect` and implement all abstract methods
3. Decorate with `@DialectRegistry.register`
4. The dialect is automatically available via `DialectRegistry.get("my_dialect")`

## Querying Dialect Info via API

## Date Spine Generation

Period-over-period metrics require generating a date series (spine). Each dialect uses a different technique:

| Dialect | Technique |
|---------|-----------|
| Postgres | `generate_series(min, max, INTERVAL)` |
| DuckDB | `generate_series(min, max, INTERVAL)` |
| Snowflake | `TABLE(GENERATOR(ROWCOUNT => ...))` + `DATEADD` |
| BigQuery | `UNNEST(GENERATE_DATE_ARRAY(min, max, INTERVAL))` |
| Databricks | `EXPLODE(SEQUENCE(min, max, INTERVAL))` |
| MySQL | Recursive CTE: `WITH RECURSIVE dates AS (...)` |
| ClickHouse | `arrayJoin(range(...))` + date arithmetic |
| Dremio | Recursive CTE: `WITH RECURSIVE dates AS (...)` |

For details, see the [Period-over-Period Metrics](period-over-period.md) guide.

## Querying Available Dialects

```bash
curl http://127.0.0.1:8000/v1/dialects
```

```json
{
  "dialects": [
    {
      "name": "bigquery",
      "capabilities": {
        "supports_cte": true,
        "supports_qualify": true,
        "supports_arrays": true,
        "supports_window_filters": true,
        "supports_ilike": false,
        "supports_time_travel": false,
        "supports_semi_structured": true,
        "supports_union_all_by_name": false,
        "supports_group_by_all": true
      },
      "supported_aggregations": [
        "any_value", "avg", "corr", "count", "count_distinct", "covar_pop",
        "covar_samp", "listagg", "max", "median", "min", "mode", "stddev",
        "stddev_pop", "sum", "var_pop", "variance"
      ],
      "supported_functions": [
        "abs", "ceil", "coalesce", "concat", "div", "ends_with", "exp", "floor",
        "greatest", "least", "length", "ln", "log", "lower", "lpad", "ltrim",
        "mod", "nullif", "position", "power", "replace", "round", "rpad",
        "rtrim", "sign", "split_part", "sqrt", "starts_with", "substring",
        "trim", "trunc", "upper"
      ]
    },
    ...
  ]
}
```

`capabilities` are structural SQL features. The two lists are the vocabulary: every OBML
`aggregation:` value this dialect can compute, and every entry of the
[portable function catalog](functions.md) it can render.
Both are stated positively, so answering "may I use `median` on this warehouse?" needs no second
call. Anything absent is refused at compile time with a 422 rather than emitted and failed at the
database — MySQL, for instance, lists neither `median` nor `mode`.
