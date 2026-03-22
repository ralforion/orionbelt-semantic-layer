# Multi-Dialect Output

This example shows how the same semantic model and query produce different SQL for each of the eight supported dialects.

## The Model

Using the [Sales Model](sales-model.md) with Customers, Products, and Orders data objects.

## The Query

Revenue by customer country, filtered to SMB/MidMarket segments, ordered by revenue descending, limited to 1000 rows.

```python
from orionbelt.models.query import *

query = QueryObject(
    select=QuerySelect(
        dimensions=["Customer Country"],
        measures=["Revenue"],
    ),
    where=[
        QueryFilter(field="Customer Segment", op=FilterOperator.IN, value=["SMB", "MidMarket"]),
    ],
    order_by=[QueryOrderBy(field="Revenue", direction=SortDirection.DESC)],
    limit=1000,
)
```

## Generated SQL by Dialect

=== "BigQuery"

    ```sql
    SELECT
      `Customers`.`COUNTRY` AS `Customer Country`,
      SUM(`Orders`.`PRICE` * `Orders`.`QUANTITY`) AS `Revenue`
    FROM WAREHOUSE.PUBLIC.ORDERS AS `Orders`
    LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS `Customers`
      ON `Orders`.`CUSTOMER_ID` = `Customers`.`CUSTOMER_ID`
    WHERE (`Customers`.`SEGMENT` IN ('SMB', 'MidMarket'))
    GROUP BY `Customers`.`COUNTRY`
    ORDER BY `Revenue` DESC
    LIMIT 1000
    ```

    **Key traits:** Backtick-quoted identifiers, `DATE_TRUNC()` with column-first argument, `LOWER()` + `LIKE` for string matching, supports `QUALIFY` and semi-structured types.

=== "ClickHouse"

    ```sql
    SELECT
      "Customers"."COUNTRY" AS "Customer Country",
      SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
    FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
    LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
      ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
    WHERE ("Customers"."SEGMENT" IN ('SMB', 'MidMarket'))
    GROUP BY "Customers"."COUNTRY"
    ORDER BY "Revenue" DESC
    LIMIT 1000
    ```

    **Key traits:** Double-quoted identifiers, custom time functions (`toStartOfMonth()`, `toStartOfYear()`), native type conversion (`toInt64()`, `toFloat64()`).

=== "Databricks"

    ```sql
    SELECT
      `Customers`.`COUNTRY` AS `Customer Country`,
      SUM(`Orders`.`PRICE` * `Orders`.`QUANTITY`) AS `Revenue`
    FROM WAREHOUSE.PUBLIC.ORDERS AS `Orders`
    LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS `Customers`
      ON `Orders`.`CUSTOMER_ID` = `Customers`.`CUSTOMER_ID`
    WHERE (`Customers`.`SEGMENT` IN ('SMB', 'MidMarket'))
    GROUP BY `Customers`.`COUNTRY`
    ORDER BY `Revenue` DESC
    LIMIT 1000
    ```

    **Key traits:** Backtick-quoted identifiers (Spark SQL), `date_trunc()` for time grains, `lower()` + `LIKE` for case-insensitive matching.

=== "Dremio"

    ```sql
    SELECT
      "Customers"."COUNTRY" AS "Customer Country",
      SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
    FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
    LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
      ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
    WHERE ("Customers"."SEGMENT" IN ('SMB', 'MidMarket'))
    GROUP BY "Customers"."COUNTRY"
    ORDER BY "Revenue" DESC
    LIMIT 1000
    ```

    **Key traits:** Double-quoted identifiers, `DATE_TRUNC()`, no `ILIKE` support (uses `LOWER()` + `LIKE` workaround), minimal capability set.

=== "DuckDB"

    ```sql
    SELECT
      "Customers"."COUNTRY" AS "Customer Country",
      SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
    FROM PUBLIC.ORDERS AS "Orders"
    LEFT JOIN PUBLIC.CUSTOMERS AS "Customers"
      ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
    WHERE ("Customers"."SEGMENT" IN ('SMB', 'MidMarket'))
    GROUP BY "Customers"."COUNTRY"
    ORDER BY "Revenue" DESC
    LIMIT 1000
    ```

    **Key traits:** Double-quoted identifiers (PostgreSQL-compatible), two-part table refs, `date_trunc()`, `ILIKE`, `UNION ALL BY NAME`.

