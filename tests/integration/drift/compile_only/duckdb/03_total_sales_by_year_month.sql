SELECT date_trunc('year', "Sales"."salesdate") AS "Sales Year", date_trunc('month', "Sales"."salesdate") AS "Sales Month", CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
