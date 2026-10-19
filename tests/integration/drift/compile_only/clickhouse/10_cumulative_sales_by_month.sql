WITH "cumulative_base" AS (
SELECT CAST(toStartOfMonth("Sales"."salesdate") AS Nullable(Date)) AS "Sales Month", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
)
SELECT "Sales Month" AS "Sales Month", CAST(round(toDecimal256(toString(SUM("Total Sales") OVER (ORDER BY "Sales Month" ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)), 3), 2) AS Nullable(Decimal(18, 2))) AS "Cumulative Sales"
FROM "cumulative_base" AS "cumulative_base"
