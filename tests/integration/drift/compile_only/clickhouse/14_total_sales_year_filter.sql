SELECT CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
WHERE "Sales"."salesdate" = '2025-01-01'
SETTINGS join_use_nulls = 1
