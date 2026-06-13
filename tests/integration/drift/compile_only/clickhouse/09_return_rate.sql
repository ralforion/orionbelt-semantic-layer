WITH "composite_01" AS (
SELECT CAST(round("Returns"."returnamount", 2) AS Nullable(Decimal(18, 2))) AS "Total Returns", CAST(round(NULL, 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."returns" AS "Returns"
UNION ALL
SELECT CAST(round(NULL, 2) AS Nullable(Decimal(18, 2))) AS "Total Returns", CAST(round("Sales"."salesamount", 2) AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
)
SELECT CAST(round(CAST(SUM("composite_01"."Total Returns") AS Nullable(Decimal(38, 14))) / CAST(NULLIF(SUM("composite_01"."Total Sales"), 0) AS Nullable(Decimal(38, 14))), 4) AS Nullable(Decimal(18, 4))) AS "Return Rate"
FROM "composite_01" AS "composite_01"
