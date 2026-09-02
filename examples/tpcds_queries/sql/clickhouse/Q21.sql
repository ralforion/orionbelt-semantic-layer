-- Q21 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Warehouse"."w_warehouse_name" AS "Warehouse Name",
  "Item"."i_item_id" AS "Item ID",
  CAST(round(
    SUM(
      CASE
        WHEN "Date"."d_date" < '2000-03-11'
        THEN "Inventory"."inv_quantity_on_hand"
      END
    ),
    3
  ) AS Nullable(Decimal(18, 3))) AS "Inventory Before",
  CAST(round(
    SUM(
      CASE
        WHEN "Date"."d_date" >= '2000-03-11'
        THEN "Inventory"."inv_quantity_on_hand"
      END
    ),
    3
  ) AS Nullable(Decimal(18, 3))) AS "Inventory After",
  CAST(round(
    toDecimal256(
      toString(
        CAST(SUM(
          CASE
            WHEN "Date"."d_date" >= '2000-03-11'
            THEN "Inventory"."inv_quantity_on_hand"
          END
        ) * 1.0 AS Nullable(Decimal(38, 14))) / nullIf(
          CAST(nullIf(
            SUM(
              CASE
                WHEN "Date"."d_date" < '2000-03-11'
                THEN "Inventory"."inv_quantity_on_hand"
              END
            ),
            0
          ) AS Nullable(Decimal(38, 14))),
          0
        )
      ),
      7
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "Inventory Ratio"
FROM "tpcds"."inventory" AS "Inventory"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Inventory"."inv_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Inventory"."inv_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."warehouse" AS "Warehouse"
  ON "Inventory"."inv_warehouse_sk" = "Warehouse"."w_warehouse_sk"
WHERE
  "Item"."i_current_price" BETWEEN 0.99 AND 1.49
  AND "Date"."d_date" BETWEEN '2000-02-10' AND '2000-04-10'
GROUP BY ALL
HAVING
  "Inventory Ratio" BETWEEN 0.666666666 AND 1.5
ORDER BY
  "Warehouse"."w_warehouse_name" ASC,
  "Item"."i_item_id" ASC
LIMIT 100
