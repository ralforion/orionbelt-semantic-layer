-- Q61 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  CAST(round(
    toDecimal256(
      toString(
        SUM(
          CASE
            WHEN "Promotion"."p_channel_dmail" = 'Y'
            OR "Promotion"."p_channel_email" = 'Y'
            OR "Promotion"."p_channel_tv" = 'Y'
            THEN "Store Sales"."ss_ext_sales_price"
          END
        )
      ),
      3
    ),
    2
  ) AS Nullable(Decimal(18, 2))) AS "Promotional Sales",
  CAST(round(toDecimal256(toString(SUM("Store Sales"."ss_ext_sales_price")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Store Sales Amount",
  CAST(round(
    toDecimal256(
      toString(
        CAST(SUM(
          CASE
            WHEN "Promotion"."p_channel_dmail" = 'Y'
            OR "Promotion"."p_channel_email" = 'Y'
            OR "Promotion"."p_channel_tv" = 'Y'
            THEN "Store Sales"."ss_ext_sales_price"
          END
        ) * 100.0 AS Nullable(Decimal(38, 14))) / nullIf(CAST(SUM("Store Sales"."ss_ext_sales_price") AS Nullable(Decimal(38, 14))), 0)
      ),
      7
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "promo_pct"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."promotion" AS "Promotion"
  ON "Store Sales"."ss_promo_sk" = "Promotion"."p_promo_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "tpcds"."customer" AS "Customer"
  ON "Store Sales"."ss_customer_sk" = "Customer"."c_customer_sk"
LEFT JOIN "tpcds"."customer_address" AS "Customer Address"
  ON "Customer"."c_current_addr_sk" = "Customer Address"."ca_address_sk"
WHERE
  "Date"."d_year" = 1998
  AND "Date"."d_moy" = 11
  AND "Item"."i_category" = 'Jewelry'
  AND "Store"."s_gmt_offset" = -5
  AND "Customer Address"."ca_gmt_offset" = -5
  AND NOT (
    "Store Sales"."ss_customer_sk" IS NULL
  )
  AND NOT (
    "Store Sales"."ss_store_sk" IS NULL
  )
