WITH "date_range" AS (
SELECT toStartOfMonth(MIN("Sales"."salesdate")) AS min_date,
       toStartOfMonth(MAX("Sales"."salesdate")) AS max_date
  FROM "orionbelt_1"."sales" AS "Sales"
),
"date_spine" AS (
SELECT addMonths((SELECT min_date FROM "date_range"), n) AS spine_date,
       CASE WHEN addYears(addMonths((SELECT min_date FROM "date_range"), n), -1) >= (SELECT min_date FROM "date_range")
            THEN addYears(addMonths((SELECT min_date FROM "date_range"), n), -1) END AS spine_date_prev
FROM (SELECT arrayJoin(range(0, toUInt32(dateDiff('month', (SELECT min_date FROM "date_range"), (SELECT max_date FROM "date_range"))) + 1)) AS n)
),
"pop_base" AS (
SELECT "date_spine".spine_date AS "Sales Month",
       SUM("Sales"."salesamount") AS "Total Sales"
  FROM "date_spine"
  LEFT JOIN "orionbelt_1"."sales" AS "Sales"
    ON toStartOfMonth("Sales"."salesdate") = "date_spine".spine_date
  GROUP BY 1
),
"pop_compare" AS (
SELECT "pop_base"."Sales Month" AS "Sales Month",
       CAST("pop_base"."Total Sales" AS Nullable(Decimal(38, 14))) / CAST(NULLIF(pop_prev."Total Sales", 0) AS Nullable(Decimal(38, 14))) - 1 AS "Sales YoY Growth"
  FROM "pop_base"
  LEFT JOIN "date_spine" ON "pop_base"."Sales Month" = "date_spine".spine_date
  LEFT JOIN "pop_base" AS pop_prev
    ON "date_spine".spine_date_prev = pop_prev."Sales Month"
)
SELECT "Sales Month" AS "Sales Month", "Sales YoY Growth" AS "Sales YoY Growth"
FROM "pop_compare" AS "pop_compare"
