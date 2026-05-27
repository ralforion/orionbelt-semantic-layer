SELECT DATE_TRUNC('year', "Sales"."salesdate") AS "Sales Year", DATE_TRUNC('month', "Sales"."salesdate") AS "Sales Month", CAST(SUM("Sales"."salesamount") AS NUMBER(18, 2)) AS "Total Sales"
FROM ""."orionbelt_1"."sales" AS "Sales"
GROUP BY ALL
