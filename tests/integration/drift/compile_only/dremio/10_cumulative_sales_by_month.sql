WITH "cumulative_base" AS (
SELECT CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) AS "Sales Month", CASE WHEN CAST(CASE WHEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) = 0 AND ABS(CAST(SUM("Sales"."salesamount") AS DOUBLE)) >= CAST('1E-2' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE)
)
SELECT "Sales Month" AS "Sales Month", CASE WHEN CAST(CASE WHEN CAST(SUM("Total Sales") OVER (ORDER BY "Sales Month" ASC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS DECIMAL(18, 2)) = 0 AND ABS(CAST(SUM("Total Sales") OVER (ORDER BY "Sales Month" ASC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS DOUBLE)) >= CAST('1E-2' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Total Sales") OVER (ORDER BY "Sales Month" ASC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS DECIMAL(18, 2)) END AS "Cumulative Sales"
FROM "cumulative_base" AS "cumulative_base"
