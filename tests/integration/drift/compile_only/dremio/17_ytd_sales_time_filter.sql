WITH "cumulative_base" AS (
SELECT CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) AS "Sales Month", CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("Sales"."salesamount") AS DECIMAL(38, 2))) >= 10000000000000000 OR ABS(CAST(SUM("Sales"."salesamount") AS DOUBLE)) >= CAST('20000000000000000' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
WHERE CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) >= '2021-03-01' AND CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) < '2021-05-01'
GROUP BY CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE)
),
"cumulative_lookback" AS (
SELECT CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE) AS "Sales Month", CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("Sales"."salesamount") AS DECIMAL(38, 2))) >= 10000000000000000 OR ABS(CAST(SUM("Sales"."salesamount") AS DOUBLE)) >= CAST('20000000000000000' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY CAST(DATE_TRUNC('month', "Sales"."salesdate") AS DATE)
),
"cumulative_window" AS (
SELECT "Sales Month" AS "Sales Month", SUM("Total Sales") OVER (PARTITION BY DATE_TRUNC('year', "Sales Month") ORDER BY "Sales Month" ASC NULLS LAST ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS "YTD Sales"
FROM "cumulative_lookback" AS "cumulative_lookback"
),
"cumulative_joined" AS (
SELECT "cumulative_base"."Sales Month" AS "Sales Month", "cumulative_base"."Total Sales" AS "Total Sales", "cumulative_window"."YTD Sales" AS "YTD Sales"
FROM "cumulative_base" AS "cumulative_base"
LEFT JOIN "cumulative_window" AS "cumulative_window" ON "cumulative_base"."Sales Month" = "cumulative_window"."Sales Month" OR "cumulative_base"."Sales Month" IS NULL AND "cumulative_window"."Sales Month" IS NULL
)
SELECT "Sales Month" AS "Sales Month", "Total Sales" AS "Total Sales", CASE WHEN CAST(CASE WHEN ABS(CAST("YTD Sales" AS DECIMAL(38, 2))) >= 10000000000000000 OR ABS(CAST("YTD Sales" AS DOUBLE)) >= CAST('20000000000000000' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST("YTD Sales" AS DECIMAL(18, 2)) END AS "YTD Sales"
FROM "cumulative_joined" AS "cumulative_joined"
