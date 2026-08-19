-- Q34 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Customer"."c_last_name" AS "Customer Name",
  "Customer"."c_first_name" AS "Customer First Name",
  "Customer"."c_salutation" AS "Customer Salutation",
  "Customer"."c_preferred_cust_flag" AS "Customer Preferred Flag",
  "Store Sales"."ss_ticket_number" AS "Ticket Number",
  CAST(COUNT(1) AS Nullable(Int32)) AS "Store Sales Count"
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
  AND "Store"."s_county" = 'Williamson County'
  AND "Household Demographics"."hd_vehicle_count" > 0
  AND CASE
    WHEN "Household Demographics"."hd_vehicle_count" > 0
    THEN CAST("Household Demographics"."hd_dep_count" * 1.0 AS Nullable(Decimal(38, 14))) / nullIf(CAST("Household Demographics"."hd_vehicle_count" AS Nullable(Decimal(38, 14))), 0)
    ELSE NULL
  END > 1.2
  AND "Household Demographics"."hd_buy_potential" IN ('>10000', 'Unknown')
  AND (
    "Date"."d_dom" BETWEEN 1 AND 3 OR "Date"."d_dom" BETWEEN 25 AND 28
  )
GROUP BY ALL
HAVING
  "Store Sales Count" BETWEEN 15 AND 20
ORDER BY
  "Customer"."c_last_name" DESC,
  "Customer"."c_first_name" DESC,
  "Customer"."c_salutation" DESC,
  "Customer"."c_preferred_cust_flag" DESC,
  "Store Sales"."ss_ticket_number" ASC
