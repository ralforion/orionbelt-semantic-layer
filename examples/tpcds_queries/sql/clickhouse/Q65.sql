-- Q65 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

WITH "base" AS (
  SELECT
    "Store"."s_store_sk" AS "Store Key Dim",
    "Item"."i_item_sk" AS "Item Key Dim",
    "Store"."s_store_name" AS "Store Name",
    "Item"."i_item_desc" AS "Item Description",
    "Item"."i_current_price" AS "Current Price",
    "Item"."i_wholesale_cost" AS "Item Wholesale Cost",
    "Item"."i_brand" AS "Brand",
    CAST(round(toDecimal256(toString(SUM("Store Sales"."ss_sales_price")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Sales Price Sum",
    SUM("Store Sales"."ss_sales_price") AS "Store Revenue",
    COUNT(
      DISTINCT CASE
        WHEN NOT (
          "Store Sales"."ss_sales_price" IS NULL
        )
        THEN "Item"."i_item_sk"
      END
    ) AS "Store Item Groups"
  FROM "tpcds"."store_sales" AS "Store Sales"
  LEFT JOIN "tpcds"."item" AS "Item"
    ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
  LEFT JOIN "tpcds"."store" AS "Store"
    ON "Store Sales"."ss_store_sk" = "Store"."s_store_sk"
  LEFT JOIN "tpcds"."date_dim" AS "Date"
    ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
  WHERE
    "Date"."d_month_seq" BETWEEN 1176 AND 1187
    AND NOT (
      "Store Sales"."ss_store_sk" IS NULL
    )
    AND NOT (
      "Store Sales"."ss_item_sk" IS NULL
    )
  GROUP BY ALL
), "having_window" AS (
  SELECT
    "Store Key Dim" AS "Store Key Dim",
    "Item Key Dim" AS "Item Key Dim",
    "Store Name" AS "Store Name",
    "Item Description" AS "Item Description",
    "Current Price" AS "Current Price",
    "Item Wholesale Cost" AS "Item Wholesale Cost",
    "Brand" AS "Brand",
    "Sales Price Sum" AS "Sales Price Sum",
    CASE
      WHEN CAST(SUM("Store Revenue") OVER (PARTITION BY "Store Key Dim") AS Nullable(Decimal(38, 14))) / nullIf(
        CAST(SUM("Store Item Groups") OVER (PARTITION BY "Store Key Dim") AS Nullable(Decimal(38, 14))),
        0
      ) > 0
      THEN CAST("Sales Price Sum" AS Nullable(Decimal(38, 14))) / nullIf(
        CAST(CAST(SUM("Store Revenue") OVER (PARTITION BY "Store Key Dim") AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(SUM("Store Item Groups") OVER (PARTITION BY "Store Key Dim") AS Nullable(Decimal(38, 14))),
          0
        ) AS Nullable(Decimal(38, 14))),
        0
      )
      ELSE NULL
    END AS "Store Item Revenue Share"
  FROM "base" AS "base"
)
SELECT
  "having_window"."Store Key Dim" AS "Store Key Dim",
  "having_window"."Item Key Dim" AS "Item Key Dim",
  "having_window"."Store Name" AS "Store Name",
  "having_window"."Item Description" AS "Item Description",
  "having_window"."Current Price" AS "Current Price",
  "having_window"."Item Wholesale Cost" AS "Item Wholesale Cost",
  "having_window"."Brand" AS "Brand",
  "having_window"."Sales Price Sum" AS "Sales Price Sum"
FROM "having_window" AS "having_window"
WHERE
  "Store Item Revenue Share" <= 0.1
ORDER BY
  "Store Name" ASC,
  "Item Description" ASC
LIMIT 100
