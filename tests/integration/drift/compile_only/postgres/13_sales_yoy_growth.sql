WITH "date_range" AS (
SELECT MIN("__ob_pop_src"."__ob_bucket") AS min_date,
       MAX("__ob_pop_src"."__ob_bucket") AS max_date
  FROM (
    SELECT date_trunc('month', "Sales"."salesdate") AS "__ob_bucket"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
),
"date_spine" AS (
SELECT d::date AS spine_date,
       CASE WHEN (d + INTERVAL '-1 year')::date >= (SELECT min_date FROM "date_range")
            THEN (d + INTERVAL '-1 year')::date END AS spine_date_prev
FROM generate_series((SELECT min_date FROM "date_range")::timestamp, (SELECT max_date FROM "date_range")::timestamp, INTERVAL '1 month') AS d
),
"pop_base" AS (
SELECT "date_spine".spine_date AS "Sales Month",
       CAST(SUM("__ob_pop_src"."Sales__salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
  FROM (
    SELECT date_trunc('month', "Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
  JOIN "date_spine"
    ON "__ob_pop_src"."__ob_bucket" = "date_spine".spine_date
  GROUP BY 1
UNION ALL
SELECT (SELECT spine_date FROM "date_spine" WHERE 1 = 0) AS "Sales Month",
       CAST(SUM("__ob_pop_src"."Sales__salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
  FROM (
    SELECT date_trunc('month', "Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
  WHERE "__ob_pop_src"."__ob_bucket" IS NULL
  HAVING COUNT(*) > 0
),
"pop_compare" AS (
SELECT "pop_base"."Sales Month" AS "Sales Month",
       "pop_base"."Total Sales" * 1.0 / NULLIF(pop_prev."Total Sales", 0) - 1 AS "Sales YoY Growth"
  FROM (SELECT * FROM "pop_base" WHERE "Sales Month" IS NOT NULL) AS "pop_base"
  LEFT JOIN "date_spine" ON "pop_base"."Sales Month" = "date_spine".spine_date
  LEFT JOIN (SELECT * FROM "pop_base" WHERE "Sales Month" IS NOT NULL) AS pop_prev
    ON "date_spine".spine_date_prev = pop_prev."Sales Month"
UNION ALL
SELECT "pop_base"."Sales Month" AS "Sales Month",
       "pop_base"."Total Sales" * 1.0 / NULLIF(pop_prev."Total Sales", 0) - 1 AS "Sales YoY Growth"
  FROM (SELECT * FROM "pop_base" WHERE "Sales Month" IS NULL) AS "pop_base"
  LEFT JOIN (SELECT * FROM "date_spine" WHERE 1 = 0) AS "date_spine" ON 1 = 0
  LEFT JOIN (SELECT * FROM "pop_base" WHERE 1 = 0) AS pop_prev ON 1 = 0
)
SELECT "Sales Month" AS "Sales Month", CAST("Sales YoY Growth" AS DECIMAL(18, 4)) AS "Sales YoY Growth"
FROM "pop_compare" AS "pop_compare"
