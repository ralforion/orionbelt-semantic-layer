WITH "date_range" AS (
SELECT MIN("__ob_pop_src"."__ob_bucket") AS min_date,
       MAX("__ob_pop_src"."__ob_bucket") AS max_date
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket"
      FROM "orionbelt_1"."sales" AS "Sales"
      WHERE "Sales"."salesdate" >= '2021-03-01' AND "Sales"."salesdate" < '2021-05-01'
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
  FROM "date_spine"
  LEFT JOIN (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
      WHERE "Sales"."salesdate" >= '2021-03-01' AND "Sales"."salesdate" < '2021-05-01'
  ) AS "__ob_pop_src"
    ON "__ob_pop_src"."__ob_bucket" = "date_spine".spine_date
  GROUP BY 1
),
"pop_lookback" AS (
SELECT "__ob_pop_src"."__ob_bucket" AS "Sales Month",
       CAST(round(toDecimal256(toString(SUM("__ob_pop_src"."Sales__salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
  FROM (
    SELECT toStartOfMonth("Sales"."salesdate") AS "__ob_bucket",
           "Sales"."salesamount" AS "Sales__salesamount"
      FROM "orionbelt_1"."sales" AS "Sales"
  ) AS "__ob_pop_src"
  GROUP BY 1
),
"pop_compare" AS (
SELECT "pop_base"."Sales Month" AS "Sales Month",
       "pop_base"."Total Sales" AS "Total Sales",
       "pop_base"."Total Sales" - pop_prev_0."Total Sales" AS "Sales MoM Change"
  FROM "pop_base"
  LEFT JOIN "pop_lookback" AS pop_prev_0
    ON pop_prev_0."Sales Month" = addMonths("pop_base"."Sales Month", -1)
)
SELECT "Sales Month" AS "Sales Month", "Total Sales" AS "Total Sales", CAST(round(toDecimal256(toString("Sales MoM Change"), 3), 2) AS Nullable(Decimal(18, 2))) AS "Sales MoM Change"
FROM "pop_compare" AS "pop_compare"
