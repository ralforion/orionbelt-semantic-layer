WITH "composite_01" AS (
SELECT "Returns"."returnamount" AS "Total Returns", CAST(NULL AS DECIMAL(38, 20)) AS "Total Sales"
FROM "orionbelt_1"."returns" AS "Returns"
UNION ALL
SELECT CAST(NULL AS DECIMAL(38, 20)) AS "Total Returns", "Sales"."salesamount" AS "Total Sales"
FROM "orionbelt_1"."sales" AS "Sales"
)
SELECT CASE WHEN CAST(CASE WHEN ABS(CAST(COALESCE(SUM("composite_01"."Total Returns"), CAST(NULL AS DECIMAL(1, 1))) / NULLIF(NULLIF(SUM("composite_01"."Total Sales"), 0), 0) AS DECIMAL(38, 4))) >= 100000000000000 THEN 'OBSL: a value past DECIMAL(18, 4)' ELSE '0' END AS INT) = 0 THEN CAST(COALESCE(SUM("composite_01"."Total Returns"), CAST(NULL AS DECIMAL(1, 1))) / NULLIF(NULLIF(SUM("composite_01"."Total Sales"), 0), 0) AS DECIMAL(18, 4)) END AS "Return Rate"
FROM "composite_01" AS "composite_01"
