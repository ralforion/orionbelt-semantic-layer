WITH "composite_01" AS (
SELECT CAST(round("Sales"."salesamount", 12) AS Nullable(Decimal(38, 12))) AS "Total Sales", CAST(round(NULL, 12) AS Nullable(Decimal(38, 12))) AS "Total Purchases"
FROM "orionbelt_1"."sales" AS "Sales"
UNION ALL
SELECT CAST(round(NULL, 12) AS Nullable(Decimal(38, 12))) AS "Total Sales", CAST(round("Purchases"."purchaseamount", 12) AS Nullable(Decimal(38, 12))) AS "Total Purchases"
FROM "orionbelt_1"."purchases" AS "Purchases"
)
SELECT CAST(round(SUM("composite_01"."Total Sales"), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales", CAST(round(SUM("composite_01"."Total Purchases"), 2) AS Nullable(Decimal(18, 2))) AS "Total Purchases"
FROM "composite_01" AS "composite_01"
