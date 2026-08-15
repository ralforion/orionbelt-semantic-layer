-- Q96 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  CAST(COUNT(1) AS INT) AS "Store Sales Count"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."time_dim" AS "Time"
  ON "Store Sales"."ss_sold_time_sk" = "Time"."t_time_sk"
LEFT JOIN "main"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
WHERE
  "Time"."t_hour" = 20
  AND "Time"."t_minute" >= 30
  AND "Household Demographics"."hd_dep_count" = 7
  AND "Store"."s_store_name" = 'ese'
