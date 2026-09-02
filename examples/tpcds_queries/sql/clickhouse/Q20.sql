-- Q20 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

WITH "base" AS (
  SELECT
    "Item"."i_item_id" AS "Item ID",
    "Item"."i_item_desc" AS "Item Description",
    "Item"."i_category" AS "Category",
    "Item"."i_class" AS "Class",
    "Item"."i_current_price" AS "Current Price",
    CAST(round(toDecimal256(toString(SUM("Catalog Sales"."cs_ext_sales_price")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Catalog Sales Amount",
    SUM("Catalog Sales"."cs_ext_sales_price") AS "Catalog Class Revenue"
  FROM "tpcds"."catalog_sales" AS "Catalog Sales"
  LEFT JOIN "tpcds"."item" AS "Item"
    ON "Catalog Sales"."cs_item_sk" = "Item"."i_item_sk"
  LEFT JOIN "tpcds"."date_dim" AS "Date"
    ON "Catalog Sales"."cs_sold_date_sk" = "Date"."d_date_sk"
  WHERE
    "Item"."i_category" IN ('Sports', 'Books', 'Home')
    AND "Date"."d_date" BETWEEN '1999-02-22' AND '1999-03-24'
  GROUP BY ALL
)
SELECT
  "Item ID" AS "Item ID",
  "Item Description" AS "Item Description",
  "Category" AS "Category",
  "Class" AS "Class",
  "Current Price" AS "Current Price",
  "Catalog Sales Amount" AS "Catalog Sales Amount",
  CAST("Catalog Sales Amount" * 100.0 AS Nullable(Decimal(38, 14))) / nullIf(
    CAST(SUM("Catalog Class Revenue") OVER (PARTITION BY "Class") AS Nullable(Decimal(38, 14))),
    0
  ) AS "Catalog Revenue Ratio"
FROM "base" AS "base"
ORDER BY
  "Category" ASC,
  "Class" ASC,
  "Item ID" ASC,
  "Item Description" ASC,
  "Catalog Revenue Ratio" ASC
LIMIT 100