=== "PostgreSQL"

    ```sql
    SELECT
      "Customers"."COUNTRY" AS "Customer Country",
      SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
    FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
    LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
      ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
    WHERE ("Customers"."SEGMENT" IN ('SMB', 'MidMarket'))
    GROUP BY "Customers"."COUNTRY"
    ORDER BY "Revenue" DESC
    LIMIT 1000
    ```

    **Key traits:** Double-quoted identifiers, `date_trunc()` for time grains, `ILIKE` for case-insensitive matching.

=== "Snowflake"

    ```sql
    SELECT
      "Customers"."COUNTRY" AS "Customer Country",
      SUM("Orders"."PRICE" * "Orders"."QUANTITY") AS "Revenue"
    FROM WAREHOUSE.PUBLIC.ORDERS AS "Orders"
    LEFT JOIN WAREHOUSE.PUBLIC.CUSTOMERS AS "Customers"
      ON "Orders"."CUSTOMER_ID" = "Customers"."CUSTOMER_ID"
    WHERE ("Customers"."SEGMENT" IN ('SMB', 'MidMarket'))
    GROUP BY "Customers"."COUNTRY"
    ORDER BY "Revenue" DESC
    LIMIT 1000
    ```

    **Key traits:** Double-quoted identifiers (case-sensitive), `DATE_TRUNC()` (uppercase), `CONTAINS()` for string matching, supports `QUALIFY` and window filters.

## Key Differences

### Identifier Quoting

| Dialect | Style | Example |
|---------|-------|---------|
| ClickHouse, Dremio, DuckDB, Postgres, Snowflake | Double quotes | `"column"` |
| BigQuery, Databricks | Backticks | `` `column` `` |

### Time Grain: Monthly Aggregation

If the query included `"Order Date:month"` as a dimension:

=== "BigQuery"

    ```sql
    DATE_TRUNC(`Orders`.`ORDER_DATE`, 'month') AS `Order Date`
    ```

=== "ClickHouse"

    ```sql
    toStartOfMonth("Orders"."ORDER_DATE") AS "Order Date"
    ```

=== "Databricks"

    ```sql
    date_trunc('month', `Orders`.`ORDER_DATE`) AS `Order Date`
    ```

=== "Dremio"

    ```sql
    DATE_TRUNC('month', "Orders"."ORDER_DATE") AS "Order Date"
    ```

=== "DuckDB"

    ```sql
    date_trunc('month', "Orders"."ORDER_DATE") AS "Order Date"
    ```

=== "Postgres"

    ```sql
    date_trunc('month', "Orders"."ORDER_DATE") AS "Order Date"
    ```

=== "Snowflake"

    ```sql
    DATE_TRUNC('month', "Orders"."ORDER_DATE") AS "Order Date"
    ```

### String Contains Filter

If the query filtered with `{ field: "Customer Country", op: "contains", value: "United" }`:

=== "BigQuery"

    ```sql
    LOWER(`Customers`.`COUNTRY`) LIKE '%' || LOWER('United') || '%'
    ```

=== "ClickHouse"

    ```sql
    "Customers"."COUNTRY" ILIKE '%' || 'United' || '%'
    ```

=== "Databricks"

    ```sql
    lower(`Customers`.`COUNTRY`) LIKE '%' || lower('United') || '%'
    ```

=== "Dremio"

    ```sql
    LOWER("Customers"."COUNTRY") LIKE '%' || LOWER('United') || '%'
    ```

=== "DuckDB"

    ```sql
    "Customers"."COUNTRY" ILIKE '%' || 'United' || '%'
    ```

=== "Postgres"

    ```sql
    "Customers"."COUNTRY" ILIKE '%' || 'United' || '%'
    ```

=== "Snowflake"

    ```sql
    CONTAINS("Customers"."COUNTRY", 'United')
    ```

## Compiling for All Dialects

```python
from orionbelt.compiler.pipeline import CompilationPipeline

pipeline = CompilationPipeline()

for dialect in ["bigquery", "clickhouse", "databricks", "dremio", "duckdb", "postgres", "snowflake"]:
    result = pipeline.compile(query, model, dialect)
    print(f"--- {dialect} ---")
    print(result.sql)
    print()
```
