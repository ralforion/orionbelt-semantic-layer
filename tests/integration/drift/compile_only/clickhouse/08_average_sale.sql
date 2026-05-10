SELECT CAST((CAST(SUM("Sales"."salesamount") AS Nullable(Decimal(38, 14))) / CAST(COUNT(DISTINCT "Sales"."salesid") AS Nullable(Decimal(38, 14)))) AS Nullable(Decimal(18, 2))) AS "Average Sale"
FROM "orionbelt_1"."sales" AS "Sales"
