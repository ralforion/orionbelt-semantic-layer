WITH "cumulative_base" AS (
SELECT CAST(toDate("Sales"."salesdate") AS Nullable(Date)) AS "Sales Date", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
)
SELECT "Sales Date" AS "Sales Date", CAST(round(toDecimal256(toString(AVG("Total Sales") OVER (ORDER BY "Sales Date" ASC ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)), 1), 0) AS Nullable(Decimal(18, 0))) AS "Rolling 30 Day Sales"
FROM "cumulative_base" AS "cumulative_base"
