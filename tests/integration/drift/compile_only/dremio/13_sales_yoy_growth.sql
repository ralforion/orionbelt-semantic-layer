WITH "date_range" AS (
SELECT DATE_TRUNC('month', MIN("Sales"."salesdate")) AS min_date,
       DATE_TRUNC('month', MAX("Sales"."salesdate")) AS max_date
  FROM "orionbelt_1"."sales" AS "Sales"
),
"date_spine" AS (
SELECT d AS spine_date,
       CASE WHEN DATE_ADD(d, INTERVAL '-1' YEAR) >= (SELECT min_date FROM date_range)
            THEN DATE_ADD(d, INTERVAL '-1' YEAR) END AS spine_date_prev
FROM (
  SELECT CAST(TIMESTAMPADD(MONTH, n, (SELECT min_date FROM date_range)) AS DATE) AS d
  FROM (
    SELECT a.n + b.n * 10 + c.n * 100 AS n
    FROM (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) a(n)
    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) b(n)
    CROSS JOIN (VALUES(0),(1),(2),(3),(4),(5),(6),(7),(8),(9)) c(n)
  ) AS nums
  WHERE TIMESTAMPADD(MONTH, n, (SELECT min_date FROM date_range)) <= (SELECT max_date FROM date_range)
) AS spine
),
"pop_base" AS (
SELECT date_spine.spine_date AS "Sales Month",
       SUM("Sales"."salesamount") AS "Total Sales"
  FROM date_spine
  LEFT JOIN "orionbelt_1"."sales" AS "Sales"
    ON DATE_TRUNC('month', "Sales"."salesdate") = date_spine.spine_date
  GROUP BY 1
),
"pop_compare" AS (
SELECT pop_base."Sales Month" AS "Sales Month",
       pop_base."Total Sales" / NULLIF(prev."Total Sales", 0) - 1 AS "Sales YoY Growth"
  FROM pop_base
  LEFT JOIN date_spine ON pop_base."Sales Month" = date_spine.spine_date
  LEFT JOIN pop_base AS prev
    ON date_spine.spine_date_prev = prev."Sales Month"
)
SELECT "Sales Month" AS "Sales Month", "Sales YoY Growth" AS "Sales YoY Growth"
FROM "pop_compare" AS "pop_compare"
