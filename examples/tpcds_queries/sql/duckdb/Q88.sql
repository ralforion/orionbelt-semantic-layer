-- Q88 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 8
      AND "Time"."t_minute" >= 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h8_30_to_9",
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 9
      AND "Time"."t_minute" < 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h9_to_9_30",
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 9
      AND "Time"."t_minute" >= 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h9_30_to_10",
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 10
      AND "Time"."t_minute" < 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h10_to_10_30",
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 10
      AND "Time"."t_minute" >= 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h10_30_to_11",
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 11
      AND "Time"."t_minute" < 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h11_to_11_30",
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 11
      AND "Time"."t_minute" >= 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h11_30_to_12",
  CAST(COUNT(
    CASE
      WHEN "Time"."t_hour" = 12
      AND "Time"."t_minute" < 30
      AND "Store"."s_store_name" = 'ese'
      AND (
        "Household Demographics"."hd_dep_count" = 4
        AND "Household Demographics"."hd_vehicle_count" <= 6
        OR "Household Demographics"."hd_dep_count" = 2
        AND "Household Demographics"."hd_vehicle_count" <= 4
        OR "Household Demographics"."hd_dep_count" = 0
        AND "Household Demographics"."hd_vehicle_count" <= 2
      )
      THEN "Store Sales"."ss_ticket_number"
    END
  ) AS BIGINT) AS "h12_to_12_30"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "main"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "main"."time_dim" AS "Time"
  ON "Store Sales"."ss_sold_time_sk" = "Time"."t_time_sk"
