SELECT CAST(round(toDecimal256(toString((SUM("Sales"."salesamount") + toDecimal256(0, 24)) / NULLIF(NULLIF(COUNT(1), 0), 0)), 3), 2) AS Nullable(Decimal(18, 2))) AS "Average Sale"
FROM "orionbelt_1"."sales" AS "Sales"
