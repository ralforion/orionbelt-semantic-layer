-- Q72 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Item"."i_item_desc" AS "Item Description",
  "Inventory Warehouse"."w_warehouse_name" AS "Inventory Warehouse Name",
  "Catalog Sold Date"."d_week_seq" AS "Catalog Sold Week",
  CAST(COUNT(
    CASE
      WHEN "Promotion"."p_promo_sk" IS NULL
      THEN "Catalog Sales"."cs_order_number"
    END
  ) AS BIGINT) AS "Catalog Sales No Promo Count",
  CAST(COUNT(
    CASE
      WHEN NOT "Promotion"."p_promo_sk" IS NULL
      THEN "Catalog Sales"."cs_order_number"
    END
  ) AS BIGINT) AS "Catalog Sales Promo Count",
  CAST(COUNT("Catalog Sales"."cs_order_number") AS BIGINT) AS "Catalog Sales Row Count"
FROM "main"."catalog_sales" AS "Catalog Sales"
LEFT JOIN "main"."date_dim" AS "Catalog Sold Date"
  ON "Catalog Sales"."cs_sold_date_sk" = "Catalog Sold Date"."d_date_sk"
LEFT JOIN "main"."inventory" AS "Inventory"
  ON "Catalog Sales"."cs_item_sk" = "Inventory"."inv_item_sk"
LEFT JOIN "main"."warehouse" AS "Inventory Warehouse"
  ON "Inventory"."inv_warehouse_sk" = "Inventory Warehouse"."w_warehouse_sk"
LEFT JOIN "main"."item" AS "Item"
  ON "Catalog Sales"."cs_item_sk" = "Item"."i_item_sk"
LEFT JOIN "main"."promotion" AS "Promotion"
  ON "Catalog Sales"."cs_promo_sk" = "Promotion"."p_promo_sk"
LEFT JOIN "main"."date_dim" AS "Inventory Date"
  ON "Inventory"."inv_date_sk" = "Inventory Date"."d_date_sk"
LEFT JOIN "main"."date_dim" AS "Catalog Ship Date"
  ON "Catalog Sales"."cs_ship_date_sk" = "Catalog Ship Date"."d_date_sk"
LEFT JOIN "main"."household_demographics" AS "Household Demographics"
  ON "Catalog Sales"."cs_bill_hdemo_sk" = "Household Demographics"."hd_demo_sk"
LEFT JOIN "main"."customer_demographics" AS "Customer Demographics"
  ON "Catalog Sales"."cs_bill_cdemo_sk" = "Customer Demographics"."cd_demo_sk"
WHERE
  (
    "Catalog Sold Date"."d_week_seq" = "Inventory Date"."d_week_seq"
  ) = TRUE
  AND (
    "Inventory"."inv_quantity_on_hand" < "Catalog Sales"."cs_quantity"
  ) = TRUE
  AND (
    "Catalog Ship Date"."d_date" > "Catalog Sold Date"."d_date" + 5
  ) = TRUE
  AND "Household Demographics"."hd_buy_potential" = '>10000'
  AND "Catalog Sold Date"."d_year" = 1999
  AND "Customer Demographics"."cd_marital_status" = 'D'
GROUP BY ALL
ORDER BY
  COUNT("Catalog Sales"."cs_order_number") DESC,
  "Item"."i_item_desc" ASC,
  "Inventory Warehouse"."w_warehouse_name" ASC,
  "Catalog Sold Date"."d_week_seq" ASC
LIMIT 100
