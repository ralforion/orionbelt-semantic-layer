-- Q90 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" BETWEEN 8 AND 9
      AND "Household Demographics"."hd_dep_count" = 6
      AND "Web Page"."wp_char_count" BETWEEN 5000 AND 5200
      THEN "Web Sales"."ws_order_number"
    END
  ) / COUNT(
    CASE
      WHEN "Time"."t_hour" BETWEEN 19 AND 20
      AND "Household Demographics"."hd_dep_count" = 6
      AND "Web Page"."wp_char_count" BETWEEN 5000 AND 5200
      THEN "Web Sales"."ws_order_number"
    END
  ) AS DECIMAL(18, 6)) AS "am_pm_ratio"
FROM "main"."web_sales" AS "Web Sales"
LEFT JOIN "main"."household_demographics" AS "Household Demographics"
  ON "Web Sales"."ws_ship_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "main"."time_dim" AS "Time"
  ON "Web Sales"."ws_sold_time_sk" = "Time"."t_time_sk"
LEFT JOIN "main"."web_page" AS "Web Page"
  ON "Web Sales"."ws_web_page_sk" = "Web Page"."wp_web_page_sk"
