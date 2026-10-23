SELECT CAST(dateTrunc('year', toDate32("Sales"."salesdate")) AS Nullable(Date32)) AS "Sales Year", CAST(dateTrunc('month', toDate32("Sales"."salesdate")) AS Nullable(Date32)) AS "Sales Month", CAST(round(toDecimal256(toString(SUM("Sales"."salesamount")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
