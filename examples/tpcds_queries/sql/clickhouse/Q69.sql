-- Q69 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Customer Demographics"."cd_gender" AS "Gender",
  "Customer Demographics"."cd_marital_status" AS "Marital Status",
  "Customer Demographics"."cd_education_status" AS "Education Status",
  "Customer Demographics"."cd_purchase_estimate" AS "Purchase Estimate",
  "Customer Demographics"."cd_credit_rating" AS "Credit Rating",
  CAST(COUNT(1) AS Nullable(Int32)) AS "Customer Count"
FROM "tpcds"."customer" AS "Customer"
LEFT JOIN "tpcds"."customer_demographics" AS "Customer Demographics"
  ON "Customer"."c_current_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
LEFT JOIN "tpcds"."customer_address" AS "Customer Address"
  ON "Customer"."c_current_addr_sk" = "Customer Address"."ca_address_sk"
WHERE
  "Customer Address"."ca_state" IN ('KY', 'GA', 'NM')
  AND NOT (
    "Customer"."c_current_cdemo_sk" IS NULL
  )
  AND EXISTS(
    SELECT
      1
    FROM "tpcds"."store_sales" AS "Store Sales"
    INNER JOIN "tpcds"."date_dim" AS "Date"
      ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
    WHERE
      "Customer"."c_customer_sk" = "Store Sales"."ss_customer_sk"
      AND "Date"."d_year" = 2001
      AND "Date"."d_moy" BETWEEN 4 AND 6
  )
  AND NOT EXISTS(
    SELECT
      1
    FROM "tpcds"."web_sales" AS "Web Sales"
    INNER JOIN "tpcds"."date_dim" AS "Date"
      ON "Web Sales"."ws_sold_date_sk" = "Date"."d_date_sk"
    WHERE
      "Customer"."c_customer_sk" = "Web Sales"."ws_bill_customer_sk"
      AND "Date"."d_year" = 2001
      AND "Date"."d_moy" BETWEEN 4 AND 6
  )
  AND NOT EXISTS(
    SELECT
      1
    FROM "tpcds"."catalog_sales" AS "Catalog Sales"
    INNER JOIN "tpcds"."date_dim" AS "Date"
      ON "Catalog Sales"."cs_sold_date_sk" = "Date"."d_date_sk"
    WHERE
      "Customer"."c_customer_sk" = "Catalog Sales"."cs_ship_customer_sk"
      AND "Date"."d_year" = 2001
      AND "Date"."d_moy" BETWEEN 4 AND 6
  )
GROUP BY ALL
ORDER BY
  "Customer Demographics"."cd_gender" ASC,
  "Customer Demographics"."cd_marital_status" ASC,
  "Customer Demographics"."cd_education_status" ASC,
  "Customer Demographics"."cd_purchase_estimate" ASC,
  "Customer Demographics"."cd_credit_rating" ASC
LIMIT 100
