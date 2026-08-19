-- Q61 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  CAST(SUM(
    CASE
      WHEN "Promotion"."p_channel_dmail" = 'Y'
      OR "Promotion"."p_channel_email" = 'Y'
      OR "Promotion"."p_channel_tv" = 'Y'
      THEN "Store Sales"."ss_ext_sales_price"
    END
  ) AS DECIMAL(18, 2)) AS "Promotional Sales",
  CAST(SUM("Store Sales"."ss_ext_sales_price") AS DECIMAL(18, 2)) AS "Store Sales Amount",
  CAST(SUM(
    CASE
      WHEN "Promotion"."p_channel_dmail" = 'Y'
      OR "Promotion"."p_channel_email" = 'Y'
      OR "Promotion"."p_channel_tv" = 'Y'
      THEN "Store Sales"."ss_ext_sales_price"
    END
  ) * 100.0 / NULLIF(SUM("Store Sales"."ss_ext_sales_price"), 0) AS DECIMAL(18, 6)) AS "promo_pct"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."promotion" AS "Promotion"
  ON "Store Sales"."ss_promo_sk" = "Promotion"."p_promo_sk"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "main"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "main"."customer" AS "Customer"
  ON "Store Sales"."ss_customer_sk" = "Customer"."c_customer_sk"
LEFT JOIN "main"."customer_address" AS "Customer Address"
  ON "Customer"."c_current_addr_sk" = "Customer Address"."ca_address_sk"
WHERE
  "Date"."d_year" = 1998
  AND "Date"."d_moy" = 11
  AND "Item"."i_category" = 'Jewelry'
  AND "Store"."s_gmt_offset" = -5
  AND "Customer Address"."ca_gmt_offset" = -5
  AND NOT "Store Sales"."ss_customer_sk" IS NULL
  AND NOT "Store Sales"."ss_store_sk" IS NULL
