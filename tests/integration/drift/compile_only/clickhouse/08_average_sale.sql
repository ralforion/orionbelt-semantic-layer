SELECT CAST(round(toDecimal256(toString(toFloat64(SUM("Sales"."salesamount")) / NULLIF(NULLIF(COUNT(1), 0), 0)), 3), 2) AS Nullable(Decimal(18, 2))) AS "Average Sale"
FROM "orionbelt_1"."sales" AS "Sales"
SETTINGS join_use_nulls = 1
