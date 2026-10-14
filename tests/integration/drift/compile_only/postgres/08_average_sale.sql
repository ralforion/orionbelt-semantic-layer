SELECT CAST(SUM("Sales"."salesamount") * 1.0 / NULLIF(NULLIF(COUNT(1), 0), 0) AS DECIMAL(18, 2)) AS "Average Sale"
FROM "orionbelt_1"."sales" AS "Sales"
