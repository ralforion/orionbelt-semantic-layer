-- Q27 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Item"."i_item_id" AS "Item ID",
  "Store"."s_state" AS "Store State",
  CAST(AVG("Store Sales"."ss_quantity") AS DECIMAL(18, 6)) AS "Avg Quantity",
  CAST(AVG("Store Sales"."ss_list_price") AS DECIMAL(18, 6)) AS "Avg List Price Precise",
  CAST(AVG("Store Sales"."ss_coupon_amt") AS DECIMAL(18, 6)) AS "Avg Coupon Amount",
  CAST(AVG("Store Sales"."ss_sales_price") AS DECIMAL(18, 6)) AS "Avg Sales Price Precise",
  GROUPING("Item"."i_item_id") AS "_g_Item ID",
  GROUPING("Store"."s_state") AS "_g_Store State"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "main"."customer_demographics" AS "Customer Demographics"
  ON "Store Sales"."ss_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
WHERE
  "Customer Demographics"."cd_gender" = 'M'
  AND "Customer Demographics"."cd_marital_status" = 'S'
  AND "Customer Demographics"."cd_education_status" = 'College'
  AND "Date"."d_year" = 2002
  AND "Store"."s_state" = 'TN'
GROUP BY
  ROLLUP (
    "Item"."i_item_id",
    "Store"."s_state"
  )
ORDER BY
  "Item"."i_item_id" ASC NULLS FIRST,
  "Store"."s_state" ASC NULLS FIRST
