SELECT CAST(DATE_TRUNC('year', "Sales"."salesdate") AS DATE) AS "Sales Year", CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) AS "Sales Month", CAST(SUM("Sales"."salesamount") AS NUMBER(18, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
