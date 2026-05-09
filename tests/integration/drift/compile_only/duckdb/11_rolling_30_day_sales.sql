WITH "cumulative_base" AS (
SELECT date_trunc('day', "Sales"."salesdate") AS "Sales Date", SUM("Sales"."salesamount") AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY date_trunc('day', "Sales"."salesdate")
)
SELECT "Sales Date" AS "Sales Date", AVG("Total Sales") OVER (ORDER BY "Sales Date" ASC ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS "Rolling 30 Day Sales"
FROM cumulative_base AS "cumulative_base"
