-- Q40 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Warehouse"."w_state" AS "Warehouse State",
  "Item"."i_item_id" AS "Item ID",
  CAST(round(
    toDecimal256(
      toString(
        COALESCE(
          SUM(
            CASE
              WHEN "Date"."d_date" < '2000-03-11'
              THEN "Catalog Sales"."cs_sales_price" - COALESCE("Catalog Returns"."cr_refunded_cash", 0)
            END
          ),
          0
        )
      ),
      3
    ),
    2
  ) AS Nullable(Decimal(18, 2))) AS "Sales Before",
  CAST(round(
    toDecimal256(
      toString(
        COALESCE(
          SUM(
            CASE
              WHEN "Date"."d_date" >= '2000-03-11'
              THEN "Catalog Sales"."cs_sales_price" - COALESCE("Catalog Returns"."cr_refunded_cash", 0)
            END
          ),
          0
        )
      ),
      3
    ),
    2
  ) AS Nullable(Decimal(18, 2))) AS "Sales After"
FROM "tpcds"."catalog_sales" AS "Catalog Sales"
LEFT JOIN "tpcds"."catalog_returns" AS "Catalog Returns"
  ON "Catalog Sales"."cs_order_number" = "Catalog Returns"."cr_order_number"
  AND "Catalog Sales"."cs_item_sk" = "Catalog Returns"."cr_item_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Catalog Sales"."cs_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Catalog Sales"."cs_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."warehouse" AS "Warehouse"
  ON "Catalog Sales"."cs_warehouse_sk" = "Warehouse"."w_warehouse_sk"
WHERE
  "Item"."i_current_price" BETWEEN 0.99 AND 1.49
  AND "Date"."d_date" BETWEEN '2000-02-10' AND '2000-04-10'
  AND NOT (
    "Catalog Sales"."cs_warehouse_sk" IS NULL
  )
GROUP BY ALL
ORDER BY
  "Warehouse"."w_state" ASC,
  "Item"."i_item_id" ASC
