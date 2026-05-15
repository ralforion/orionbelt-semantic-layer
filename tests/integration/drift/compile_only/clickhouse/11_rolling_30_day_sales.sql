WITH "cumulative_base" AS (
SELECT toDate("Sales"."salesdate") AS "Sales Date", CAST(round(SUM("Sales"."salesamount"), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY toDate("Sales"."salesdate")
)
SELECT "Sales Date" AS "Sales Date", CAST(round(AVG("Total Sales") OVER (ORDER BY "Sales Date" ASC ROWS BETWEEN 29 PRECEDING AND CURRENT ROW), 0) AS Nullable(Decimal(18, 0))) AS "Rolling 30 Day Sales"
FROM "cumulative_base" AS "cumulative_base"
