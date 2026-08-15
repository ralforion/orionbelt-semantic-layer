-- Q9 — OBSL-compiled, dialect: duckdb
-- Regenerate: uv run python sweep.py --dialect duckdb --dump

SELECT
  CAST(CASE
    WHEN COUNT(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 1 AND 20
        THEN "Store Sales"."ss_ticket_number"
      END
    ) > 74129
    THEN AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 1 AND 20
        THEN "Store Sales"."ss_ext_discount_amt"
      END
    )
    ELSE AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 1 AND 20
        THEN "Store Sales"."ss_net_paid"
      END
    )
  END AS DECIMAL(18, 6)) AS "bucket1",
  CAST(CASE
    WHEN COUNT(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 21 AND 40
        THEN "Store Sales"."ss_ticket_number"
      END
    ) > 122840
    THEN AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 21 AND 40
        THEN "Store Sales"."ss_ext_discount_amt"
      END
    )
    ELSE AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 21 AND 40
        THEN "Store Sales"."ss_net_paid"
      END
    )
  END AS DECIMAL(18, 6)) AS "bucket2",
  CAST(CASE
    WHEN COUNT(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 41 AND 60
        THEN "Store Sales"."ss_ticket_number"
      END
    ) > 56580
    THEN AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 41 AND 60
        THEN "Store Sales"."ss_ext_discount_amt"
      END
    )
    ELSE AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 41 AND 60
        THEN "Store Sales"."ss_net_paid"
      END
    )
  END AS DECIMAL(18, 6)) AS "bucket3",
  CAST(CASE
    WHEN COUNT(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 61 AND 80
        THEN "Store Sales"."ss_ticket_number"
      END
    ) > 10097
    THEN AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 61 AND 80
        THEN "Store Sales"."ss_ext_discount_amt"
      END
    )
    ELSE AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 61 AND 80
        THEN "Store Sales"."ss_net_paid"
      END
    )
  END AS DECIMAL(18, 6)) AS "bucket4",
  CAST(CASE
    WHEN COUNT(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 81 AND 100
        THEN "Store Sales"."ss_ticket_number"
      END
    ) > 165306
    THEN AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 81 AND 100
        THEN "Store Sales"."ss_ext_discount_amt"
      END
    )
    ELSE AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 81 AND 100
        THEN "Store Sales"."ss_net_paid"
      END
    )
  END AS DECIMAL(18, 6)) AS "bucket5"
FROM "main"."store_sales" AS "Store Sales"
