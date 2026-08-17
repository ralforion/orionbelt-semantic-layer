WITH "composite_01" AS (
SELECT "Returns"."returnamount" AS "Total Returns", CAST(NULL AS DECIMAL(38, 20)) AS "Total Sales"
FROM "orionbelt_1"."returns" AS "Returns"
UNION ALL
SELECT CAST(NULL AS DECIMAL(38, 20)) AS "Total Returns", "Sales"."salesamount" AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
)
SELECT CAST(SUM("composite_01"."Total Returns") / NULLIF(SUM("composite_01"."Total Sales"), 0) AS DECIMAL(18, 4)) AS "Return Rate"
FROM "composite_01" AS "composite_01"
