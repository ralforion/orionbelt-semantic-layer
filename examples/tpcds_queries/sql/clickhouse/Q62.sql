-- Q62 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  SUBSTRING(toString("Warehouse"."w_warehouse_name"), 1, 20) AS "Warehouse Name 20",
  "Ship Mode"."sm_type" AS "Ship Type",
  "Web Site"."web_name" AS "Web Name",
  CAST(COUNT(
    CASE
      WHEN "Web Sales"."ws_ship_date_sk" - "Web Sales"."ws_sold_date_sk" <= 30
      THEN "Web Sales"."ws_order_number"
    END
  ) AS Nullable(Int64)) AS "Web 30 days",
  CAST(COUNT(
    CASE
      WHEN "Web Sales"."ws_ship_date_sk" - "Web Sales"."ws_sold_date_sk" BETWEEN 31 AND 60
      THEN "Web Sales"."ws_order_number"
    END
  ) AS Nullable(Int64)) AS "Web 31-60 days",
  CAST(COUNT(
    CASE
      WHEN "Web Sales"."ws_ship_date_sk" - "Web Sales"."ws_sold_date_sk" BETWEEN 61 AND 90
      THEN "Web Sales"."ws_order_number"
    END
  ) AS Nullable(Int64)) AS "Web 61-90 days",
  CAST(COUNT(
    CASE
      WHEN "Web Sales"."ws_ship_date_sk" - "Web Sales"."ws_sold_date_sk" BETWEEN 91 AND 120
      THEN "Web Sales"."ws_order_number"
    END
  ) AS Nullable(Int64)) AS "Web 91-120 days",
  CAST(COUNT(
    CASE
      WHEN "Web Sales"."ws_ship_date_sk" - "Web Sales"."ws_sold_date_sk" > 120
      THEN "Web Sales"."ws_order_number"
    END
  ) AS Nullable(Int64)) AS "Web >120 days"
FROM "tpcds"."web_sales" AS "Web Sales"
LEFT JOIN "tpcds"."ship_mode" AS "Ship Mode"
  ON "Web Sales"."ws_ship_mode_sk" = "Ship Mode"."sm_ship_mode_sk"
LEFT JOIN "tpcds"."warehouse" AS "Warehouse"
  ON "Web Sales"."ws_warehouse_sk" = "Warehouse"."w_warehouse_sk"
LEFT JOIN "tpcds"."web_site" AS "Web Site"
  ON "Web Sales"."ws_web_site_sk" = "Web Site"."web_site_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Web Sales"."ws_ship_date_sk" = "Date"."d_date_sk"
WHERE
  "Date"."d_month_seq" BETWEEN 1200 AND 1211
  AND NOT (
    "Web Sales"."ws_web_site_sk" IS NULL
  )
  AND NOT (
    "Web Sales"."ws_warehouse_sk" IS NULL
  )
  AND NOT (
    "Web Sales"."ws_ship_mode_sk" IS NULL
  )
GROUP BY ALL
ORDER BY
  SUBSTRING(toString("Warehouse"."w_warehouse_name"), 1, 20) ASC,
  "Ship Mode"."sm_type" ASC,
  "Web Site"."web_name" ASC
