WITH "cumulative_base" AS (
SELECT toStartOfMonth("Sales"."salesdate") AS "Sales Month", CAST(round(SUM("Sales"."salesamount"), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY toStartOfMonth("Sales"."salesdate")
)
SELECT "Sales Month" AS "Sales Month", CAST(round(SUM("Total Sales") OVER (ORDER BY "Sales Month" ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS Nullable(Decimal(18, 2))) AS "Cumulative Sales"
FROM "cumulative_base" AS "cumulative_base"
