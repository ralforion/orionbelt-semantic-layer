SELECT CAST(round(toDecimal256(toString(CAST(SUM("Sales"."salesamount") AS Nullable(Decimal(38, 14))) / NULLIF(CAST(NULLIF(COUNT(1), 0) AS Nullable(Decimal(38, 14))), 0)), 3), 2) AS Nullable(Decimal(18, 2))) AS "Average Sale"
FROM "orionbelt_1"."sales" AS "Sales"
