-- Q22 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Item"."i_product_name" AS "Product Name",
  "Item"."i_brand" AS "Brand",
  "Item"."i_class" AS "Class",
  "Item"."i_category" AS "Category",
  CAST(round(toDecimal256(toString(AVG("Inventory"."inv_quantity_on_hand")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Avg Inventory",
  GROUPING("Item"."i_product_name") AS "_g_Product Name",
  GROUPING("Item"."i_brand") AS "_g_Brand",
  GROUPING("Item"."i_class") AS "_g_Class",
  GROUPING("Item"."i_category") AS "_g_Category"
FROM "tpcds"."inventory" AS "Inventory"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Inventory"."inv_item_sk" = "Item"."i_item_sk"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Inventory"."inv_date_sk" = "Date"."d_date_sk"
WHERE
  "Date"."d_month_seq" BETWEEN 1200 AND 1211
GROUP BY
  "Item"."i_product_name",
  "Item"."i_brand",
  "Item"."i_class",
  "Item"."i_category"
  WITH ROLLUP
ORDER BY
  AVG("Inventory"."inv_quantity_on_hand") ASC NULLS FIRST,
  "Item"."i_product_name" ASC NULLS FIRST,
  "Item"."i_brand" ASC NULLS FIRST,
  "Item"."i_class" ASC NULLS FIRST,
  "Item"."i_category" ASC NULLS FIRST
LIMIT 100
