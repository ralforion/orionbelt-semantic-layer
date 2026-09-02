-- Q15 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Customer Address"."ca_zip" AS "Customer Zip",
  CAST(round(toDecimal256(toString(SUM("Catalog Sales"."cs_sales_price")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Catalog Sales Price Sum"
FROM "tpcds"."catalog_sales" AS "Catalog Sales"
LEFT JOIN "tpcds"."customer" AS "Customer"
  ON "Catalog Sales"."cs_bill_customer_sk" = "Customer"."c_customer_sk"
LEFT JOIN "tpcds"."customer_address" AS "Customer Address"
  ON "Customer"."c_current_addr_sk" = "Customer Address"."ca_address_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Catalog Sales"."cs_sold_date_sk" = "Date"."d_date_sk"
WHERE
  "Date"."d_qoy" = 2
  AND "Date"."d_year" = 2001
  AND (
    SUBSTRING(toString("Customer Address"."ca_zip"), 1, 5) IN ('85669', '86197', '88274', '83405', '86475', '85392', '85460', '80348', '81792')
    OR "Customer Address"."ca_state" IN ('CA', 'WA', 'GA')
    OR "Catalog Sales"."cs_sales_price" > 500
  )
GROUP BY ALL
ORDER BY
  "Customer Address"."ca_zip" ASC
LIMIT 100
