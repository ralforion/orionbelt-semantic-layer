-- Q83 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

WITH "composite_01" AS (
  SELECT
    "Item"."i_item_id" AS "Item ID",
    CAST("Store Returns"."sr_return_quantity" AS BIGINT) AS "Store Returns Quantity",
    CAST(1 AS INT) AS "Store Returns Count"
  FROM "main"."store_returns" AS "Store Returns"
  LEFT JOIN "main"."date_dim" AS "Date"
    ON "Store Returns"."sr_returned_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "main"."item" AS "Item"
    ON "Store Returns"."sr_item_sk" = "Item"."i_item_sk"
  WHERE
    EXISTS(
      SELECT
        1
      FROM "main"."date_dim" AS "Week Date"
      WHERE
        "Date"."d_week_seq" = "Week Date"."d_week_seq"
        AND "Week Date"."d_date" IN ('2000-06-30', '2000-09-27', '2000-11-17')
    )
  UNION ALL BY NAME
  SELECT
    "Item"."i_item_id" AS "Item ID",
    CAST("Catalog Returns"."cr_return_quantity" AS BIGINT) AS "Catalog Returns Quantity",
    CAST(1 AS INT) AS "Catalog Returns Count"
  FROM "main"."catalog_returns" AS "Catalog Returns"
  LEFT JOIN "main"."date_dim" AS "Date"
    ON "Catalog Returns"."cr_returned_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "main"."item" AS "Item"
    ON "Catalog Returns"."cr_item_sk" = "Item"."i_item_sk"
  WHERE
    EXISTS(
      SELECT
        1
      FROM "main"."date_dim" AS "Week Date"
      WHERE
        "Date"."d_week_seq" = "Week Date"."d_week_seq"
        AND "Week Date"."d_date" IN ('2000-06-30', '2000-09-27', '2000-11-17')
    )
  UNION ALL BY NAME
  SELECT
    "Item"."i_item_id" AS "Item ID",
    CAST("Web Returns"."wr_return_quantity" AS BIGINT) AS "Web Returns Quantity",
    CAST(1 AS INT) AS "Web Returns Count"
  FROM "main"."web_returns" AS "Web Returns"
  LEFT JOIN "main"."date_dim" AS "Date"
    ON "Web Returns"."wr_returned_date_sk" = "Date"."d_date_sk"
  LEFT JOIN "main"."item" AS "Item"
    ON "Web Returns"."wr_item_sk" = "Item"."i_item_sk"
  WHERE
    EXISTS(
      SELECT
        1
      FROM "main"."date_dim" AS "Week Date"
      WHERE
        "Date"."d_week_seq" = "Week Date"."d_week_seq"
        AND "Week Date"."d_date" IN ('2000-06-30', '2000-09-27', '2000-11-17')
    )
)
SELECT
  "Item ID" AS "Item ID",
  CAST(SUM("composite_01"."Store Returns Quantity") AS BIGINT) AS "Store Returns Quantity",
  CAST(SUM("composite_01"."Catalog Returns Quantity") AS BIGINT) AS "Catalog Returns Quantity",
  CAST(SUM("composite_01"."Web Returns Quantity") AS BIGINT) AS "Web Returns Quantity",
  CAST(SUM("composite_01"."Store Returns Quantity") * 1.0 / (
    SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity")
  ) / 3.0 * 100 AS DECIMAL(18, 8)) AS "Store Return Share",
  CAST(SUM("composite_01"."Catalog Returns Quantity") * 1.0 / (
    SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity")
  ) / 3.0 * 100 AS DECIMAL(18, 8)) AS "Catalog Return Share",
  CAST(SUM("composite_01"."Web Returns Quantity") * 1.0 / (
    SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity")
  ) / 3.0 * 100 AS DECIMAL(18, 8)) AS "Web Return Share",
  CAST((
    SUM("composite_01"."Store Returns Quantity") + SUM("composite_01"."Catalog Returns Quantity") + SUM("composite_01"."Web Returns Quantity")
  ) / 3.0 AS DECIMAL(18, 6)) AS "Average Channel Returns"
FROM "composite_01" AS "composite_01"
GROUP BY ALL
HAVING
  CAST(COUNT("composite_01"."Store Returns Count") AS INT) > 0
  AND CAST(COUNT("composite_01"."Catalog Returns Count") AS INT) > 0
  AND CAST(COUNT("composite_01"."Web Returns Count") AS INT) > 0
ORDER BY
  "Item ID" ASC,
  "Store Returns Quantity" ASC
LIMIT 100
