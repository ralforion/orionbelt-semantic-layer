SELECT CAST(SUM("Sales"."salesamount") AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
