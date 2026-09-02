-- Q73 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Customer"."c_last_name" AS "Customer Name",
  "Customer"."c_first_name" AS "Customer First Name",
  "Customer"."c_salutation" AS "Customer Salutation",
  "Customer"."c_preferred_cust_flag" AS "Customer Preferred Flag",
  "Store Sales"."ss_ticket_number" AS "Ticket Number",
  accurateCast(trunc(COUNT(1)), 'Nullable(Int32)') AS "Store Sales Count"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."customer" AS "Customer"
  ON "Store Sales"."ss_customer_sk" = "Customer"."c_customer_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "tpcds"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
WHERE
  "Date"."d_year" IN (1999, 2000, 2001)
  AND "Date"."d_dom" BETWEEN 1 AND 2
  AND "Store"."s_county" IN ('Orange County', 'Bronx County', 'Franklin Parish', 'Williamson County')
  AND "Household Demographics"."hd_vehicle_count" > 0
  AND CASE
    WHEN "Household Demographics"."hd_vehicle_count" > 0
    THEN CAST("Household Demographics"."hd_dep_count" * 1.0 AS Nullable(Decimal(38, 14))) / nullIf(CAST("Household Demographics"."hd_vehicle_count" AS Nullable(Decimal(38, 14))), 0)
    ELSE NULL
  END > 1
  AND "Household Demographics"."hd_buy_potential" IN ('Unknown', '>10000')
  AND NOT (
    "Store Sales"."ss_customer_sk" IS NULL
  )
GROUP BY ALL
HAVING
  "Store Sales Count" BETWEEN 1 AND 5
ORDER BY
  COUNT(1) DESC,
  "Customer"."c_last_name" ASC
