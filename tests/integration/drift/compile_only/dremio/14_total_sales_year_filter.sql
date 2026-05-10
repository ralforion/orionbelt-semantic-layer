SELECT CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
WHERE ("Sales"."salesdate" = '2025-01-01')
