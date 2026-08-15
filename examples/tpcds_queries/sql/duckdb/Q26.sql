-- Q26 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Item"."i_item_id" AS "Item ID",
  CAST(AVG("Catalog Sales"."cs_quantity") AS DECIMAL(18, 6)) AS "Catalog Avg Quantity",
  CAST(AVG("Catalog Sales"."cs_list_price") AS DECIMAL(18, 6)) AS "Catalog Avg List Price",
  CAST(AVG("Catalog Sales"."cs_coupon_amt") AS DECIMAL(18, 6)) AS "Catalog Avg Coupon Amount",
  CAST(AVG("Catalog Sales"."cs_sales_price") AS DECIMAL(18, 6)) AS "Catalog Avg Sales Price"
FROM "main"."catalog_sales" AS "Catalog Sales"
LEFT JOIN "main"."item" AS "Item"
  ON "Catalog Sales"."cs_item_sk" = "Item"."i_item_sk"
LEFT JOIN "main"."customer_demographics" AS "Customer Demographics"
  ON "Catalog Sales"."cs_bill_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Catalog Sales"."cs_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "main"."promotion" AS "Promotion"
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
