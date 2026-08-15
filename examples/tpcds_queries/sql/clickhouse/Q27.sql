-- Q27 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Item"."i_item_id" AS "Item ID",
  "Store"."s_state" AS "Store State",
  CAST(round(AVG("Store Sales"."ss_quantity"), 6) AS Nullable(Decimal(18, 6))) AS "Avg Quantity",
  CAST(round(AVG("Store Sales"."ss_list_price"), 6) AS Nullable(Decimal(18, 6))) AS "Avg List Price Precise",
  CAST(round(AVG("Store Sales"."ss_coupon_amt"), 6) AS Nullable(Decimal(18, 6))) AS "Avg Coupon Amount",
  CAST(round(AVG("Store Sales"."ss_sales_price"), 6) AS Nullable(Decimal(18, 6))) AS "Avg Sales Price Precise",
  GROUPING("Item"."i_item_id") AS "_g_Item ID",
  GROUPING("Store"."s_state") AS "_g_Store State"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "tpcds"."customer_demographics" AS "Customer Demographics"
  ON "Store Sales"."ss_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
WHERE
  "Customer Demographics"."cd_gender" = 'M'
  AND "Customer Demographics"."cd_marital_status" = 'S'
  AND "Customer Demographics"."cd_education_status" = 'College'
  AND "Date"."d_year" = 2002
  AND "Store"."s_state" = 'TN'
GROUP BY
  "Item"."i_item_id",
  "Store"."s_state"
  WITH ROLLUP
ORDER BY
  "Item"."i_item_id" ASC NULLS FIRST,
  "Store"."s_state" ASC NULLS FIRST
