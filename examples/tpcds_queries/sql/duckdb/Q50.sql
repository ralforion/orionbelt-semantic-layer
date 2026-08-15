-- Q50 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Store"."s_store_name" AS "Store Name",
  "Store"."s_company_id" AS "Store Company ID",
  "Store"."s_street_number" AS "Store Street Number",
  "Store"."s_street_name" AS "Store Street Name",
  "Store"."s_street_type" AS "Store Street Type",
  "Store"."s_suite_number" AS "Store Suite Number",
  "Store"."s_city" AS "Store City",
  "Store"."s_county" AS "Store County",
  "Store"."s_state" AS "Store State",
  "Store"."s_zip" AS "Store Zip",
  CAST(COUNT(
    CASE
      WHEN "Store Returns"."sr_returned_date_sk" - "Store Sales"."ss_sold_date_sk" <= 30
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "Store Return 30 days",
  CAST(COUNT(
    CASE
      WHEN "Store Returns"."sr_returned_date_sk" - "Store Sales"."ss_sold_date_sk" BETWEEN 31 AND 60
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "Store Return 31-60 days",
  CAST(COUNT(
    CASE
      WHEN "Store Returns"."sr_returned_date_sk" - "Store Sales"."ss_sold_date_sk" BETWEEN 61 AND 90
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "Store Return 61-90 days",
  CAST(COUNT(
    CASE
      WHEN "Store Returns"."sr_returned_date_sk" - "Store Sales"."ss_sold_date_sk" BETWEEN 91 AND 120
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "Store Return 91-120 days",
  CAST(COUNT(
    CASE
      WHEN "Store Returns"."sr_returned_date_sk" - "Store Sales"."ss_sold_date_sk" > 120
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "Store Return over 120 days"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "main"."store_returns" AS "Store Returns"
  ON "Store Sales"."ss_item_sk" = "Store Returns"."sr_item_sk"
  AND "Store Sales"."ss_ticket_number" = "Store Returns"."sr_ticket_number"
LEFT JOIN "main"."date_dim" AS "Store Returned Date"
  ON "Store Returns"."sr_returned_date_sk" = "Store Returned Date"."d_date_sk"
WHERE
  "Store Returned Date"."d_year" = 2001
  AND "Store Returned Date"."d_moy" = 8
  AND (
    "Store Sales"."ss_customer_sk" = "Store Returns"."sr_customer_sk"
  ) = TRUE
  AND NOT "Store Sales"."ss_store_sk" IS NULL
GROUP BY ALL
ORDER BY
  "Store"."s_store_name" ASC,
  "Store"."s_company_id" ASC,
  "Store"."s_street_number" ASC,
  "Store"."s_street_name" ASC,
  "Store"."s_street_type" ASC,
  "Store"."s_suite_number" ASC,
  "Store"."s_city" ASC,
  "Store"."s_county" ASC,
  "Store"."s_state" ASC,
  "Store"."s_zip" ASC
LIMIT 100
