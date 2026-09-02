-- Q83 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

WITH "composite_01" AS (
  SELECT
    "Item"."i_item_id" AS "Item ID",
    accurateCast(trunc("Store Returns"."sr_return_quantity"), 'Nullable(Int64)') AS "Store Returns Quantity",
    CAST(1 AS Nullable(Int32)) AS "Store Returns Count",
    CAST(NULL AS Nullable(Int64)) AS "Catalog Returns Quantity",
    CAST(NULL AS Nullable(Int32)) AS "Catalog Returns Count",
    CAST(NULL AS Nullable(Int64)) AS "Web Returns Quantity",
    CAST(NULL AS Nullable(Int32)) AS "Web Returns Count"
  FROM "tpcds"."store_returns" AS "Store Returns"
  LEFT JOIN "tpcds"."date_dim" AS "Date"
    ON "Store Returns"."sr_returned_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "tpcds"."item" AS "Item"
    ON "Store Returns"."sr_item_sk" = "Item"."i_item_sk"
  WHERE
    EXISTS(
      SELECT
        1
      FROM "tpcds"."date_dim" AS "Week Date"
      WHERE
        "Date"."d_week_seq" = "Week Date"."d_week_seq"
        AND "Week Date"."d_date" IN ('2000-06-30', '2000-09-27', '2000-11-17')
    )
  UNION ALL
  SELECT
    "Item"."i_item_id" AS "Item ID",
    CAST(NULL AS Nullable(Int64)) AS "Store Returns Quantity",
    CAST(NULL AS Nullable(Int32)) AS "Store Returns Count",
    accurateCast(trunc("Catalog Returns"."cr_return_quantity"), 'Nullable(Int64)') AS "Catalog Returns Quantity",
    CAST(1 AS Nullable(Int32)) AS "Catalog Returns Count",
    CAST(NULL AS Nullable(Int64)) AS "Web Returns Quantity",
    CAST(NULL AS Nullable(Int32)) AS "Web Returns Count"
  FROM "tpcds"."catalog_returns" AS "Catalog Returns"
  LEFT JOIN "tpcds"."date_dim" AS "Date"
    ON "Catalog Returns"."cr_returned_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "tpcds"."item" AS "Item"
    ON "Catalog Returns"."cr_item_sk" = "Item"."i_item_sk"
  WHERE
    EXISTS(
      SELECT
        1
      FROM "tpcds"."date_dim" AS "Week Date"
      WHERE
        "Date"."d_week_seq" = "Week Date"."d_week_seq"
        AND "Week Date"."d_date" IN ('2000-06-30', '2000-09-27', '2000-11-17')
    )
  UNION ALL
  SELECT
    "Item"."i_item_id" AS "Item ID",
    CAST(NULL AS Nullable(Int64)) AS "Store Returns Quantity",
    CAST(NULL AS Nullable(Int32)) AS "Store Returns Count",
    CAST(NULL AS Nullable(Int64)) AS "Catalog Returns Quantity",
    CAST(NULL AS Nullable(Int32)) AS "Catalog Returns Count",
    accurateCast(trunc("Web Returns"."wr_return_quantity"), 'Nullable(Int64)') AS "Web Returns Quantity",
    CAST(1 AS Nullable(Int32)) AS "Web Returns Count"
  FROM "tpcds"."web_returns" AS "Web Returns"
  LEFT JOIN "tpcds"."date_dim" AS "Date"
    ON "Web Returns"."wr_returned_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "tpcds"."item" AS "Item"
    ON "Web Returns"."wr_item_sk" = "Item"."i_item_sk"
  WHERE
    EXISTS(
      SELECT
        1
      FROM "tpcds"."date_dim" AS "Week Date"
      WHERE
        "Date"."d_week_seq" = "Week Date"."d_week_seq"
        AND "Week Date"."d_date" IN ('2000-06-30', '2000-09-27', '2000-11-17')
    )
)
SELECT
  "Item ID" AS "Item ID",
  accurateCast(trunc(SUM("composite_01"."Store Returns Quantity")), 'Nullable(Int64)') AS "Store Returns Quantity",
  accurateCast(trunc(SUM("composite_01"."Catalog Returns Quantity")), 'Nullable(Int64)') AS "Catalog Returns Quantity",
  accurateCast(trunc(SUM("composite_01"."Web Returns Quantity")), 'Nullable(Int64)') AS "Web Returns Quantity",
  CAST(round(
    toDecimal256(
      toString(
        CAST(CAST(SUM("composite_01"."Store Returns Quantity") * 1.0 AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity") AS Nullable(Decimal(38, 14))),
          0
        ) AS Nullable(Decimal(38, 14))) / CAST(3.0 AS Nullable(Decimal(38, 14))) * 100
      ),
      9
    ),
    8
  ) AS Nullable(Decimal(18, 8))) AS "Store Return Share",
  CAST(round(
    toDecimal256(
      toString(
        CAST(CAST(SUM("composite_01"."Catalog Returns Quantity") * 1.0 AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity") AS Nullable(Decimal(38, 14))),
          0
        ) AS Nullable(Decimal(38, 14))) / CAST(3.0 AS Nullable(Decimal(38, 14))) * 100
      ),
      9
    ),
    8
  ) AS Nullable(Decimal(18, 8))) AS "Catalog Return Share",
  CAST(round(
    toDecimal256(
      toString(
        CAST(CAST(SUM("composite_01"."Web Returns Quantity") * 1.0 AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity") AS Nullable(Decimal(38, 14))),
          0
        ) AS Nullable(Decimal(38, 14))) / CAST(3.0 AS Nullable(Decimal(38, 14))) * 100
      ),
      9
    ),
    8
  ) AS Nullable(Decimal(18, 8))) AS "Web Return Share",
  CAST(round(
    toDecimal256(
      toString(
        CAST(SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity") AS Nullable(Decimal(38, 14))) / CAST(3.0 AS Nullable(Decimal(38, 14)))
      ),
      7
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "Average Channel Returns"
FROM "composite_01" AS "composite_01"
GROUP BY ALL
HAVING
  accurateCast(trunc(COUNT("composite_01"."Store Returns Count")), 'Nullable(Int32)') > 0
  AND accurateCast(trunc(COUNT("composite_01"."Catalog Returns Count")), 'Nullable(Int32)') > 0
  AND accurateCast(trunc(COUNT("composite_01"."Web Returns Count")), 'Nullable(Int32)') > 0
ORDER BY
  "Item ID" ASC,
  "Store Returns Quantity" ASC
LIMIT 100
