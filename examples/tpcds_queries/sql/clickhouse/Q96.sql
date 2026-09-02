-- Q96 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  accurateCast(trunc(COUNT(1)), 'Nullable(Int32)') AS "Store Sales Count"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."time_dim" AS "Time"
  ON "Store Sales"."ss_sold_time_sk" = "Time"."t_time_sk"
LEFT JOIN "tpcds"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "tpcds"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
WHERE
  "Time"."t_hour" = 20
  AND "Time"."t_minute" >= 30
  AND "Household Demographics"."hd_dep_count" = 7
  AND "Store"."s_store_name" = 'ese'
