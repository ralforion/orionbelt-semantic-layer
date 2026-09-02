-- Q53 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

WITH "base" AS (
  SELECT
    "Item"."i_manufact_id" AS "Manufacturer ID",
    "Date"."d_qoy" AS "Quarter of Year",
    CAST(round(SUM("Store Sales"."ss_sales_price"), 2) AS Nullable(Decimal(18, 2))) AS "Sales Price Sum",
    SUM("Store Sales"."ss_sales_price") AS "Manufacturer Sales",
    COUNT(DISTINCT "Date"."d_qoy") AS "Manufacturer Quarter Groups",
    SUM("Store Sales"."ss_sales_price") AS "Manufacturer Sales",
    COUNT(DISTINCT "Date"."d_qoy") AS "Manufacturer Quarter Groups"
  FROM "tpcds"."store_sales" AS "Store Sales"
  LEFT JOIN "tpcds"."date_dim" AS "Date"
    ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "tpcds"."item" AS "Item"
    ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
  WHERE
    "Date"."d_month_seq" BETWEEN 1200 AND 1211
    AND NOT (
      "Store Sales"."ss_store_sk" IS NULL
    )
    AND (
      "Item"."i_category" IN ('Books', 'Children', 'Electronics')
      AND "Item"."i_class" IN ('personal', 'portable', 'reference', 'self-help')
      AND "Item"."i_brand" IN (
        'scholaramalgamalg #14',
        'scholaramalgamalg #7',
        'exportiunivamalg #9',
        'scholaramalgamalg #9'
      )
      OR "Item"."i_category" IN ('Women', 'Music', 'Men')
      AND "Item"."i_class" IN ('accessories', 'classical', 'fragrances', 'pants')
      AND "Item"."i_brand" IN ('amalgimporto #1', 'edu packscholar #1', 'exportiimporto #1', 'importoamalg #1')
    )
  GROUP BY ALL
), "having_window" AS (
  SELECT
    "Manufacturer ID" AS "Manufacturer ID",
    "Quarter of Year" AS "Quarter of Year",
    "Sales Price Sum" AS "Sales Price Sum",
    CAST(SUM("Manufacturer Sales") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))) / nullIf(
      CAST(SUM("Manufacturer Quarter Groups") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))),
      0
    ) AS "Avg Quarterly Sales",
    CASE
      WHEN CAST(SUM("Manufacturer Sales") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))) / nullIf(
        CAST(SUM("Manufacturer Quarter Groups") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))),
        0
      ) > 0
      THEN CAST(ABS(
        "Sales Price Sum" - CAST(SUM("Manufacturer Sales") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(SUM("Manufacturer Quarter Groups") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))),
          0
        )
      ) AS Nullable(Decimal(38, 14))) / nullIf(
        CAST(CAST(SUM("Manufacturer Sales") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(SUM("Manufacturer Quarter Groups") OVER (PARTITION BY "Manufacturer ID") AS Nullable(Decimal(38, 14))),
          0
        ) AS Nullable(Decimal(38, 14))),
        0
      )
      ELSE NULL
    END AS "Quarterly Deviation"
  FROM "base" AS "base"
)
SELECT
  "having_window"."Manufacturer ID" AS "Manufacturer ID",
  "having_window"."Quarter of Year" AS "Quarter of Year",
  "having_window"."Sales Price Sum" AS "Sales Price Sum",
  "having_window"."Avg Quarterly Sales" AS "Avg Quarterly Sales"
FROM "having_window" AS "having_window"
WHERE
  "Quarterly Deviation" > 0.1
ORDER BY
  "Avg Quarterly Sales" ASC,
  "Sales Price Sum" ASC,
  "Manufacturer ID" ASC
LIMIT 100
