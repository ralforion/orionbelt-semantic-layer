WITH "cumulative_base" AS (
SELECT toStartOfMonth("Sales"."salesdate") AS "Sales Month", SUM("Sales"."salesamount") AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY toStartOfMonth("Sales"."salesdate")
)
SELECT "Sales Month" AS "Sales Month", SUM("Total Sales") OVER (ORDER BY "Sales Month" ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS "Cumulative Sales"
FROM cumulative_base AS "cumulative_base"
