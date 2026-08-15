-- Q79 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Customer"."c_last_name" AS "Customer Name",
  "Customer"."c_first_name" AS "Customer First Name",
  SUBSTRING("Store"."s_city", 1, 30) AS "Store City 30",
  "Store Sales"."ss_ticket_number" AS "Ticket Number",
  "Store Sales"."ss_addr_sk" AS "Sales Addr Key",
  CAST(SUM("Store Sales"."ss_coupon_amt") AS DECIMAL(18, 2)) AS "Coupon Amount Sum",
  CAST(SUM("Store Sales"."ss_net_profit") AS DECIMAL(18, 2)) AS "Store Net Profit"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."customer" AS "Customer"
  ON "Store Sales"."ss_customer_sk" = "Customer"."c_customer_sk"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "main"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
WHERE
  "Date"."d_dow" = 1
  AND "Date"."d_year" IN (1999, 2000, 2001)
  AND "Store"."s_number_employees" BETWEEN 200 AND 295
  AND NOT "Store Sales"."ss_customer_sk" IS NULL
  AND (
    "Household Demographics"."hd_dep_count" = 6
    OR "Household Demographics"."hd_vehicle_count" > 2
  )
GROUP BY ALL
ORDER BY
  "Customer"."c_last_name" ASC,
  "Customer"."c_first_name" ASC,
  SUBSTRING("Store"."s_city", 1, 30) ASC,
  SUM("Store Sales"."ss_net_profit") ASC,
  "Store Sales"."ss_ticket_number" ASC
