-- Q63 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

WITH "base" AS (
  SELECT
    "Item"."i_manager_id" AS "Manager ID",
    "Date"."d_moy" AS "Month of Year",
    CAST(SUM("Store Sales"."ss_sales_price") AS DECIMAL(18, 2)) AS "Sales Price Sum",
    SUM("Store Sales"."ss_sales_price") AS "Manager Sales",
    COUNT(DISTINCT "Date"."d_moy") AS "Manager Month Groups",
    SUM("Store Sales"."ss_sales_price") AS "Manager Sales",
    COUNT(DISTINCT "Date"."d_moy") AS "Manager Month Groups"
  FROM "main"."store_sales" AS "Store Sales"
  LEFT JOIN "main"."date_dim" AS "Date"
    ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "main"."item" AS "Item"
    ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
  WHERE
    "Date"."d_month_seq" BETWEEN 1200 AND 1211
    AND NOT "Store Sales"."ss_store_sk" IS NULL
    AND (
      "Item"."i_category" IN ('Books', 'Children', 'Electronics')
      AND "Item"."i_class" IN ('personal', 'portable', 'reference', 'self-help')
      AND "Item"."i_brand" IN (
        'scholaramalgamalg #14',
        'scholaramalgamalg #7',
        'exportiunivamalg #9',
        'scholaramalgamalg #9'
      )
      OR "Item"."i_category" IN ('Women', 'Music', 'Men')
      AND "Item"."i_class" IN ('accessories', 'classical', 'fragrances', 'pants')
      AND "Item"."i_brand" IN ('amalgimporto #1', 'edu packscholar #1', 'exportiimporto #1', 'importoamalg #1')
    )
  GROUP BY ALL
)
SELECT
  "Manager ID" AS "Manager ID",
  "Month of Year" AS "Month of Year",
  "Sales Price Sum" AS "Sales Price Sum",
  SUM("Manager Sales") OVER (PARTITION BY "Manager ID") / SUM("Manager Month Groups") OVER (PARTITION BY "Manager ID") AS "Avg Monthly Sales",
  ABS(
    "Sales Price Sum" - SUM("Manager Sales") OVER (PARTITION BY "Manager ID") / SUM("Manager Month Groups") OVER (PARTITION BY "Manager ID")
  ) / (
    SUM("Manager Sales") OVER (PARTITION BY "Manager ID") / SUM("Manager Month Groups") OVER (PARTITION BY "Manager ID")
  ) AS "Monthly Variance"
FROM "base" AS "base"
