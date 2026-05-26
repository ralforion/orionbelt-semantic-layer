WITH "composite_01" AS (
SELECT CAST("Returns"."returnamount" AS NUMBER(18, 2)) AS "Total Returns"
FROM ""."orionbelt_1"."returns" AS "Returns"
UNION ALL BY NAME
SELECT CAST("Sales"."salesamount" AS NUMBER(18, 2)) AS "Total Sales"
FROM ""."orionbelt_1"."sales" AS "Sales"
)
SELECT CAST(SUM("composite_01"."Total Returns") AS NUMBER(18, 2)) AS "Total Returns", CAST(SUM("composite_01"."Total Sales") AS NUMBER(18, 2)) AS "Total Sales", CAST(SUM("composite_01"."Total Returns") / NULLIF(SUM("composite_01"."Total Sales"), 0) AS NUMBER(18, 4)) AS "Return Rate"
FROM "composite_01" AS "composite_01"
