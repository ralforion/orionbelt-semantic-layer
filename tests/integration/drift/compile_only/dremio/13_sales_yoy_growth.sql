WITH "date_range" AS (
SELECT MIN("__ob_pop_src"."__ob_bucket") AS min_date,
       MAX("__ob_pop_src"."__ob_bucket") AS max_date
  FROM (
    SELECT DATE_TRUNC('month', "Sales"."salesdate") AS "__ob_bucket"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
),
"date_spine" AS (
SELECT d AS spine_date,
       CASE WHEN CAST(TIMESTAMPADD(YEAR, -1, d) AS DATE) >= (SELECT min_date FROM "date_range")
            THEN CAST(TIMESTAMPADD(YEAR, -1, d) AS DATE) END AS spine_date_prev
FROM (
  SELECT CAST(TIMESTAMPADD(MONTH, n, (SELECT min_date FROM "date_range")) AS DATE) AS d
  FROM (
    SELECT a.n + b.n * 10 + c.n * 100 AS n
    FROM (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) a(n)
    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) b(n)
    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) c(n)
  ) AS nums
  WHERE TIMESTAMPADD(MONTH, n, (SELECT min_date FROM "date_range")) <= (SELECT max_date FROM "date_range")
) AS spine
),
"pop_base" AS (
SELECT "date_spine".spine_date AS "Sales Month",
       CAST(SUM("__ob_pop_src"."Sales__salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
  FROM "date_spine"
  LEFT JOIN (
    SELECT DATE_TRUNC('month', "Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
    ON "__ob_pop_src"."__ob_bucket" = "date_spine".spine_date
  GROUP BY 1
),
"pop_compare" AS (
SELECT "pop_base"."Sales Month" AS "Sales Month",
       "pop_base"."Total Sales" / NULLIF(pop_prev."Total Sales", 0) - 1 AS "Sales YoY Growth"
  FROM "pop_base"
  LEFT JOIN "date_spine" ON "pop_base"."Sales Month" = "date_spine".spine_date
  LEFT JOIN "pop_base" AS pop_prev
    ON "date_spine".spine_date_prev = pop_prev."Sales Month"
)
SELECT "Sales Month" AS "Sales Month", CAST("Sales YoY Growth" AS DECIMAL(18, 4)) AS "Sales YoY Growth"
FROM "pop_compare" AS "pop_compare"
