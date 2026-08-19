-- Q98 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

WITH "base" AS (
  SELECT
    "Item"."i_item_id" AS "Item ID",
    "Item"."i_item_desc" AS "Item Description",
    "Item"."i_category" AS "Category",
    "Item"."i_class" AS "Class",
    "Item"."i_current_price" AS "Current Price",
    CAST(SUM("Store Sales"."ss_ext_sales_price") AS DECIMAL(18, 2)) AS "Store Sales Amount",
    SUM("Store Sales"."ss_ext_sales_price") AS "Class Revenue"
  FROM "main"."store_sales" AS "Store Sales"
  LEFT JOIN "main"."item" AS "Item"
    ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
  LEFT JOIN "main"."date_dim" AS "Date"
    ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
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
  "Store Sales Amount" AS "Store Sales Amount",
  "Store Sales Amount" * 100.0 / NULLIF(SUM("Class Revenue") OVER (PARTITION BY "Class"), 0) AS "Revenue Ratio"
FROM "base" AS "base"
ORDER BY
  "Category" ASC,
  "Class" ASC,
  "Item ID" ASC,
  "Item Description" ASC,
  "Revenue Ratio" ASC
