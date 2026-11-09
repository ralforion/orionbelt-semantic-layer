WITH "composite_01" AS (
SELECT "Sales"."salesamount" AS "Total Sales", CAST(NULL AS DECIMAL(38, 20)) AS "Total Purchases"
FROM "orionbelt_1"."sales" AS "Sales"
UNION ALL
SELECT CAST(NULL AS DECIMAL(38, 20)) AS "Total Sales", "Purchases"."purchaseamount" AS "Total Purchases"
FROM "orionbelt_1"."purchases" AS "Purchases"
)
SELECT CASE WHEN CAST(CASE WHEN CAST(SUM("composite_01"."Total Sales") AS DECIMAL(18, 2)) = 0 AND ABS(CAST(SUM("composite_01"."Total Sales") AS DOUBLE)) >= CAST('1E-2' AS DOUBLE) OR ABS(CAST(SUM("composite_01"."Total Sales") AS DECIMAL(38, 2))) >= 10000000000000000 THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("composite_01"."Total Sales") AS DECIMAL(18, 2)) END AS "Total Sales", CASE WHEN CAST(CASE WHEN CAST(SUM("composite_01"."Total Purchases") AS DECIMAL(18, 2)) = 0 AND ABS(CAST(SUM("composite_01"."Total Purchases") AS DOUBLE)) >= CAST('1E-2' AS DOUBLE) OR ABS(CAST(SUM("composite_01"."Total Purchases") AS DECIMAL(38, 2))) >= 10000000000000000 THEN 'OBSL: a value past DECIMAL(18, 2)' ELSE '0' END AS INT) = 0 THEN CAST(SUM("composite_01"."Total Purchases") AS DECIMAL(18, 2)) END AS "Total Purchases"
FROM "composite_01" AS "composite_01"
