WITH "date_range" AS (
SELECT MIN("__ob_pop_src"."__ob_bucket") AS min_date,
       MAX("__ob_pop_src"."__ob_bucket") AS max_date
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket"
      FROM "orionbelt_1"."sales" AS "Sales"
      WHERE ("Sales"."salesdate" >= '2021-03-01') AND ("Sales"."salesdate" < '2021-05-01')
  ) AS "__ob_pop_src"
),
"date_spine" AS (
SELECT addMonths((SELECT min_date FROM "date_range"), n) AS spine_date,
       CASE WHEN addMonths(addMonths((SELECT min_date FROM "date_range"), n), -1) >= (SELECT min_date FROM "date_range")
            THEN addMonths(addMonths((SELECT min_date FROM "date_range"), n), -1) END AS spine_date_prev
FROM (SELECT arrayJoin(range(0, toUInt32(dateDiff('month', (SELECT min_date FROM "date_range"), (SELECT max_date FROM "date_range"))) + 1)) AS n)
),
"pop_base" AS (
SELECT "date_spine".spine_date AS "Sales Month",
       CAST(round(toDecimal256(toString(SUM("__ob_pop_src"."Sales__salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
      WHERE ("Sales"."salesdate" >= '2021-03-01') AND ("Sales"."salesdate" < '2021-05-01')
  ) AS "__ob_pop_src"
  JOIN "date_spine"
    ON "__ob_pop_src"."__ob_bucket" = "date_spine".spine_date
  GROUP BY 1
UNION ALL
SELECT (SELECT spine_date FROM "date_spine" WHERE 1 = 0) AS "Sales Month",
       CAST(round(toDecimal256(toString(SUM("__ob_pop_src"."Sales__salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
      WHERE ("Sales"."salesdate" >= '2021-03-01') AND ("Sales"."salesdate" < '2021-05-01')
  ) AS "__ob_pop_src"
  WHERE "__ob_pop_src"."__ob_bucket" IS NULL
  HAVING COUNT(*) > 0
),
"date_range_lookback" AS (
SELECT MIN("__ob_pop_src"."__ob_bucket") AS min_date,
       MAX("__ob_pop_src"."__ob_bucket") AS max_date
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
),
"date_spine_lookback" AS (
SELECT addMonths((SELECT min_date FROM "date_range_lookback"), n) AS spine_date,
       CASE WHEN addMonths(addMonths((SELECT min_date FROM "date_range_lookback"), n), -1) >= (SELECT min_date FROM "date_range_lookback")
            THEN addMonths(addMonths((SELECT min_date FROM "date_range_lookback"), n), -1) END AS spine_date_prev
FROM (SELECT arrayJoin(range(0, toUInt32(dateDiff('month', (SELECT min_date FROM "date_range_lookback"), (SELECT max_date FROM "date_range_lookback"))) + 1)) AS n)
),
"pop_lookback" AS (
SELECT "date_spine_lookback".spine_date AS "Sales Month",
       CAST(round(toDecimal256(toString(SUM("__ob_pop_src"."Sales__salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
  JOIN "date_spine_lookback"
    ON "__ob_pop_src"."__ob_bucket" = "date_spine_lookback".spine_date
  GROUP BY 1
UNION ALL
SELECT (SELECT spine_date FROM "date_spine_lookback" WHERE 1 = 0) AS "Sales Month",
       CAST(round(toDecimal256(toString(SUM("__ob_pop_src"."Sales__salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
  WHERE "__ob_pop_src"."__ob_bucket" IS NULL
  HAVING COUNT(*) > 0
),
"pop_compare" AS (
SELECT "pop_base"."Sales Month" AS "Sales Month",
       "pop_base"."Total Sales" AS "Total Sales",
       "pop_base"."Total Sales" - pop_prev."Total Sales" AS "Sales MoM Change"
  FROM (SELECT * FROM "pop_base" WHERE "Sales Month" IS NOT NULL) AS "pop_base"
  LEFT JOIN "date_spine_lookback" ON "pop_base"."Sales Month" = "date_spine_lookback".spine_date
  LEFT JOIN (SELECT * FROM "pop_lookback" WHERE "Sales Month" IS NOT NULL) AS pop_prev
    ON "date_spine_lookback".spine_date_prev = pop_prev."Sales Month"
UNION ALL
SELECT "pop_base"."Sales Month" AS "Sales Month",
       "pop_base"."Total Sales" AS "Total Sales",
       "pop_base"."Total Sales" - pop_prev."Total Sales" AS "Sales MoM Change"
  FROM (SELECT * FROM "pop_base" WHERE "Sales Month" IS NULL) AS "pop_base"
  LEFT JOIN (SELECT * FROM "date_spine_lookback" WHERE 1 = 0) AS "date_spine_lookback" ON 1 = 0
  LEFT JOIN (SELECT * FROM "pop_lookback" WHERE 1 = 0) AS pop_prev ON 1 = 0
)
SELECT "Sales Month" AS "Sales Month", "Total Sales" AS "Total Sales", CAST(round(toDecimal256(toString("Sales MoM Change"), 3), 2) AS Nullable(Decimal(18, 2))) AS "Sales MoM Change"
FROM "pop_compare" AS "pop_compare"
SETTINGS join_use_nulls = 1
