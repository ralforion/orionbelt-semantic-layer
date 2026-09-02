-- Q88 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h8_30_to_9",
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h9_to_9_30",
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h9_30_to_10",
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h10_to_10_30",
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h10_30_to_11",
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h11_to_11_30",
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h11_30_to_12",
  accurateCast(
    trunc(
      COUNT(
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
      )
    ),
    'Nullable(Int64)'
  ) AS "h12_to_12_30"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."household_demographics" AS "Household Demographics"
  ON "Store Sales"."ss_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "tpcds"."store" AS "Store"
  ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
LEFT JOIN "tpcds"."time_dim" AS "Time"
  ON "Store Sales"."ss_sold_time_sk" = "Time"."t_time_sk"
