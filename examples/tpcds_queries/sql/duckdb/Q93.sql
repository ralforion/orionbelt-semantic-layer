-- Q93 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  "Store Sales"."ss_customer_sk" AS "Customer SK",
  CAST(SUM(
    CASE
      WHEN NOT "Store Returns"."sr_return_quantity" IS NULL
      THEN (
        "Store Sales"."ss_quantity" - "Store Returns"."sr_return_quantity"
      ) * "Store Sales"."ss_sales_price"
      ELSE "Store Sales"."ss_quantity" * "Store Sales"."ss_sales_price"
    END
  ) AS DECIMAL(18, 2)) AS "Act Sales"
FROM "main"."store_sales" AS "Store Sales"
LEFT JOIN "main"."store_returns" AS "Store Returns"
  ON "Store Sales"."ss_item_sk" = "Store Returns"."sr_item_sk"
  AND "Store Sales"."ss_ticket_number" = "Store Returns"."sr_ticket_number"
LEFT JOIN "main"."reason" AS "Reason"
  ON "Store Returns"."sr_reason_sk" = "Reason"."r_reason_sk"
WHERE
  "Reason"."r_reason_desc" = 'reason 28'
GROUP BY ALL
ORDER BY
  SUM(
    CASE
      WHEN NOT "Store Returns"."sr_return_quantity" IS NULL
      THEN (
        "Store Sales"."ss_quantity" - "Store Returns"."sr_return_quantity"
      ) * "Store Sales"."ss_sales_price"
      ELSE "Store Sales"."ss_quantity" * "Store Sales"."ss_sales_price"
    END
  ) ASC,
  "Store Sales"."ss_customer_sk" ASC
LIMIT 100
