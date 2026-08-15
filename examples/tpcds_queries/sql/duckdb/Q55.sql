-- Q55 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Item"."i_brand_id" AS "Brand ID",
  "Item"."i_brand" AS "Brand",
  CAST(SUM("Store Sales"."ss_ext_sales_price") AS DECIMAL(18, 2)) AS "Store Sales Amount"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
LEFT JOIN "main"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
WHERE
  "Item"."i_manager_id" = 28 AND "Date"."d_moy" = 11 AND "Date"."d_year" = 1999
GROUP BY ALL
ORDER BY
  SUM("Store Sales"."ss_ext_sales_price") DESC,
  "Item"."i_brand_id" ASC
LIMIT 100
