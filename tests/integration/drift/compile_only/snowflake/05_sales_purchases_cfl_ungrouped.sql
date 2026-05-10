WITH "composite_01" AS (
SELECT "Sales"."salesamount" AS "Total Sales"
FROM ""."orionbelt_1"."sales" AS "Sales"
UNION ALL BY NAME
SELECT "Purchases"."purchaseamount" AS "Total Purchases"
FROM ""."orionbelt_1"."purchases" AS "Purchases"
)
SELECT CAST(SUM("composite_01"."Total Sales") AS NUMBER(18, 2)) AS "Total Sales", CAST(SUM("composite_01"."Total Purchases") AS NUMBER(18, 2)) AS "Total Purchases"
FROM composite_01 AS "composite_01"
