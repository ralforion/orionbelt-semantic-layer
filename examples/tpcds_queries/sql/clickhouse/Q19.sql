-- Q19 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Item"."i_brand_id" AS "Brand ID",
  "Item"."i_brand" AS "Brand",
  "Item"."i_manufact_id" AS "Manufacturer ID",
  "Item"."i_manufact" AS "Manufacturer",
  CAST(round(toDecimal256(toString(SUM("Store Sales"."ss_ext_sales_price")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Store Sales Amount"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "tpcds"."customer" AS "Customer"
  ON "Store Sales"."ss_customer_sk" = "Customer"."c_customer_sk"
LEFT JOIN "tpcds"."customer_address" AS "Customer Address"
  ON "Customer"."c_current_addr_sk" = "Customer Address"."ca_address_sk"
WHERE
  "Item"."i_manager_id" = 8
  AND "Date"."d_moy" = 11
  AND "Date"."d_year" = 1998
  AND (
    SUBSTRING(toString("Store"."s_zip"), 1, 5) = SUBSTRING(toString("Customer Address"."ca_zip"), 1, 5)
  ) = FALSE
GROUP BY ALL
ORDER BY
  SUM("Store Sales"."ss_ext_sales_price") DESC,
  "Item"."i_brand" ASC,
  "Item"."i_brand_id" ASC,
  "Item"."i_manufact_id" ASC,
  "Item"."i_manufact" ASC
LIMIT 100
