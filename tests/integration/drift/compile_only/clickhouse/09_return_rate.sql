WITH "composite_01" AS (
SELECT CAST(round(toDecimal256(toString("Returns"."returnamount"), 20), 20) AS Nullable(Decimal(76, 20))) AS "Total Returns", CAST(NULL AS Nullable(Decimal(76, 20))) AS "Total Sales"
FROM "orionbelt_1"."returns" AS "Returns"
UNION ALL
SELECT CAST(NULL AS Nullable(Decimal(76, 20))) AS "Total Returns", CAST(round(toDecimal256(toString("Sales"."salesamount"), 20), 20) AS Nullable(Decimal(76, 20))) AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
)
SELECT CAST(round(toDecimal256(toString((SUM("composite_01"."Total Returns") + toDecimal256(0, 24)) / NULLIF(NULLIF(SUM("composite_01"."Total Sales"), 0), 0)), 5), 4) AS Nullable(Decimal(18, 4))) AS "Return Rate"
FROM "composite_01" AS "composite_01"
