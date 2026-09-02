-- Q42 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  "Date"."d_year" AS "Year",
  "Item"."i_category_id" AS "Category ID",
  "Item"."i_category" AS "Category",
  CAST(round(SUM("Store Sales"."ss_ext_sales_price"), 2) AS Nullable(Decimal(18, 2))) AS "Store Sales Amount"
FROM "tpcds"."store_sales" AS "Store Sales"
LEFT JOIN "tpcds"."date_dim" AS "Date"
  ON "Store Sales"."ss_sold_date_sk" = "Date"."d_date_sk"
LEFT JOIN "tpcds"."item" AS "Item"
  ON "Store Sales"."ss_item_sk" = "Item"."i_item_sk"
WHERE
  "Item"."i_manager_id" = 1 AND "Date"."d_moy" = 11 AND "Date"."d_year" = 2000
GROUP BY ALL
ORDER BY
  SUM("Store Sales"."ss_ext_sales_price") DESC,
  "Date"."d_year" ASC,
  "Item"."i_category_id" ASC,
  "Item"."i_category" ASC
LIMIT 100
