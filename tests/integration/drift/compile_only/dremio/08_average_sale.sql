SELECT CAST(COALESCE(SUM("Sales"."salesamount"), CAST(NULL AS DECIMAL(1, 1))) / NULLIF(NULLIF(COUNT(1), 0), 0) AS DECIMAL(18, 2)) AS "Average Sale"
FROM "orionbelt_1"."sales" AS "Sales"
