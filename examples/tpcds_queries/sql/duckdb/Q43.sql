-- Q43 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Store"."s_store_name" AS "Store Name",
  "Store"."s_store_id" AS "Store ID",
  CAST(SUM(CASE WHEN "Date"."d_day_name" = 'Sunday' THEN "Store Sales"."ss_sales_price" END) AS DECIMAL(18, 2)) AS "Sun Sales",
  CAST(SUM(CASE WHEN "Date"."d_day_name" = 'Monday' THEN "Store Sales"."ss_sales_price" END) AS DECIMAL(18, 2)) AS "Mon Sales",
  CAST(SUM(
    CASE WHEN "Date"."d_day_name" = 'Tuesday' THEN "Store Sales"."ss_sales_price" END
  ) AS DECIMAL(18, 2)) AS "Tue Sales",
  CAST(SUM(
    CASE WHEN "Date"."d_day_name" = 'Wednesday' THEN "Store Sales"."ss_sales_price" END
  ) AS DECIMAL(18, 2)) AS "Wed Sales",
  CAST(SUM(
    CASE WHEN "Date"."d_day_name" = 'Thursday' THEN "Store Sales"."ss_sales_price" END
  ) AS DECIMAL(18, 2)) AS "Thu Sales",
  CAST(SUM(CASE WHEN "Date"."d_day_name" = 'Friday' THEN "Store Sales"."ss_sales_price" END) AS DECIMAL(18, 2)) AS "Fri Sales",
  CAST(SUM(
    CASE WHEN "Date"."d_day_name" = 'Saturday' THEN "Store Sales"."ss_sales_price" END
  ) AS DECIMAL(18, 2)) AS "Sat Sales"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
WHERE
  "Store"."s_gmt_offset" = -5 AND "Date"."d_year" = 2000
GROUP BY ALL
ORDER BY
  "Store"."s_store_name" ASC,
  "Store"."s_store_id" ASC
