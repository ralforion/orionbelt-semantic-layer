SELECT CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
WHERE CAST(DATE_TRUNC('year', "Sales"."salesdate") AS Nullable(Date)) = '2025-01-01'
