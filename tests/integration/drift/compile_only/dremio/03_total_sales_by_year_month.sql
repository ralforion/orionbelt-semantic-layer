SELECT CAST(DATE_TRUNC('year', "Sales"."salesdate") AS DATE) AS "Sales Year", CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) AS "Sales Month", CAST(SUM("Sales"."salesamount") AS DECIMAL(38, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY CAST(DATE_TRUNC('year', "Sales"."salesdate") AS DATE), CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE)
