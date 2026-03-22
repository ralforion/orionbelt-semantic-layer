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
        "supports_semi_structured": true
      }
    },
    ...
  ]
}
```
