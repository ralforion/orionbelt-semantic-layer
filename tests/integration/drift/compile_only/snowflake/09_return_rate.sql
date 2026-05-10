WITH "composite_01" AS (
SELECT "Returns"."returnamount" AS "Total Returns"
FROM ""."orionbelt_1"."returns" AS "Returns"
UNION ALL BY NAME
SELECT "Sales"."salesamount" AS "Total Sales"
FROM ""."orionbelt_1"."sales" AS "Sales"
)
SELECT CAST(SUM("composite_01"."Total Returns") AS NUMBER(18, 2)) AS "Total Returns", CAST(SUM("composite_01"."Total Sales") AS NUMBER(18, 2)) AS "Total Sales", CAST((SUM("composite_01"."Total Returns") / SUM("composite_01"."Total Sales")) AS NUMBER(18, 4)) AS "Return Rate"
FROM composite_01 AS "composite_01"
