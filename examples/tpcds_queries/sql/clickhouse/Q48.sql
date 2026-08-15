-- Q48 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  CAST(SUM("Store Sales"."ss_quantity") AS Nullable(Int64)) AS "Quantity Sum"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."customer_demographics" AS "Customer Demographics"
  ON "Store Sales"."ss_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "tpcds"."customer_address" AS "Customer Address"
  ON "Store Sales"."ss_addr_sk" = "Customer Address"."ca_address_sk"
WHERE
  "Date"."d_year" = 2000
  AND NOT (
    "Store Sales"."ss_store_sk" IS NULL
  )
  AND (
    "Customer Demographics"."cd_marital_status" = 'M'
    AND "Customer Demographics"."cd_education_status" = '4 yr Degree'
    AND "Store Sales"."ss_sales_price" BETWEEN 100.0 AND 150.0
    OR "Customer Demographics"."cd_marital_status" = 'D'
    AND "Customer Demographics"."cd_education_status" = '2 yr Degree'
    AND "Store Sales"."ss_sales_price" BETWEEN 50.0 AND 100.0
    OR "Customer Demographics"."cd_marital_status" = 'S'
    AND "Customer Demographics"."cd_education_status" = 'College'
    AND "Store Sales"."ss_sales_price" BETWEEN 150.0 AND 200.0
  )
  AND "Customer Address"."ca_country" = 'United States'
  AND (
    "Customer Address"."ca_state" IN ('CO', 'OH', 'TX')
    AND "Store Sales"."ss_net_profit" BETWEEN 0 AND 2000
    OR "Customer Address"."ca_state" IN ('OR', 'MN', 'KY')
    AND "Store Sales"."ss_net_profit" BETWEEN 150 AND 3000
    OR "Customer Address"."ca_state" IN ('VA', 'CA', 'MS')
    AND "Store Sales"."ss_net_profit" BETWEEN 50 AND 25000
  )
