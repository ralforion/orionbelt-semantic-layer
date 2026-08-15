-- Q13 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  CAST(AVG("Store Sales"."ss_quantity") AS DECIMAL(18, 6)) AS "Avg Quantity",
  CAST(AVG("Store Sales"."ss_ext_sales_price") AS DECIMAL(18, 6)) AS "Avg Ext Sales Price",
  CAST(AVG("Store Sales"."ss_ext_wholesale_cost") AS DECIMAL(18, 6)) AS "Avg Ext Wholesale Cost",
  CAST(SUM("Store Sales"."ss_ext_wholesale_cost") AS DECIMAL(18, 2)) AS "Ext Wholesale Cost Sum"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "main"."customer_demographics" AS "Customer Demographics"
  ON "Store Sales"."ss_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "main"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "main"."customer_address" AS "Customer Address"
  ON "Store Sales"."ss_addr_sk" = "Customer Address"."ca_address_sk"
WHERE
  "Date"."d_year" = 2001
  AND NOT "Store Sales"."ss_store_sk" IS NULL
  AND NOT "Store Sales"."ss_hdemo_sk" IS NULL
  AND NOT "Store Sales"."ss_cdemo_sk" IS NULL
  AND (
    "Customer Demographics"."cd_marital_status" = 'M'
    AND "Customer Demographics"."cd_education_status" = 'Advanced Degree'
    AND "Store Sales"."ss_sales_price" BETWEEN 100.0 AND 150.0
    AND "Household Demographics"."hd_dep_count" = 3
    OR "Customer Demographics"."cd_marital_status" = 'S'
    AND "Customer Demographics"."cd_education_status" = 'College'
    AND "Store Sales"."ss_sales_price" BETWEEN 50.0 AND 100.0
    AND "Household Demographics"."hd_dep_count" = 1
    OR "Customer Demographics"."cd_marital_status" = 'W'
    AND "Customer Demographics"."cd_education_status" = '2 yr Degree'
    AND "Store Sales"."ss_sales_price" BETWEEN 150.0 AND 200.0
    AND "Household Demographics"."hd_dep_count" = 1
  )
  AND "Customer Address"."ca_country" = 'United States'
  AND (
    "Customer Address"."ca_state" IN ('TX', 'OH')
    AND "Store Sales"."ss_net_profit" BETWEEN 100 AND 200
    OR "Customer Address"."ca_state" IN ('OR', 'NM', 'KY')
    AND "Store Sales"."ss_net_profit" BETWEEN 150 AND 300
    OR "Customer Address"."ca_state" IN ('VA', 'TX', 'MS')
    AND "Store Sales"."ss_net_profit" BETWEEN 50 AND 250
  )
