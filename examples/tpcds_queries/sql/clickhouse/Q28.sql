-- Q28 — OBSL-compiled, dialect: clickhouse
-- Regenerate: uv run python sweep.py --dialect clickhouse --dump

SELECT
  CAST(round(
    AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 0 AND 5
        AND (
          "Store Sales"."ss_list_price" BETWEEN 8 AND 18
          OR "Store Sales"."ss_coupon_amt" BETWEEN 459 AND 1459
          OR "Store Sales"."ss_wholesale_cost" BETWEEN 57 AND 77
        )
        THEN "Store Sales"."ss_list_price"
      END
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "B1 LP",
  CAST(COUNT(
    CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 0 AND 5
      AND (
        "Store Sales"."ss_list_price" BETWEEN 8 AND 18
        OR "Store Sales"."ss_coupon_amt" BETWEEN 459 AND 1459
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 57 AND 77
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B1 CNT",
  CAST(COUNT(
    DISTINCT CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 0 AND 5
      AND (
        "Store Sales"."ss_list_price" BETWEEN 8 AND 18
        OR "Store Sales"."ss_coupon_amt" BETWEEN 459 AND 1459
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 57 AND 77
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B1 CNTD",
  CAST(round(
    AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 6 AND 10
        AND (
          "Store Sales"."ss_list_price" BETWEEN 90 AND 100
          OR "Store Sales"."ss_coupon_amt" BETWEEN 2323 AND 3323
          OR "Store Sales"."ss_wholesale_cost" BETWEEN 31 AND 51
        )
        THEN "Store Sales"."ss_list_price"
      END
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "B2 LP",
  CAST(COUNT(
    CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 6 AND 10
      AND (
        "Store Sales"."ss_list_price" BETWEEN 90 AND 100
        OR "Store Sales"."ss_coupon_amt" BETWEEN 2323 AND 3323
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 31 AND 51
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B2 CNT",
  CAST(COUNT(
    DISTINCT CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 6 AND 10
      AND (
        "Store Sales"."ss_list_price" BETWEEN 90 AND 100
        OR "Store Sales"."ss_coupon_amt" BETWEEN 2323 AND 3323
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 31 AND 51
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B2 CNTD",
  CAST(round(
    AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 11 AND 15
        AND (
          "Store Sales"."ss_list_price" BETWEEN 142 AND 152
          OR "Store Sales"."ss_coupon_amt" BETWEEN 12214 AND 13214
          OR "Store Sales"."ss_wholesale_cost" BETWEEN 79 AND 99
        )
        THEN "Store Sales"."ss_list_price"
      END
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "B3 LP",
  CAST(COUNT(
    CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 11 AND 15
      AND (
        "Store Sales"."ss_list_price" BETWEEN 142 AND 152
        OR "Store Sales"."ss_coupon_amt" BETWEEN 12214 AND 13214
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 79 AND 99
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B3 CNT",
  CAST(COUNT(
    DISTINCT CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 11 AND 15
      AND (
        "Store Sales"."ss_list_price" BETWEEN 142 AND 152
        OR "Store Sales"."ss_coupon_amt" BETWEEN 12214 AND 13214
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 79 AND 99
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B3 CNTD",
  CAST(round(
    AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 16 AND 20
        AND (
          "Store Sales"."ss_list_price" BETWEEN 135 AND 145
          OR "Store Sales"."ss_coupon_amt" BETWEEN 6071 AND 7071
          OR "Store Sales"."ss_wholesale_cost" BETWEEN 38 AND 58
        )
        THEN "Store Sales"."ss_list_price"
      END
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "B4 LP",
  CAST(COUNT(
    CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 16 AND 20
      AND (
        "Store Sales"."ss_list_price" BETWEEN 135 AND 145
        OR "Store Sales"."ss_coupon_amt" BETWEEN 6071 AND 7071
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 38 AND 58
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B4 CNT",
  CAST(COUNT(
    DISTINCT CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 16 AND 20
      AND (
        "Store Sales"."ss_list_price" BETWEEN 135 AND 145
        OR "Store Sales"."ss_coupon_amt" BETWEEN 6071 AND 7071
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 38 AND 58
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B4 CNTD",
  CAST(round(
    AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 21 AND 25
        AND (
          "Store Sales"."ss_list_price" BETWEEN 122 AND 132
          OR "Store Sales"."ss_coupon_amt" BETWEEN 836 AND 1836
          OR "Store Sales"."ss_wholesale_cost" BETWEEN 17 AND 37
        )
        THEN "Store Sales"."ss_list_price"
      END
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "B5 LP",
  CAST(COUNT(
    CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 21 AND 25
      AND (
        "Store Sales"."ss_list_price" BETWEEN 122 AND 132
        OR "Store Sales"."ss_coupon_amt" BETWEEN 836 AND 1836
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 17 AND 37
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B5 CNT",
  CAST(COUNT(
    DISTINCT CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 21 AND 25
      AND (
        "Store Sales"."ss_list_price" BETWEEN 122 AND 132
        OR "Store Sales"."ss_coupon_amt" BETWEEN 836 AND 1836
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 17 AND 37
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B5 CNTD",
  CAST(round(
    AVG(
      CASE
        WHEN "Store Sales"."ss_quantity" BETWEEN 26 AND 30
        AND (
          "Store Sales"."ss_list_price" BETWEEN 154 AND 164
          OR "Store Sales"."ss_coupon_amt" BETWEEN 7326 AND 8326
          OR "Store Sales"."ss_wholesale_cost" BETWEEN 7 AND 27
        )
        THEN "Store Sales"."ss_list_price"
      END
    ),
    6
  ) AS Nullable(Decimal(18, 6))) AS "B6 LP",
  CAST(COUNT(
    CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 26 AND 30
      AND (
        "Store Sales"."ss_list_price" BETWEEN 154 AND 164
        OR "Store Sales"."ss_coupon_amt" BETWEEN 7326 AND 8326
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 7 AND 27
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B6 CNT",
  CAST(COUNT(
    DISTINCT CASE
      WHEN "Store Sales"."ss_quantity" BETWEEN 26 AND 30
      AND (
        "Store Sales"."ss_list_price" BETWEEN 154 AND 164
        OR "Store Sales"."ss_coupon_amt" BETWEEN 7326 AND 8326
        OR "Store Sales"."ss_wholesale_cost" BETWEEN 7 AND 27
      )
      THEN "Store Sales"."ss_list_price"
    END
  ) AS Nullable(Int64)) AS "B6 CNTD"
FROM "tpcds"."store_sales" AS "Store Sales"
