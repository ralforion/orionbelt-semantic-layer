-- Q46 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Customer"."c_last_name" AS "Customer Name",
  "Customer"."c_first_name" AS "Customer First Name",
  "Customer Address"."ca_city" AS "Customer City",
  "Sale Address"."ca_city" AS "Bought City",
  "Store Sales"."ss_ticket_number" AS "Ticket Number",
  CAST(SUM("Store Sales"."ss_coupon_amt") AS DECIMAL(18, 2)) AS "Coupon Amount Sum",
  CAST(SUM("Store Sales"."ss_net_profit") AS DECIMAL(18, 2)) AS "Store Net Profit"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."customer" AS "Customer"
  ON "Store Sales"."ss_customer_sk" = "Customer"."c_customer_sk"
LEFT JOIN "main"."customer_address" AS "Customer Address"
  ON "Customer"."c_current_addr_sk" = "Customer Address"."ca_address_sk"
LEFT JOIN "main"."customer_address" AS "Sale Address"
  ON "Store Sales"."ss_addr_sk" = "Sale Address"."ca_address_sk"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "main"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
WHERE
  "Date"."d_dow" IN (6, 0)
  AND "Date"."d_year" IN (1999, 2000, 2001)
  AND "Store"."s_city" IN ('Fairview', 'Midway')
  AND (
    "Household Demographics"."hd_dep_count" = 4
    OR "Household Demographics"."hd_vehicle_count" = 3
  )
  AND (
    "Sale Address"."ca_city" <> "Customer Address"."ca_city"
  ) = TRUE
GROUP BY ALL
ORDER BY
  "Customer"."c_last_name" ASC,
  "Customer"."c_first_name" ASC,
  "Customer Address"."ca_city" ASC,
  "Sale Address"."ca_city" ASC,
  "Store Sales"."ss_ticket_number" ASC
LIMIT 100
