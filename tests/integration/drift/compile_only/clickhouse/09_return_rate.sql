WITH "composite_01" AS (
SELECT "Returns"."returnamount" AS "Total Returns", CAST(NULL AS Nullable(Decimal(18, 2))) AS "Total Sales"
FROM "orionbelt_1"."returns" AS "Returns"
UNION ALL
SELECT CAST(NULL AS Nullable(Decimal(18, 2))) AS "Total Returns", "Sales"."salesamount" AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
)
SELECT CAST(SUM("composite_01"."Total Returns") AS Nullable(Decimal(18, 2))) AS "Total Returns", CAST(SUM("composite_01"."Total Sales") AS Nullable(Decimal(18, 2))) AS "Total Sales", CAST((CAST(SUM("composite_01"."Total Returns") AS Nullable(Decimal(38, 14))) / CAST(SUM("composite_01"."Total Sales") AS Nullable(Decimal(38, 14)))) AS Nullable(Decimal(18, 4))) AS "Return Rate"
FROM composite_01 AS "composite_01"
