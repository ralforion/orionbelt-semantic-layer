SELECT toStartOfYear("Sales"."salesdate") AS "Sales Year", toStartOfMonth("Sales"."salesdate") AS "Sales Month", CAST(round(SUM("Sales"."salesamount"), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
