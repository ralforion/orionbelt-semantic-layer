WITH "cumulative_base" AS (
SELECT CAST(DATE_TRUNC('day', "Sales"."salesdate") AS DATE) AS "Sales Date", CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("Sales"."salesamount") AS DECIMAL(38, 2))) >= 10000000000000000 OR ABS(CAST(SUM("Sales"."salesamount") AS DOUBLE)) >= CAST('20000000000000000' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
WHERE CAST(DATE_TRUNC('day', "Sales"."salesdate") AS DATE) >= '2021-02-01' AND CAST(DATE_TRUNC('day', "Sales"."salesdate") AS DATE) < '2021-03-01'
GROUP BY CAST(DATE_TRUNC('day', "Sales"."salesdate") AS DATE)
),
"cumulative_lookback" AS (
SELECT CAST(DATE_TRUNC('day', "Sales"."salesdate") AS DATE) AS "Sales Date", CASE WHEN CAST(CASE WHEN ABS(CAST(SUM("Sales"."salesamount") AS DECIMAL(38, 2))) >= 10000000000000000 OR ABS(CAST(SUM("Sales"."salesamount") AS DOUBLE)) >= CAST('20000000000000000' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("Sales"."salesamount") AS DECIMAL(18, 2)) END AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
GROUP BY CAST(DATE_TRUNC('day', "Sales"."salesdate") AS DATE)
),
"cumulative_rolling" AS (
SELECT "cumulative_current"."Sales Date" AS "Sales Date", AVG("cumulative_prior"."Total Sales") AS "Rolling 30 Day Sales", MAX("cumulative_prior"."Total Sales") AS "Peak Daily Sales 30D"
FROM "cumulative_lookback" AS "cumulative_current"
INNER JOIN "cumulative_lookback" AS "cumulative_prior" ON TIMESTAMPDIFF(DAY, DATE_TRUNC('day', "cumulative_prior"."Sales Date"), DATE_TRUNC('day', "cumulative_current"."Sales Date")) >= 0 AND TIMESTAMPDIFF(DAY, DATE_TRUNC('day', "cumulative_prior"."Sales Date"), DATE_TRUNC('day', "cumulative_current"."Sales Date")) <= 29 OR "cumulative_current"."Sales Date" IS NULL AND "cumulative_prior"."Sales Date" IS NULL
GROUP BY "cumulative_current"."Sales Date"
),
"cumulative_joined" AS (
SELECT "cumulative_base"."Sales Date" AS "Sales Date", "cumulative_base"."Total Sales" AS "Total Sales", "cumulative_rolling"."Rolling 30 Day Sales" AS "Rolling 30 Day Sales", "cumulative_rolling"."Peak Daily Sales 30D" AS "Peak Daily Sales 30D"
FROM "cumulative_base" AS "cumulative_base"
LEFT JOIN "cumulative_rolling" AS "cumulative_rolling" ON "cumulative_base"."Sales Date" = "cumulative_rolling"."Sales Date" OR "cumulative_base"."Sales Date" IS NULL AND "cumulative_rolling"."Sales Date" IS NULL
)
SELECT "Sales Date" AS "Sales Date", "Total Sales" AS "Total Sales", CASE WHEN CAST(CASE WHEN ABS(CAST("Rolling 30 Day Sales" AS DECIMAL(38, 0))) >= 1000000000000000000 OR ABS(CAST("Rolling 30 Day Sales" AS DOUBLE)) >= CAST('2000000000000000000' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 0)' ELSE '0' END AS INT) = 0 THEN CAST("Rolling 30 Day Sales" AS DECIMAL(18, 0)) END AS "Rolling 30 Day Sales", CASE WHEN CAST(CASE WHEN ABS(CAST("Peak Daily Sales 30D" AS DECIMAL(38, 2))) >= 10000000000000000 OR ABS(CAST("Peak Daily Sales 30D" AS DOUBLE)) >= CAST('20000000000000000' AS DOUBLE) THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST("Peak Daily Sales 30D" AS DECIMAL(18, 2)) END AS "Peak Daily Sales 30D"
FROM "cumulative_joined" AS "cumulative_joined"
