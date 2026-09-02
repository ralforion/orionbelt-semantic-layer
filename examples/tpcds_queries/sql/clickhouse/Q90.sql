-- Q90 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  CAST(round(
    toDecimal256(
      toString(
        CAST(COUNT(
          CASE
            WHEN "Time"."t_hour" BETWEEN 8 AND 9
            AND "Household Demographics"."hd_dep_count" = 6
            AND "Web Page"."wp_char_count" BETWEEN 5000 AND 5200
            THEN "Web Sales"."ws_order_number"
          END
        ) AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(COUNT(
            CASE
              WHEN "Time"."t_hour" BETWEEN 19 AND 20
              AND "Household Demographics"."hd_dep_count" = 6
              AND "Web Page"."wp_char_count" BETWEEN 5000 AND 5200
              THEN "Web Sales"."ws_order_number"
            END
          ) AS Nullable(Decimal(38, 14))),
          0
        )
      ),
      7
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "am_pm_ratio"
FROM "tpcds"."web_sales" AS "Web Sales"
LEFT JOIN "tpcds"."household_demographics" AS "Household Demographics"
  ON "Web Sales"."ws_ship_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "tpcds"."time_dim" AS "Time"
  ON "Web Sales"."ws_sold_time_sk" = "Time"."t_time_sk"
LEFT JOIN "tpcds"."web_page" AS "Web Page"
  ON "Web Sales"."ws_web_page_sk" = "Web Page"."wp_web_page_sk"
