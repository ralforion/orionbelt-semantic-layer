WITH "cumulative_base" AS (
SELECT DATE_TRUNC('month', "Sales"."salesdate") AS "Sales Month", CAST(SUM("Sales"."salesamount") AS NUMBER(18, 2)) AS "Total Sales"
FROM ""."orionbelt_1"."sales" AS "Sales"
GROUP BY DATE_TRUNC('month', "Sales"."salesdate")
)
SELECT "Sales Month" AS "Sales Month", CAST(SUM("Total Sales") OVER (ORDER BY "Sales Month" ASC ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS NUMBER(18, 2)) AS "Cumulative Sales"
FROM "cumulative_base" AS "cumulative_base"
