-- Q26 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Item"."i_item_id" AS "Item ID",
  CAST(round(AVG("Catalog Sales"."cs_quantity"), 6) AS Nullable(Decimal(18, 6))) AS "Catalog Avg Quantity",
  CAST(round(AVG("Catalog Sales"."cs_list_price"), 6) AS Nullable(Decimal(18, 6))) AS "Catalog Avg List Price",
  CAST(round(AVG("Catalog Sales"."cs_coupon_amt"), 6) AS Nullable(Decimal(18, 6))) AS "Catalog Avg Coupon Amount",
  CAST(round(AVG("Catalog Sales"."cs_sales_price"), 6) AS Nullable(Decimal(18, 6))) AS "Catalog Avg Sales Price"
FROM "tpcds"."catalog_sales" AS "Catalog Sales"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Catalog Sales"."cs_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."customer_demographics" AS "Customer Demographics"
  ON "Catalog Sales"."cs_bill_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Catalog Sales"."cs_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."promotion" AS "Promotion"
  ON "Catalog Sales"."cs_promo_sk" = "Promotion"."p_promo_sk"
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
