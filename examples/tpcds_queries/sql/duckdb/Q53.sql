-- Q53 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

WITH "base" AS (
  SELECT
    "Item"."i_manufact_id" AS "Manufacturer ID",
    "Date"."d_qoy" AS "Quarter of Year",
    CAST(SUM("Store Sales"."ss_sales_price") AS DECIMAL(18, 2)) AS "Sales Price Sum",
    SUM("Store Sales"."ss_sales_price") AS "Manufacturer Sales",
    COUNT(DISTINCT "Date"."d_qoy") AS "Manufacturer Quarter Groups"
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
  "Manufacturer ID" AS "Manufacturer ID",
  "Quarter of Year" AS "Quarter of Year",
  "Sales Price Sum" AS "Sales Price Sum",
  SUM("Manufacturer Sales") OVER (PARTITION BY "Manufacturer ID") / SUM("Manufacturer Quarter Groups") OVER (PARTITION BY "Manufacturer ID") AS "Avg Quarterly Sales"
FROM "base" AS "base"
