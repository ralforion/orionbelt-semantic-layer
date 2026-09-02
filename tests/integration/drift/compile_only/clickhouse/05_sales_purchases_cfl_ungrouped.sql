WITH "composite_01" AS (
SELECT CAST(round(toDecimal256(toString("Sales"."salesamount"), 20), 20) AS Nullable(Decimal(76, 20))) AS "Total Sales", CAST(NULL AS Nullable(Decimal(76, 20))) AS "Total Purchases"
FROM "orionbelt_1"."sales" AS "Sales"
UNION ALL
SELECT CAST(NULL AS Nullable(Decimal(76, 20))) AS "Total Sales", CAST(round(toDecimal256(toString("Purchases"."purchaseamount"), 20), 20) AS Nullable(Decimal(76, 20))) AS "Total Purchases"
FROM "orionbelt_1"."purchases" AS "Purchases"
)
SELECT CAST(round(toDecimal256(toString(SUM("composite_01"."Total Sales")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Sales", CAST(round(toDecimal256(toString(SUM("composite_01"."Total Purchases")), 3), 2) AS Nullable(Decimal(18, 2))) AS "Total Purchases"
FROM "composite_01" AS "composite_01"
