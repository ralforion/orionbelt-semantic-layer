-- Q07 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Item"."i_item_id" AS "Item ID",
  CAST(round(AVG("Store Sales"."ss_quantity"), 6) AS Nullable(Decimal(18, 6))) AS "Avg Quantity",
  CAST(round(AVG("Store Sales"."ss_list_price"), 6) AS Nullable(Decimal(18, 6))) AS "Avg List Price Precise",
  CAST(round(AVG("Store Sales"."ss_coupon_amt"), 6) AS Nullable(Decimal(18, 6))) AS "Avg Coupon Amount",
  CAST(round(AVG("Store Sales"."ss_sales_price"), 6) AS Nullable(Decimal(18, 6))) AS "Avg Sales Price Precise"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."customer_demographics" AS "Customer Demographics"
  ON "Store Sales"."ss_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."promotion" AS "Promotion"
  ON "Store Sales"."ss_promo_sk" = "Promotion"."p_promo_sk"
WHERE
  "Customer Demographics"."cd_gender" = 'M'
  AND "Customer Demographics"."cd_marital_status" = 'S'
  AND "Customer Demographics"."cd_education_status" = 'College'
  AND "Date"."d_year" = 2000
  AND (
    "Promotion"."p_channel_email" = 'N' OR "Promotion"."p_channel_event" = 'N'
  )
GROUP BY ALL
ORDER BY
  "Item"."i_item_id" ASC
LIMIT 100
