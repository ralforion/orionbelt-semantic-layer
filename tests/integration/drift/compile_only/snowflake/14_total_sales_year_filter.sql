SELECT CAST(SUM("Sales"."salesamount") AS NUMBER(18, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
WHERE CAST(DATE_TRUNC('year', "Sales"."salesdate") AS DATE) = '2025-01-01'
