-- Q99 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  SUBSTRING("Warehouse"."w_warehouse_name", 1, 20) AS "Warehouse Name 20",
  "Ship Mode"."sm_type" AS "Ship Type",
  "Call Center"."cc_name" AS "Call Center Name",
  CAST(COUNT(
    CASE
      WHEN "Catalog Sales"."cs_ship_date_sk" - "Catalog Sales"."cs_sold_date_sk" <= 30
      THEN "Catalog Sales"."cs_order_number"
    END
  ) AS Nullable(Int64)) AS "Catalog 30 days",
  CAST(COUNT(
    CASE
      WHEN "Catalog Sales"."cs_ship_date_sk" - "Catalog Sales"."cs_sold_date_sk" BETWEEN 31 AND 60
      THEN "Catalog Sales"."cs_order_number"
    END
  ) AS Nullable(Int64)) AS "Catalog 31-60 days",
  CAST(COUNT(
    CASE
      WHEN "Catalog Sales"."cs_ship_date_sk" - "Catalog Sales"."cs_sold_date_sk" BETWEEN 61 AND 90
      THEN "Catalog Sales"."cs_order_number"
    END
  ) AS Nullable(Int64)) AS "Catalog 61-90 days",
  CAST(COUNT(
    CASE
      WHEN "Catalog Sales"."cs_ship_date_sk" - "Catalog Sales"."cs_sold_date_sk" BETWEEN 91 AND 120
      THEN "Catalog Sales"."cs_order_number"
    END
  ) AS Nullable(Int64)) AS "Catalog 91-120 days",
  CAST(COUNT(
    CASE
      WHEN "Catalog Sales"."cs_ship_date_sk" - "Catalog Sales"."cs_sold_date_sk" > 120
      THEN "Catalog Sales"."cs_order_number"
    END
  ) AS Nullable(Int64)) AS "Catalog >120 days"
FROM "tpcds"."catalog_sales" AS "Catalog Sales"
LEFT JOIN "tpcds"."call_center" AS "Call Center"
  ON "Catalog Sales"."cs_call_center_sk" = "Call Center"."cc_call_center_sk"
LEFT JOIN "tpcds"."ship_mode" AS "Ship Mode"
  ON "Catalog Sales"."cs_ship_mode_sk" = "Ship Mode"."sm_ship_mode_sk"
LEFT JOIN "tpcds"."warehouse" AS "Warehouse"
  ON "Catalog Sales"."cs_warehouse_sk" = "Warehouse"."w_warehouse_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Catalog Sales"."cs_ship_date_sk" = "Date"."d_date_sk"
WHERE
  "Date"."d_month_seq" BETWEEN 1200 AND 1211
  AND NOT (
    "Catalog Sales"."cs_call_center_sk" IS NULL
  )
  AND NOT (
    "Catalog Sales"."cs_warehouse_sk" IS NULL
  )
  AND NOT (
    "Catalog Sales"."cs_ship_mode_sk" IS NULL
  )
GROUP BY ALL
ORDER BY
  SUBSTRING("Warehouse"."w_warehouse_name", 1, 20) ASC,
  "Ship Mode"."sm_type" ASC,
  "Call Center"."cc_name" ASC
